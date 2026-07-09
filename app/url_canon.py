"""URL canonicalisation for the Outlook-COM newsletter poller (v0.3.0+).

Two jobs:

1. Strip tracking parameters from a URL. We use an ALLOWLIST of known
   tracker names (utm_*, fbclid, gclid, mc_cid, mc_eid, ref, _ga,
   hsCtaTracking) — never a generic aggressive strip, because destroying
   ``?q=coach`` or ``?id=42`` would break plenty of legitimate URLs.
2. Unwrap redirect wrappers from known newsletter dispatchers
   (Substack, Sendgrid, Mailerlite, etc.) via a single HEAD request to
   follow the redirect chain. Restricted to an ALLOWLIST of hostnames so
   we never HEAD arbitrary URLs (avoids SSRF-style abuse).

Both operations are best-effort: on network failure the original URL is
returned unchanged. Callers must therefore never treat a canonicalised
URL as "guaranteed valid" — only "at worst equal to the input".
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tracker-param strip
# ---------------------------------------------------------------------------

# Case-insensitive: params matched against their .lower() form. Add here
# instead of writing a regex — an explicit set beats a wildcard.
_TRACKER_PREFIXES = ("utm_",)
_TRACKER_EXACT = frozenset(
    {
        "fbclid", "gclid", "gclsrc", "dclid",
        "mc_cid", "mc_eid",
        "ref", "ref_src", "ref_url",
        "_ga", "_gl",
        "hsctatracking", "hsenc", "hsctatoken",
        "yclid", "msclkid",
        "vero_id", "vero_conv",
        "wickedid", "wickedsource", "wickedplacement",
        "email_source", "email_campaign",
        "cmpid", "camp",
    }
)


def _strip_trackers(url: str) -> str:
    """Remove known tracker params from a URL's querystring.

    Untracked params (?q=coach, ?id=42, ...) survive. Fragment is
    preserved. Case-insensitive matching on the parameter *name*, values
    are untouched.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not parsed.query:
        return url

    kept: list[tuple[str, str]] = []
    for name, value in parse_qsl(parsed.query, keep_blank_values=True):
        low = name.lower()
        if low in _TRACKER_EXACT:
            continue
        if any(low.startswith(p) for p in _TRACKER_PREFIXES):
            continue
        kept.append((name, value))

    new_query = urlencode(kept, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


# ---------------------------------------------------------------------------
# Redirector unwrap
# ---------------------------------------------------------------------------

# Hostnames whose primary purpose is to redirect (click-tracking wrappers).
# We only HEAD-follow URLs served by one of these. Everything else is
# returned verbatim so we don't accidentally hit tracking pixels on
# arbitrary domains or expose internal IPs to random hosts.
_REDIRECTOR_HOSTS = frozenset(
    {
        "link.substack.com",
        "click.mailerlite.com",
        "click.mailerlite2.com",
        "email.mg2.substack.com",
        "u.mailinglist.io",
        "trk.klclick.com",
        "trk.klclick1.com",
        "click.convertkit-mail4.com",
        "email.mg.email.substack.com",
    }
)
# Also accept anything whose host matches ``<label>.<known-vendor>``, where
# ``<label>`` is one of the redirector prefixes and ``<known-vendor>`` is
# from a small explicit suffix set. This intentionally rejects
# ``track.attacker.com`` (attacker-registerable) — the earlier lax
# ``<label>.<anything>`` pattern would have admitted it into the
# HEAD-unwrap path.
_REDIRECTOR_VENDOR_SUFFIXES = (
    "substack.com",
    "mailerlite.com",
    "mailerlite2.com",
    "klclick.com",
    "klclick1.com",
    "convertkit-mail4.com",
    "email.substack.com",
    "mg.email.substack.com",
    "mg2.substack.com",
    "sendgrid.net",
    "list-manage.com",
    "campaign-archive.com",
    "mailchimp.com",
    "hs-sites.com",
    "hubspotstarter.net",
)
_REDIRECTOR_LABEL_RE = re.compile(
    r"^(t|click|email|link|trk|mail|track)\.", re.IGNORECASE
)


def _is_redirector(hostname: str) -> bool:
    if not hostname:
        return False
    low = hostname.lower()
    if low in _REDIRECTOR_HOSTS:
        return True
    if not _REDIRECTOR_LABEL_RE.match(low):
        return False
    # Require the tail to be one of the known vendor suffixes — otherwise
    # ``t.attacker.com`` would slip through the label regex alone.
    return any(low.endswith("." + suf) or low == suf for suf in _REDIRECTOR_VENDOR_SUFFIXES)


def _unwrap_redirect(url: str, *, timeout: float = 3.0, max_hops: int = 3) -> str:
    """HEAD-follow the URL, up to ``max_hops`` redirects, only when the host
    matches an allow-listed redirector pattern. Returns the last non-3xx
    Location, or the input URL on any failure."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return url
    if not _is_redirector(parsed.hostname or ""):
        return url

    current = url
    hops_left = max_hops
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": "FTNewsMonitor/1 (link-canonicaliser)"},
        ) as client:
            while hops_left > 0:
                resp = client.head(current)
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        return current
                    # Substack sometimes URL-encodes the target
                    current = unquote(location)
                    hops_left -= 1
                    continue
                # 2xx or an unexpected code — stop unwrapping.
                return current
        # Reached max_hops without a terminal response — return best-effort.
        return current
    except Exception as exc:  # pragma: no cover - network path
        logger.debug("URL unwrap failed for %s: %s", url, exc)
        return url


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def canonicalise_url(raw: str, *, follow_redirects: bool = True) -> str:
    """Return a canonical form of the URL: trackers stripped, wrapper
    hostnames HEAD-unwrapped (if opt-in).

    Never raises. On any failure (unparseable, network, …) the input is
    returned. Callers should still consider the result opaque and use it
    only for dedup — do NOT display the unwrapped URL to the user if you
    haven't verified it's still on-topic.
    """
    if not raw:
        return raw
    stripped = _strip_trackers(raw)
    if follow_redirects:
        unwrapped = _unwrap_redirect(stripped)
        # Re-strip in case the unwrap surfaced a URL with its own trackers.
        return _strip_trackers(unwrapped)
    return stripped

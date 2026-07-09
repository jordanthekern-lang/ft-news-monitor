"""URL canonicalisation for the Outlook newsletter poller (v0.3.0+) and,
since v0.7.1, for the Bing / Google News collectors as well.

Three jobs:

1. Unwrap news-aggregator wrappers (v0.7.1). Bing News rewrites every
   target URL as ``bing.com/news/apiclick.aspx?url=<real>&tid=<random>``;
   Google News uses ``news.google.com/rss/articles/CBM<protobuf>?oc=5``.
   Both give the *same* underlying article a fresh wrapper URL for every
   keyword query, so the ``UNIQUE(url)`` constraint never fires. This
   inflated the DB by ~5× and put the same story into search results
   30+ times. Bing's target sits in the ``url=`` query param (pure
   string parse, no network). Google's is protobuf-encoded and needs a
   HEAD redirect to reveal — kept behind the same allow-list gate the
   newsletter unwrap uses.
2. Strip tracking parameters. ALLOWLIST of names (utm_*, fbclid, gclid,
   mc_cid, mc_eid, ref, _ga, hsCtaTracking, plus aggregator-specific
   tid/mkt/oc/aid/c). Never a generic strip — destroying ``?q=coach``
   or ``?id=42`` would break plenty of legitimate URLs.
3. Unwrap newsletter-dispatcher redirects (Substack, Sendgrid,
   Mailerlite, …) via a single HEAD chain. Allow-listed hostnames only
   to avoid SSRF-style abuse.

All three are best-effort: on network failure the original URL is
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

# Params that are only safe to strip when they sit on a news-aggregator
# wrapper URL — NOT on regular publisher pages, where they may be genuine
# CMS parameters (e.g. bbc.co.uk `?cid=…` as a cache-buster, some sites
# use `?mkt=…` for locale selection). Applied in ``_unwrap_bing_news``
# only, after we've confirmed the host is bing.com.
_AGGREGATOR_TRACKER_EXTRA = frozenset({"tid", "oc", "aid", "cid", "mkt", "c"})


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
# News-aggregator unwrap (v0.7.1)
# ---------------------------------------------------------------------------
#
# Bing News feeds every result through
# ``http://www.bing.com/news/apiclick.aspx?url=<real>&tid=<random>&…``.
# The real target is directly readable from the ``url=`` query parameter,
# so this is a pure string operation — no network call, no failure modes
# other than "URL didn't match the wrapper pattern".
#
# Google News RSS feeds emit
# ``https://news.google.com/rss/articles/CBM<protobuf-base64>?oc=5``. The
# target is protobuf-encoded and needs a HEAD-follow to reveal. We reuse
# the redirector allow-list machinery (same 3s timeout, same UA).


def _unwrap_bing_news(url: str) -> Optional[str]:
    """If ``url`` is a Bing News click-through wrapper, return the real
    target URL from its ``url=`` query param. Otherwise return None.

    Pure string parse — no network. Never raises.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    # Match both `www.bing.com` and rare `bing.com` bare hosts.
    if not (host == "bing.com" or host.endswith(".bing.com")):
        return None
    if not parsed.path.lower().endswith("/apiclick.aspx"):
        return None
    for name, value in parse_qsl(parsed.query, keep_blank_values=False):
        if name.lower() == "url" and value:
            # Bing sometimes URL-encodes the target; unquote once.
            candidate = unquote(value)
            # Refuse to unwrap into another aggregator loop
            # (defensive — real Bing wrappers never nest, but Bing has
            # changed its URL shape before).
            try:
                candidate_host = (urlparse(candidate).hostname or "").lower()
            except ValueError:
                return None
            if candidate_host == "bing.com" or candidate_host.endswith(".bing.com"):
                return None
            return candidate
    return None


def _is_google_news(hostname: str) -> bool:
    if not hostname:
        return False
    low = hostname.lower()
    return low == "news.google.com" or low.endswith(".news.google.com")


def _unwrap_google_news(url: str, *, timeout: float = 3.0) -> Optional[str]:
    """If ``url`` is a Google News RSS wrapper, HEAD-follow to the
    publisher URL. Returns the target on success, None on any failure.

    Only a single hop — Google's RSS wrappers redirect straight to the
    publisher. If Google ever adds an interstitial we'd want to notice,
    not silently follow, so we do not loop like `_unwrap_redirect` does.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if not _is_google_news(parsed.hostname or ""):
        return None
    if "/rss/articles/" not in parsed.path:
        return None
    try:
        with httpx.Client(
            follow_redirects=False,
            timeout=timeout,
            headers={"User-Agent": "FTNewsMonitor/1 (link-canonicaliser)"},
        ) as client:
            resp = client.head(url)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("location")
                if not location:
                    return None
                target = unquote(location)
                # Defensive: refuse to redirect back into Google News
                try:
                    target_host = (urlparse(target).hostname or "").lower()
                except ValueError:
                    return None
                if _is_google_news(target_host):
                    return None
                return target
            return None
    except Exception as exc:  # pragma: no cover - network path
        logger.debug("Google News unwrap failed for %s: %s", url, exc)
        return None


def _unwrap_news_aggregator(url: str, *, follow_redirects: bool = True) -> str:
    """Public entry: unwrap Bing / Google News wrappers when detected.

    Strips aggregator-specific tracking params (``tid``, ``oc``, …)
    ONLY from the wrapper URL itself before probing — the target URL
    is left untouched here; the generic ``_strip_trackers`` runs on it
    later.

    Returns the target URL on successful unwrap, or the input URL
    otherwise (never raises, never returns None).
    """
    if not url:
        return url

    bing_target = _unwrap_bing_news(url)
    if bing_target is not None:
        return bing_target

    if follow_redirects:
        gnews_target = _unwrap_google_news(url)
        if gnews_target is not None:
            return gnews_target

    return url


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
    hostnames unwrapped (aggregator-side pure string, newsletter-side HEAD).

    Order of operations matters:

    1. Unwrap news-aggregator wrappers first — Bing/Google-News URLs
       carry the real target as a parameter (Bing) or a redirect
       target (Google). We do this BEFORE trackers because the wrapper
       itself has params like ``tid=`` / ``oc=`` that are noise; but
       once we're on the publisher URL the params could be genuine.
    2. Strip trackers on the resolved URL.
    3. Follow newsletter-dispatcher redirects (Substack &c.), if
       opt-in via ``follow_redirects``.
    4. Re-strip in case the newsletter unwrap surfaced a URL with its
       own tracking pixels.

    Never raises. On any failure (unparseable, network, …) the closest
    prior value is returned. Callers should still consider the result
    opaque and use it only for dedup — do NOT display the unwrapped URL
    to the user if you haven't verified it's still on-topic.
    """
    if not raw:
        return raw
    # v0.7.1: aggregator wrapper before anything else.
    aggregator_unwrapped = _unwrap_news_aggregator(
        raw, follow_redirects=follow_redirects
    )
    stripped = _strip_trackers(aggregator_unwrapped)
    if follow_redirects:
        unwrapped = _unwrap_redirect(stripped)
        # Re-strip in case the unwrap surfaced a URL with its own trackers.
        return _strip_trackers(unwrapped)
    return stripped

"""Domain extraction and outlet matching."""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import urlparse

import tldextract

from . import database

logger = logging.getLogger(__name__)


# Use a no-suffix-fetch extractor so we never try to update the public-suffix
# list at runtime (offline environments). This is precise enough for our list
# of known outlets.
_EXTRACTOR = tldextract.TLDExtract(suffix_list_urls=())


def extract_domain(url: str) -> Optional[str]:
    """Return registered domain (e.g. 'bmv.de'), or None if not parseable."""
    if not url:
        return None
    try:
        # tldextract handles bare domains, but it expects a scheme — add one.
        target = url if "://" in url else f"https://{url}"
        ext = _EXTRACTOR(target)
        if not ext.domain or not ext.suffix:
            # Fallback: use netloc directly
            host = urlparse(target).netloc
            return host.lower() or None
        return f"{ext.domain}.{ext.suffix}".lower()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("Failed to extract domain from %s: %s", url, exc)
        return None


def extract_subdomain(url: str) -> Optional[str]:
    """Return the full host (e.g. 'trasporti.mobilita.it')."""
    if not url:
        return None
    target = url if "://" in url else f"https://{url}"
    host = urlparse(target).netloc.lower()
    return host or None


def match_article_to_outlet(article_url: str) -> Optional[dict]:
    """Look up an article's domain in the cached outlet_domains table.

    We try the full host first (so 'trasporti.mobilita.it' matches a
    subdomain-specific outlet), then fall back to the registered domain.
    """
    host = extract_subdomain(article_url)
    domain = extract_domain(article_url)

    for candidate in (host, domain):
        if not candidate:
            continue
        row = database.lookup_outlet_domain(candidate)
        if row is not None:
            return dict(row)
    return None

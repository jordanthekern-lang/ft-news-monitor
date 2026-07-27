"""Collector: gathers articles from RSS, Google News and outlet site searches.

Each source is wrapped in a try/except so that a single broken feed cannot
abort the whole run. Per-source health is recorded in the source_health table.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from time import mktime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urljoin, urlparse

import feedparser
import httpx

from . import database, domain_utils, url_canon
from .config import LANG_TO_BING_NEWS, LANG_TO_GOOGLE_NEWS, MARKET_TO_LANGS
from .models import FeedSource, Keyword, NotionConfig, Outlet

logger = logging.getLogger(__name__)


# Common feed-discovery suffixes used by news sites
RSS_DISCOVERY_PATHS = ("/feed", "/rss", "/feed.xml", "/news/feed", "/atom.xml")

# Regex for <link rel="alternate" type="application/rss+xml" href="...">
# (or atom+xml, in any tag-attribute order). Used by the Website-Scrape
# code path to fish a feed URL out of an outlet's HTML when no feed was
# entered in Notion.
_RSS_AUTODISCOVERY_RE = re.compile(
    r'<link\b[^>]*\brel=["\']alternate["\'][^>]*\btype=["\']application/(?:rss|atom)\+xml["\'][^>]*\bhref=["\']([^"\']+)["\']'
    r'|<link\b[^>]*\bhref=["\']([^"\']+)["\'][^>]*\brel=["\']alternate["\'][^>]*\btype=["\']application/(?:rss|atom)\+xml["\']'
    r'|<link\b[^>]*\btype=["\']application/(?:rss|atom)\+xml["\'][^>]*\bhref=["\']([^"\']+)["\']',
    re.IGNORECASE,
)

GOOGLE_NEWS_TEMPLATE = (
    "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
)
BING_NEWS_TEMPLATE = (
    "https://www.bing.com/news/search?q={q}&setlang={lang}&cc={cc}&format=rss"
)
GDELT_DOC_TEMPLATE = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query={q}&mode=ArtList&format=json&maxrecords=250&timespan={timespan}"
)

# Google-News-Zeitfenster-Notation: 1h, 1d, 7d, 1m, 3m, 6m, 1y, 5y.
# Wird als `when:{window}`-Query-Operator im q-Feld eingefügt.
_VALID_TIME_WINDOWS = {"1h", "1d", "7d", "1m", "3m", "6m", "1y", "5y"}

# Bing News nutzt `qft=interval:"..."` mit einer eigenen Notation. Bing's
# RSS-Freshness-Buckets sind grober als Google's: nur "past 24h" (7),
# "past week" (8) und "past month" (9). Alles darüber hinaus (Monate,
# Jahre) fällt zwangsläufig auf den breitesten Bucket zurück — Bing
# indexiert historische Artikel nicht so tief wie Google/GDELT. Für
# "1h" existiert kein natives Bing-Interval; wir fallen auf "past 24h"
# zurück, weil das der engste Bing-Bucket ist.
_BING_INTERVAL_MAP = {
    "1h": '"7"',    # Bing has no "past hour" — collapse to "past 24h"
    "1d": '"7"',    # past 24 hours
    "7d": '"8"',    # past week
    "1m": '"9"',    # past month (Bing's broadest bucket)
    "3m": '"9"',    # >1m collapses to "past month"
    "6m": '"9"',
    "1y": '"9"',
    "5y": '"9"',
}

# GDELT DOC 2.0 nutzt `timespan=` mit eigenem Format.
_GDELT_TIMESPAN_MAP = {
    "1h": "1h",
    "1d": "1d",
    "7d": "1w",
    "1m": "1m",
    "3m": "3m",
    "6m": "6m",
    "1y": "1y",
    "5y": "5y",
}


def _bing_news_enabled() -> bool:
    raw = os.environ.get("BING_NEWS_ENABLED", "true").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _gdelt_enabled() -> bool:
    # v0.7.3: default flipped to false. Measurement on Jordan's live DB
    # (30 days): GDELT contributed 280 URLs not seen by any other source,
    # 0 of them scored >=6 in the AI filter. All 280 were noise the AI
    # filter would have thrown away anyway. Combined with the ~30 min
    # per run that GDELT's 429 backoff spends on retries, the trade is
    # not worth it. Kept behind the flag for markets where local
    # Google/Bing coverage turns out to be thin — flip GDELT_ENABLED=true
    # in .env to re-enable.
    raw = os.environ.get("GDELT_ENABLED", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _resolve_time_window(override: Optional[str] = None) -> str:
    """Pick the time-window token used for Google/Bing/GDELT queries.

    Priority: explicit override > COLLECTOR_TIME_WINDOW env > "3m" default.
    Invalid values silently fall back to "3m" — we don't crash the run
    over a typo in .env.
    """
    if override and override in _VALID_TIME_WINDOWS:
        return override
    raw = os.environ.get("COLLECTOR_TIME_WINDOW", "3m").strip().lower()
    if raw in _VALID_TIME_WINDOWS:
        return raw
    return "3m"


@dataclass
class CollectionStats:
    rss: int = 0
    google_news: int = 0
    bing_news: int = 0
    outlet_search: int = 0
    website_scrape: int = 0
    outlook: int = 0  # v0.3.0: newsletter ingest via Outlook-COM
    gdelt: int = 0    # v0.5.0: GDELT DOC 2.0 aggregator
    duplicates: int = 0
    errors: int = 0

    # v0.5.0 raw counters — how many entries the provider returned BEFORE dedup.
    # Makes the "big-numbers-behind-the-scenes" attrition visible in logs:
    # "Google: 5000 raw / 500 new / 4500 dup" instead of just "500 new".
    raw_google_news: int = 0
    raw_bing_news: int = 0
    raw_gdelt: int = 0

    @property
    def total_new(self) -> int:
        return (
            self.rss
            + self.google_news
            + self.bing_news
            + self.outlet_search
            + self.website_scrape
            + self.outlook
            + self.gdelt
        )

    def as_dict(self) -> Dict[str, int]:
        return {
            "rss_new": self.rss,
            "google_news_new": self.google_news,
            "bing_news_new": self.bing_news,
            "outlet_search_new": self.outlet_search,
            "website_scrape_new": self.website_scrape,
            "outlook_new": self.outlook,
            "gdelt_new": self.gdelt,
            "raw_google_news": self.raw_google_news,
            "raw_bing_news": self.raw_bing_news,
            "raw_gdelt": self.raw_gdelt,
            "duplicates": self.duplicates,
            "errors": self.errors,
            "total_new": self.total_new,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _hash_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _entry_published(entry: Any) -> Optional[datetime]:
    for attr in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, attr, None) or entry.get(attr) if isinstance(entry, dict) else getattr(entry, attr, None)
        if parsed:
            try:
                return datetime.fromtimestamp(mktime(parsed), tz=timezone.utc)
            except (TypeError, ValueError, OverflowError):
                continue
    return None


def _entry_summary(entry: Any) -> str:
    summary = entry.get("summary") if isinstance(entry, dict) else getattr(entry, "summary", "")
    if not summary:
        return ""
    # feedparser sometimes returns the description with HTML; strip the worst
    return summary.replace("\n", " ").strip()


def _resolve_lang_code(sprache: Optional[str], fallback_market: Optional[str]) -> str:
    """Pick a language code (uppercase) for query construction, defaulting
    to EN when neither the source nor the market gives us anything."""
    lang_code = sprache
    if not lang_code and fallback_market:
        langs = MARKET_TO_LANGS.get(fallback_market)
        if langs:
            lang_code = langs[0]
    return (lang_code or "EN").upper()


def _build_google_news_url(
    query: str,
    *,
    sprache: Optional[str],
    fallback_market: Optional[str],
    time_window: Optional[str] = None,
) -> Optional[str]:
    """Build a Google News RSS URL for a query, picking hl/gl/ceid from the
    article language. If language is missing, fall back to the primary
    language of the market.

    ``time_window`` is a token like ``"3m"`` — Google News interprets
    ``when:3m`` as a query operator inside the ``q=`` field, restricting
    results to the last N months. Without it, Google shows only its default
    view (~a few days) which caps recall drastically.
    """
    locale = LANG_TO_GOOGLE_NEWS.get(_resolve_lang_code(sprache, fallback_market))
    if not locale:
        return None
    hl, gl, ceid = locale
    window = _resolve_time_window(time_window)
    q_with_window = f"{query} when:{window}"
    return GOOGLE_NEWS_TEMPLATE.format(
        q=quote_plus(q_with_window), hl=hl, gl=gl, ceid=ceid
    )


def _build_bing_news_url(
    query: str,
    *,
    sprache: Optional[str],
    fallback_market: Optional[str],
    time_window: Optional[str] = None,
) -> Optional[str]:
    """Build a Bing News RSS URL. Bing respects the country code (`cc`) and
    UI language (`setlang`) more reliably than Google does for non-DACH
    queries from a German IP, which is exactly the case we're trying to fix.

    ``time_window`` maps to Bing's ``qft=interval:"N"``-Operator which
    tightens/broadens the freshness window. Bing has no >1m native token,
    so anything longer collapses to the broadest bucket.
    """
    locale = LANG_TO_BING_NEWS.get(_resolve_lang_code(sprache, fallback_market))
    if not locale:
        return None
    setlang, cc = locale
    window = _resolve_time_window(time_window)
    interval = _BING_INTERVAL_MAP.get(window, '"9"')
    base = BING_NEWS_TEMPLATE.format(q=quote_plus(query), lang=setlang, cc=cc)
    # qft-Wert enthält Anführungszeichen ("9") — die müssen URL-encoded werden.
    return f"{base}&qft={quote_plus('interval:' + interval)}"


def _build_gdelt_url(query: str, *, time_window: Optional[str] = None) -> str:
    """Build a GDELT DOC 2.0 API URL. GDELT is a free multilingual news
    aggregator — its index is IP-neutral (crawler-side, not viewer-side),
    which complements Google/Bing's IP-biased views."""
    window = _resolve_time_window(time_window)
    timespan = _GDELT_TIMESPAN_MAP.get(window, "3m")
    return GDELT_DOC_TEMPLATE.format(q=quote_plus(query), timespan=timespan)


def _primary_market(items: Sequence[str]) -> Optional[str]:
    return items[0] if items else None


# ---------------------------------------------------------------------------
# RSS / Google News fetch
# ---------------------------------------------------------------------------

async def _fetch_url(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    try:
        resp = await client.get(url, timeout=20.0, follow_redirects=True)
        if resp.status_code >= 400:
            logger.debug("HTTP %s for %s", resp.status_code, url)
            return None
        return resp.content
    except httpx.HTTPError as exc:
        logger.debug("HTTP error for %s: %s", url, exc)
        return None


# v0.7.2: GDELT DOC 2.0 has a soft rate-limit of ~1 req / 5 s per IP. A
# typical run fires ~90 keyword queries and used to hit 429 on ~90 % of
# them (verified in the 2026-07-27 log). We serialise GDELT calls behind
# a semaphore, enforce a minimum inter-request interval, and retry once
# with exponential backoff when a 429 slips through.
_GDELT_SEMAPHORE = asyncio.Semaphore(1)
_GDELT_MIN_INTERVAL = 5.5  # seconds; Google's docs say ~5s, add a margin.
_GDELT_LAST_CALL: List[float] = [0.0]  # list-of-one so we can mutate inside async ctx
_GDELT_MAX_RETRIES = 2


async def _fetch_gdelt_url(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    """Rate-limited GET for the GDELT DOC 2.0 API.

    Serialises calls (one at a time), spaces them at least
    ``_GDELT_MIN_INTERVAL`` apart, and retries on ``429``/``503`` with
    exponential backoff. Returns ``None`` on all-retries-exhausted or
    non-transient error — callers already handle that case.
    """
    loop = asyncio.get_event_loop()
    async with _GDELT_SEMAPHORE:
        for attempt in range(_GDELT_MAX_RETRIES + 1):
            wait = _GDELT_LAST_CALL[0] + _GDELT_MIN_INTERVAL - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            _GDELT_LAST_CALL[0] = loop.time()
            try:
                resp = await client.get(url, timeout=20.0, follow_redirects=True)
            except httpx.HTTPError as exc:
                logger.debug("GDELT HTTP error for %s: %s", url, exc)
                return None
            if resp.status_code == 200:
                return resp.content
            if resp.status_code in (429, 503) and attempt < _GDELT_MAX_RETRIES:
                backoff = _GDELT_MIN_INTERVAL * (2 ** attempt)
                logger.info(
                    "GDELT %s (attempt %d/%d) — backing off %.1fs",
                    resp.status_code, attempt + 1, _GDELT_MAX_RETRIES + 1, backoff,
                )
                await asyncio.sleep(backoff)
                continue
            logger.debug("GDELT non-retryable HTTP %s for %s", resp.status_code, url)
            return None
    return None


async def _fetch_feed_entries(
    client: httpx.AsyncClient, url: str
) -> List[Dict[str, Any]]:
    raw = await _fetch_url(client, url)
    if raw is None:
        return []
    parsed = feedparser.parse(raw)
    if parsed.bozo:
        logger.debug("feedparser bozo flag set for %s: %s", url, parsed.bozo_exception)
    return list(parsed.entries or [])


async def _fetch_gdelt_entries(
    client: httpx.AsyncClient, url: str
) -> List[Dict[str, Any]]:
    """Fetch GDELT DOC 2.0 JSON response and adapt it to the feedparser-style
    dict shape that ``_store_entries`` consumes.

    GDELT returns ``{"articles": [{url, title, sourcecountry, language, seendate, ...}]}``.
    We map ``seendate`` (compact YYYYMMDDTHHMMSSZ or full ISO) to a
    ``published_parsed`` struct_time so downstream age filtering works.
    """
    # v0.7.2: use the rate-limited fetch so a burst of ~90 keyword
    # queries no longer flames out at 429.
    raw = await _fetch_gdelt_url(client, url)
    if raw is None:
        return []
    try:
        data = json.loads(raw.decode("utf-8", errors="ignore"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.debug("GDELT JSON parse failed for %s: %s", url, exc)
        return []
    articles = data.get("articles") if isinstance(data, dict) else None
    if not isinstance(articles, list):
        return []
    entries: List[Dict[str, Any]] = []
    for art in articles:
        if not isinstance(art, dict):
            continue
        link = art.get("url") or ""
        title = art.get("title") or ""
        if not link or not title:
            continue
        entry: Dict[str, Any] = {
            "link": link,
            "title": title,
            "summary": "",
        }
        # seendate might be "20260707T083000Z" or "2026-07-07T08:30:00Z".
        seen = art.get("seendate") or ""
        parsed_time = None
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                parsed_time = datetime.strptime(seen, fmt).replace(
                    tzinfo=timezone.utc
                ).timetuple()
                break
            except (ValueError, TypeError):
                continue
        if parsed_time is not None:
            entry["published_parsed"] = parsed_time
        entries.append(entry)
    return entries


def _store_entries(
    entries: Iterable[Any],
    *,
    source_name: str,
    source_type: str,
    market: Optional[str],
    language: Optional[str] = None,
    gnews_unwrap_map: Optional[Dict[str, str]] = None,
) -> Tuple[int, int]:
    """Insert entries into DB. Returns (new, duplicates).

    ``language`` is the source language declared by the Notion config for
    this feed/outlet; passing it through means the translator can skip
    detection and act on a reliable signal.

    ``gnews_unwrap_map`` (v0.7.2): optional ``{wrapper_url: publisher_url}``
    mapping produced by an async batch HEAD-unwrap upstream. Applied
    BEFORE the sync ``canonicalise_url`` so Google-News rows dedup on the
    publisher URL rather than on Google's rotating protobuf wrapper.
    """
    new_count = 0
    dup_count = 0
    for entry in entries:
        link = entry.get("link") if isinstance(entry, dict) else getattr(entry, "link", None)
        title = entry.get("title") if isinstance(entry, dict) else getattr(entry, "title", "")
        if not link or not title:
            continue
        # v0.7.2: Prefer the batched Google-News unwrap result when
        # available. The batched HEAD runs upstream so we can afford
        # concurrency; the per-item sync path below still handles Bing.
        if gnews_unwrap_map:
            link = gnews_unwrap_map.get(link, link)
        # v0.7.1: Bing-News apiclick unwrap (pure string, no HTTP) +
        # tracker strip. ``follow_redirects=False`` here on purpose —
        # any Google-News HEAD has already happened in the batched pass.
        link = url_canon.canonicalise_url(link, follow_redirects=False)
        published = _entry_published(entry)
        snippet = _entry_summary(entry)
        rid = database.insert_article(
            url=link,
            title=title,
            source_name=source_name,
            source_type=source_type,
            country=market,
            published_at=published,
            snippet=snippet,
            language=language,
        )
        if rid is None:
            dup_count += 1
        else:
            new_count += 1
    return new_count, dup_count


# ---------------------------------------------------------------------------
# Source iterators
# ---------------------------------------------------------------------------

async def _run_news_query(
    client: httpx.AsyncClient,
    *,
    query: str,
    sprache: Optional[str],
    market: Optional[str],
    source_name: str,
    language: Optional[str],
    stats: CollectionStats,
    time_window: Optional[str] = None,
) -> Tuple[int, int]:
    """Fan a query out to Google News, (optionally) Bing News, and
    (optionally) GDELT DOC 2.0.

    Bing News' geo-segmentation is stricter than Google's, which means that
    when the crawler runs from Germany the Bing leg consistently returns
    more local-language hits for FR/IT/PT/Nordics than the Google leg does.
    GDELT is IP-neutral (crawler-side index) so it complements both for
    non-DE coverage. Running the three cheaply neutralises Google's IP
    bias without a residential proxy.
    """
    total_new = 0
    total_dup = 0

    # v0.5.0 review-fix #3: Google-URL construction returns None when the
    # keyword's language is not in LANG_TO_GOOGLE_NEWS. Old code raised
    # RuntimeError, killing Bing + GDELT for the same query too. We now
    # log-and-skip the Google leg but keep Bing/GDELT running — they may
    # still find good hits for exotic-language keywords.
    google_url = _build_google_news_url(
        query, sprache=sprache, fallback_market=market, time_window=time_window
    )
    if google_url:
        entries = await _fetch_feed_entries(client, google_url)
        stats.raw_google_news += len(entries)
        # v0.7.2: batched HEAD-unwrap for Google's RSS wrappers so
        # different keyword queries returning the same underlying story
        # collapse via the DB's UNIQUE(url) constraint. Runs concurrently
        # (up to 10 in flight); silently no-ops on network failure.
        gnews_links = [
            (e.get("link") if isinstance(e, dict) else getattr(e, "link", None))
            for e in entries
        ]
        gnews_map = await url_canon.batch_unwrap_google_news(client, gnews_links)
        new, dup = _store_entries(
            entries,
            source_name=source_name,
            source_type="google_news",
            market=market,
            language=language,
            gnews_unwrap_map=gnews_map,
        )
        stats.google_news += new
        stats.duplicates += dup
        total_new += new
        total_dup += dup
    else:
        logger.info(
            "Skipping Google News for %r (language %r not in LANG_TO_GOOGLE_NEWS); "
            "Bing/GDELT will still run.",
            query,
            sprache,
        )

    if _bing_news_enabled():
        bing_url = _build_bing_news_url(
            query, sprache=sprache, fallback_market=market, time_window=time_window
        )
        if bing_url:
            entries = await _fetch_feed_entries(client, bing_url)
            stats.raw_bing_news += len(entries)
            new, dup = _store_entries(
                entries,
                source_name=f"{source_name} [Bing]",
                source_type="bing_news",
                market=market,
                language=language,
            )
            stats.bing_news += new
            stats.duplicates += dup
            total_new += new
            total_dup += dup

    if _gdelt_enabled():
        gdelt_url = _build_gdelt_url(query, time_window=time_window)
        try:
            gdelt_entries = await _fetch_gdelt_entries(client, gdelt_url)
        except Exception as exc:
            # GDELT is a third-party API — a hiccup shouldn't sabotage the
            # Google/Bing results already collected for this query.
            logger.debug("GDELT fetch failed for %r: %s", query, exc)
            gdelt_entries = []
        stats.raw_gdelt += len(gdelt_entries)
        if gdelt_entries:
            new, dup = _store_entries(
                gdelt_entries,
                source_name=f"{source_name} [GDELT]",
                source_type="gdelt",
                market=market,
                language=language,
            )
            stats.gdelt += new
            stats.duplicates += dup
            total_new += new
            total_dup += dup

    return total_new, total_dup


async def _process_website_scrape(
    client: httpx.AsyncClient,
    *,
    feed: FeedSource,
    market: Optional[str],
    language: Optional[str],
    stats: CollectionStats,
) -> Tuple[int, int]:
    """Fetch the HTML page and follow an RSS auto-discovery <link> if present.

    A surprising number of "no RSS" outlets do publish a feed, but it isn't
    advertised in the URL bar — only as a `<link rel="alternate">` in the
    page head. Following that gets us a stable feed without a dedicated
    site-specific scraper. If no autodiscovery hint exists we log and skip;
    that's a signal to the user to find a real RSS URL or pick a different
    outlet.
    """
    page_url = feed.rss_feed
    raw = await _fetch_url(client, page_url)
    if raw is None:
        raise RuntimeError(f"Could not fetch {page_url}")

    feed_url = _discover_rss_link(raw, base_url=page_url)
    if not feed_url:
        logger.info(
            "Website-Scrape %s: no RSS autodiscovery link found; skipping",
            feed.rss_feed,
        )
        return 0, 0

    entries = await _fetch_feed_entries(client, feed_url)
    new, dup = _store_entries(
        entries,
        source_name=f"{feed.rss_feed} [autodiscovered]",
        source_type="website_scrape",
        market=market,
        language=language,
    )
    stats.website_scrape += new
    stats.duplicates += dup
    return new, dup


def _discover_rss_link(html: bytes, *, base_url: str) -> Optional[str]:
    """Pull an RSS/Atom URL out of an HTML page's <link rel="alternate">."""
    try:
        text = html.decode("utf-8", errors="ignore")
    except Exception:
        return None
    match = _RSS_AUTODISCOVERY_RE.search(text)
    if not match:
        return None
    href = next((g for g in match.groups() if g), None)
    if not href:
        return None
    if href.startswith(("http://", "https://")):
        return href
    parsed = urlparse(base_url)
    if href.startswith("//"):
        return f"{parsed.scheme}:{href}"
    if href.startswith("/"):
        return f"{parsed.scheme}://{parsed.netloc}{href}"
    return urljoin(base_url, href)


async def _process_feed(
    client: httpx.AsyncClient,
    feed: FeedSource,
    stats: CollectionStats,
    *,
    time_window: Optional[str] = None,
) -> None:
    market = _primary_market(feed.markt)
    name = feed.rss_feed
    typ = (feed.typ or "").lower()
    source_id = f"feed:{name}"

    try:
        feed_lang = feed.sprache or (
            MARKET_TO_LANGS.get(market or "", [None])[0] if market else None
        )
        if typ.startswith("google news"):
            new, dup = await _run_news_query(
                client,
                query=feed.rss_feed,
                sprache=feed.sprache,
                market=market,
                source_name=name,
                language=feed_lang,
                stats=stats,
                time_window=time_window,
            )
        elif typ.startswith("website"):
            new, dup = await _process_website_scrape(
                client, feed=feed, market=market, language=feed_lang, stats=stats
            )
        else:
            entries = await _fetch_feed_entries(client, feed.rss_feed)
            new, dup = _store_entries(
                entries,
                source_name=name,
                source_type="rss",
                market=market,
                language=feed_lang,
            )
            stats.rss += new
            stats.duplicates += dup

        database.record_source_run(
            source_id=source_id,
            source_name=name,
            source_type="feed",
            success=True,
            error=None,
            items_collected=new,
        )
    except Exception as exc:
        logger.warning("Feed %s failed: %s", name, exc)
        stats.errors += 1
        database.record_source_run(
            source_id=source_id,
            source_name=name,
            source_type="feed",
            success=False,
            error=str(exc),
            items_collected=0,
        )


def _build_keyword_queries(keywords: List[Keyword]) -> Dict[Tuple[str, str], List[Keyword]]:
    """Group keywords by (sprache, primary market) — limit 5 per query."""
    grouped: Dict[Tuple[str, str], List[Keyword]] = {}
    from . import enums as _enums  # local import to avoid circular at module load
    for kw in keywords:
        if not kw.suchbegriff or _enums.is_low_priority(kw.prioritaet):
            continue
        sprache = (kw.sprache or "").upper()
        market = _primary_market(kw.markt) or ""
        if not sprache:
            langs = MARKET_TO_LANGS.get(market, [])
            sprache = langs[0] if langs else "EN"
        key = (sprache, market)
        grouped.setdefault(key, []).append(kw)
    return grouped


async def _process_keywords(
    client: httpx.AsyncClient,
    keywords: List[Keyword],
    stats: CollectionStats,
    *,
    time_window: Optional[str] = None,
) -> None:
    """Fire one query per keyword (v0.5.0: un-batched).

    Older revisions grouped keywords in 5-term OR-chunks to reduce the number
    of HTTP round-trips. That looked economical but choked recall: Google
    News frequently returned <100 items on complex OR-queries — a hard
    ceiling since each RSS response caps at ~100 anyway. Un-batching costs
    us N-times the requests but scales recall roughly linearly, which is
    the whole point of v0.5.0.
    """
    grouped = _build_keyword_queries(keywords)
    for (sprache, market), kws in grouped.items():
        for kw in kws:
            query = f'"{kw.suchbegriff}"'
            source_name = f"KW[{sprache}/{market or '-'}]: {kw.suchbegriff}"
            # Slug the source_id from the keyword so per-keyword source_health
            # rows survive across runs (5-chunk index was unstable).
            # v0.5.0 review-fix #4: for non-Latin keywords (Cyrillic, CJK,
            # Arabic, ...) the ASCII-only regex would strip everything and
            # collapse the slug to ''. Fall back to an 8-hex-char hash of
            # the keyword so each row keeps a unique key.
            slug = re.sub(r"[^a-z0-9]+", "-", kw.suchbegriff.lower())[:60].strip("-")
            if not slug:
                slug = "kw-" + hashlib.sha256(
                    kw.suchbegriff.encode("utf-8")
                ).hexdigest()[:8]
            source_id = f"keywords:{sprache}:{market}:{slug}"
            try:
                new, _dup = await _run_news_query(
                    client,
                    query=query,
                    sprache=sprache,
                    market=market or None,
                    source_name=source_name,
                    language=sprache or None,
                    stats=stats,
                    time_window=time_window,
                )
                database.record_source_run(
                    source_id=source_id,
                    source_name=source_name,
                    source_type="keywords",
                    success=True,
                    error=None,
                    items_collected=new,
                )
            except Exception as exc:
                logger.warning("Keyword query %r failed: %s", query, exc)
                stats.errors += 1
                database.record_source_run(
                    source_id=source_id,
                    source_name=source_name,
                    source_type="keywords",
                    success=False,
                    error=str(exc),
                    items_collected=0,
                )


async def _try_outlet_rss(
    client: httpx.AsyncClient, outlet: Outlet, stats: CollectionStats
) -> None:
    if not outlet.url:
        return

    market = _primary_market(outlet.markt)
    outlet_lang = outlet.sprache or (
        MARKET_TO_LANGS.get(market or "", [None])[0] if market else None
    )
    source_id = f"outlet_rss:{outlet.outlet}"
    candidates = [outlet.url]

    # If the URL doesn't already look like a feed, try discovery suffixes
    looks_like_feed = any(
        outlet.url.lower().endswith(ext) for ext in (".xml", "/feed", "/rss", "/atom")
    ) or "feed" in outlet.url.lower() or "rss" in outlet.url.lower()
    if not looks_like_feed:
        for path in RSS_DISCOVERY_PATHS:
            candidates.append(urljoin(outlet.url.rstrip("/") + "/", path.lstrip("/")))

    last_error: Optional[str] = None
    for candidate in candidates:
        try:
            entries = await _fetch_feed_entries(client, candidate)
            if entries:
                new, dup = _store_entries(
                    entries,
                    source_name=f"Outlet RSS: {outlet.outlet}",
                    source_type="rss",
                    market=market,
                    language=outlet_lang,
                )
                stats.rss += new
                stats.duplicates += dup
                database.record_source_run(
                    source_id=source_id,
                    source_name=outlet.outlet,
                    source_type="outlet_rss",
                    success=True,
                    error=None if candidate == outlet.url else f"discovered: {candidate}",
                    items_collected=new,
                )
                return
        except Exception as exc:
            last_error = str(exc)

    logger.info("No RSS entries for outlet %s (last error: %s)", outlet.outlet, last_error)
    database.record_source_run(
        source_id=source_id,
        source_name=outlet.outlet,
        source_type="outlet_rss",
        success=False,
        error=last_error or "no entries",
        items_collected=0,
    )


async def _outlet_site_search(
    client: httpx.AsyncClient,
    outlet: Outlet,
    high_priority_keywords: List[Keyword],
    stats: CollectionStats,
    *,
    time_window: Optional[str] = None,
) -> None:
    if not outlet.url:
        return
    domain = domain_utils.extract_domain(outlet.url)
    if not domain:
        return

    market = _primary_market(outlet.markt)
    sprache = (outlet.sprache or "").upper() or None
    relevant_kws = [
        kw.suchbegriff
        for kw in high_priority_keywords
        if (sprache is None or (kw.sprache or "").upper() == sprache)
        and kw.suchbegriff
    ][:8]
    if not relevant_kws:
        return

    or_block = " OR ".join(f'"{kw}"' for kw in relevant_kws)
    query = f"site:{domain} ({or_block})"

    source_id = f"outlet_search:{outlet.outlet}"
    source_name = f"Site search: {outlet.outlet}"
    try:
        new, _dup = await _run_news_query(
            client,
            query=query,
            sprache=sprache,
            market=market,
            source_name=source_name,
            language=sprache,
            stats=stats,
            time_window=time_window,
        )
        database.record_source_run(
            source_id=source_id,
            source_name=outlet.outlet,
            source_type="outlet_search",
            success=True,
            error=None,
            items_collected=new,
        )
    except Exception as exc:
        logger.warning("Site search for %s failed: %s", outlet.outlet, exc)
        stats.errors += 1
        database.record_source_run(
            source_id=source_id,
            source_name=outlet.outlet,
            source_type="outlet_search",
            success=False,
            error=str(exc),
            items_collected=0,
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run(
    config: NotionConfig,
    *,
    time_window_override: Optional[str] = None,
) -> CollectionStats:
    """Run the collection pipeline.

    ``time_window_override`` lets scripts/backfill.py force a broader window
    (e.g. ``"5y"``) for a one-off historical sweep without changing the
    ``COLLECTOR_TIME_WINDOW`` env for regular daily runs.
    """
    stats = CollectionStats()
    from . import enums as _enums  # local import to avoid circular at module load
    high_priority_keywords = [k for k in config.keywords if _enums.is_high_priority(k.prioritaet)]

    tw = _resolve_time_window(time_window_override)
    logger.info("Collection starting with time_window=%s", tw)

    # v0.7.2: Google News' RSS backend started returning 503 to any UA
    # that self-identifies as a script (previous UA carried
    # "Python/httpx feedparser" and got 100 % blocked). A regular Chrome
    # UA passes the bot filter — verified with curl on 2026-07-27.
    # We keep the FlixBus tag inside a comment header for outlet operators
    # who inspect their access logs.
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/128.0.0.0 Safari/537.36"
        ),
        "X-Contact": "FlixBus PA foerder-monitor",
    }

    async with httpx.AsyncClient(headers=headers, timeout=20.0) as client:
        # 1) Curated RSS / Google News feeds
        for feed in config.feeds:
            await _process_feed(client, feed, stats, time_window=tw)

        # 2) Keyword sweeps via Google News / Bing / GDELT
        await _process_keywords(client, config.keywords, stats, time_window=tw)

        # 3) Outlets with RSS
        rss_outlets = [o for o in config.outlets if o.hat_rss]
        for outlet in rss_outlets:
            await _try_outlet_rss(client, outlet, stats)

        # 4) High-priority outlets without RSS — site:domain search
        site_outlets = [
            o
            for o in config.outlets
            if not o.hat_rss and _enums.is_high_priority(o.prioritaet) and o.url
        ]
        for outlet in site_outlets:
            await _outlet_site_search(
                client, outlet, high_priority_keywords, stats, time_window=tw
            )

    # 5) Newsletter Outlook-COM poller (v0.3.0). Reads a single named
    #    folder from the user's locally running Outlook desktop app.
    #    Failures (Outlook closed, folder missing, COM trust popup blocked)
    #    are isolated to source_health so they don't kill the rest of the run.
    from . import outlook_poller
    if outlook_poller.outlook_enabled():
        folder_id = f"outlook:{os.environ.get('OUTLOOK_FOLDER_PATH', '?')}"
        try:
            new, dup, msgs = await outlook_poller.poll()
            stats.outlook += new
            stats.duplicates += dup
            database.record_source_run(
                source_id=folder_id,
                source_name="Newsletter Outlook",
                source_type="newsletter",
                success=True,
                error=None,
                items_collected=new,
            )
            logger.info(
                "Outlook poll: %d new / %d duplicate / %d msg processed",
                new, dup, msgs,
            )
        except Exception as exc:
            logger.warning("Outlook poll failed: %s", exc)
            stats.errors += 1
            database.record_source_run(
                source_id=folder_id,
                source_name="Newsletter Outlook",
                source_type="newsletter",
                success=False,
                error=str(exc)[:500],
                items_collected=0,
            )

    logger.info("Collection done: %s", stats.as_dict())
    return stats

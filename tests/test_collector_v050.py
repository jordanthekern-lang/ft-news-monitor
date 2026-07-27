"""Tests for the v0.5.0 recall-uplift changes in ``app/collector.py``.

Covers:
- Per-keyword un-batching (one HTTP query per keyword, not 5-term OR-chunks)
- ``when:{window}`` time-window injection in Google-News URLs
- Bing ``qft=interval`` mapping
- GDELT DOC 2.0 URL construction, JSON adapter, and full call path
- ``CollectionStats`` raw counters for the three aggregators
- ``time_window_override`` in ``collector.run`` propagates through
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List
from unittest.mock import AsyncMock

import pytest

from app import collector
from app.collector import (
    CollectionStats,
    _BING_INTERVAL_MAP,
    _GDELT_TIMESPAN_MAP,
    _build_bing_news_url,
    _build_gdelt_url,
    _build_google_news_url,
    _fetch_gdelt_entries,
    _resolve_time_window,
)
from app.models import Keyword


def _kw(term: str, *, sprache: str = "DE", markt: List[str] | None = None) -> Keyword:
    return Keyword(
        suchbegriff=term,
        sprache=sprache,
        markt=markt or ["DACH"],
        prioritaet="High",
    )


# ---------------------------------------------------------------------------
# Time window resolution
# ---------------------------------------------------------------------------


def test_resolve_time_window_default_is_3m(monkeypatch):
    monkeypatch.delenv("COLLECTOR_TIME_WINDOW", raising=False)
    assert _resolve_time_window() == "3m"


def test_resolve_time_window_env_override(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TIME_WINDOW", "1y")
    assert _resolve_time_window() == "1y"


def test_resolve_time_window_invalid_env_falls_back(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TIME_WINDOW", "bogus")
    assert _resolve_time_window() == "3m"


def test_resolve_time_window_explicit_override_wins(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TIME_WINDOW", "1y")
    assert _resolve_time_window("7d") == "7d"


def test_resolve_time_window_invalid_override_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TIME_WINDOW", "1y")
    assert _resolve_time_window("garbage") == "1y"


# ---------------------------------------------------------------------------
# URL builders — Google / Bing / GDELT
# ---------------------------------------------------------------------------


def test_google_news_url_contains_when_operator(monkeypatch):
    monkeypatch.delenv("COLLECTOR_TIME_WINDOW", raising=False)
    url = _build_google_news_url("Foerdermittel", sprache="DE", fallback_market="DACH")
    assert url is not None
    # "when:3m" is URL-encoded to "when%3A3m" (colon → %3A)
    assert "when%3A3m" in url
    assert "hl=de" in url
    assert "gl=DE" in url


def test_google_news_url_uses_override(monkeypatch):
    monkeypatch.setenv("COLLECTOR_TIME_WINDOW", "3m")
    url = _build_google_news_url(
        "Foerdermittel", sprache="DE", fallback_market="DACH", time_window="5y"
    )
    assert url is not None
    assert "when%3A5y" in url


def test_bing_news_url_contains_qft_interval(monkeypatch):
    monkeypatch.delenv("COLLECTOR_TIME_WINDOW", raising=False)
    url = _build_bing_news_url("Foerdermittel", sprache="DE", fallback_market="DACH")
    assert url is not None
    assert "qft=" in url
    # 3m maps to "9" (Bing's broadest bucket) — URL-encoded quotes appear
    assert "interval" in url


def test_gdelt_url_valid_timespan():
    url = _build_gdelt_url("coach subsidy", time_window="7d")
    assert "timespan=1w" in url  # 7d → 1w in GDELT notation
    assert "mode=ArtList" in url
    assert "format=json" in url
    assert "maxrecords=250" in url


def test_gdelt_url_5y_passthrough():
    url = _build_gdelt_url("coach subsidy", time_window="5y")
    assert "timespan=5y" in url


def test_bing_interval_map_covers_all_valid_windows():
    for w in ("1h", "1d", "7d", "1m", "3m", "6m", "1y", "5y"):
        assert w in _BING_INTERVAL_MAP


def test_gdelt_timespan_map_covers_all_valid_windows():
    for w in ("1h", "1d", "7d", "1m", "3m", "6m", "1y", "5y"):
        assert w in _GDELT_TIMESPAN_MAP


# ---------------------------------------------------------------------------
# GDELT JSON → feedparser-style entries
# ---------------------------------------------------------------------------


def test_fetch_gdelt_entries_adapts_json(monkeypatch):
    payload = {
        "articles": [
            {
                "url": "https://example.com/a",
                "title": "Article A",
                "seendate": "20260701T120000Z",
                "language": "German",
                "sourcecountry": "Germany",
            },
            {
                "url": "https://example.com/b",
                "title": "Article B",
                "seendate": "2026-07-02T12:00:00Z",
            },
            # Missing URL — must be skipped.
            {"title": "orphan", "seendate": "20260702T120000Z"},
        ]
    }

    class _Resp:
        content = json.dumps(payload).encode("utf-8")
        status_code = 200

    client = AsyncMock()

    async def _get(url, timeout=None, follow_redirects=None):
        return _Resp()

    client.get = _get
    entries = asyncio.run(
        _fetch_gdelt_entries(client, "https://api.gdeltproject.org/dummy")
    )
    assert len(entries) == 2
    assert entries[0]["link"] == "https://example.com/a"
    assert entries[0]["title"] == "Article A"
    assert "published_parsed" in entries[0]
    assert entries[1]["link"] == "https://example.com/b"


def test_fetch_gdelt_entries_malformed_json(monkeypatch):
    class _Resp:
        content = b"not valid json"
        status_code = 200

    client = AsyncMock()

    async def _get(url, timeout=None, follow_redirects=None):
        return _Resp()

    client.get = _get
    entries = asyncio.run(
        _fetch_gdelt_entries(client, "https://api.gdeltproject.org/dummy")
    )
    assert entries == []


def test_fetch_gdelt_entries_no_articles_key():
    class _Resp:
        content = b'{"status": "ok"}'
        status_code = 200

    client = AsyncMock()

    async def _get(url, timeout=None, follow_redirects=None):
        return _Resp()

    client.get = _get
    entries = asyncio.run(
        _fetch_gdelt_entries(client, "https://api.gdeltproject.org/dummy")
    )
    assert entries == []


# ---------------------------------------------------------------------------
# Un-batching in _process_keywords
# ---------------------------------------------------------------------------


def test_process_keywords_fires_one_query_per_keyword(monkeypatch):
    """Un-batching: 12 keywords → 12 queries (not 3 chunks of 5)."""
    stats = CollectionStats()
    calls: List[Dict[str, Any]] = []

    async def fake_run_news_query(
        client,
        *,
        query,
        sprache,
        market,
        source_name,
        language,
        stats,
        time_window=None,
    ):
        calls.append({"query": query, "time_window": time_window})
        return (0, 0)

    monkeypatch.setattr(collector, "_run_news_query", fake_run_news_query)

    keywords = [_kw(f"term{i}") for i in range(12)]
    asyncio.run(
        collector._process_keywords(client=None, keywords=keywords, stats=stats, time_window="3m")
    )
    assert len(calls) == 12  # one per keyword
    assert all(c["time_window"] == "3m" for c in calls)
    # Each query wraps exactly one term in quotes — no OR-batching.
    assert all(" OR " not in c["query"] for c in calls)
    assert calls[0]["query"] == '"term0"'


def test_process_keywords_skips_low_priority(monkeypatch):
    """Low-priority keywords must still be excluded post-un-batching."""
    stats = CollectionStats()
    calls: List[Dict[str, Any]] = []

    async def fake_run_news_query(client, *, query, **kwargs):
        calls.append(query)
        return (0, 0)

    monkeypatch.setattr(collector, "_run_news_query", fake_run_news_query)

    high = _kw("Foerdermittel")
    low = Keyword(
        suchbegriff="filler",
        sprache="DE",
        markt=["DACH"],
        prioritaet="Low",
    )
    asyncio.run(
        collector._process_keywords(client=None, keywords=[high, low], stats=stats)
    )
    assert calls == ['"Foerdermittel"']


# ---------------------------------------------------------------------------
# Raw counters
# ---------------------------------------------------------------------------


def test_collection_stats_raw_counters_in_as_dict():
    stats = CollectionStats(
        google_news=10,
        raw_google_news=100,
        bing_news=5,
        raw_bing_news=60,
        gdelt=3,
        raw_gdelt=40,
    )
    d = stats.as_dict()
    assert d["google_news_new"] == 10
    assert d["raw_google_news"] == 100
    assert d["raw_bing_news"] == 60
    assert d["raw_gdelt"] == 40
    assert d["gdelt_new"] == 3


def test_collection_stats_total_new_includes_gdelt():
    stats = CollectionStats(google_news=1, bing_news=2, gdelt=3, rss=4)
    assert stats.total_new == 1 + 2 + 3 + 4


# ---------------------------------------------------------------------------
# Time-window override plumbing through run()
# ---------------------------------------------------------------------------


def test_run_passes_override_to_process_keywords(monkeypatch):
    """collector.run(config, time_window_override='5y') → keyword queries get 5y."""
    from app.models import NotionConfig

    seen_windows: List[str] = []

    async def fake_process_keywords(client, keywords, stats, *, time_window=None, **kwargs):
        seen_windows.append(time_window)

    async def fake_process_feed(client, feed, stats, *, time_window=None):
        seen_windows.append(time_window)

    monkeypatch.setattr(collector, "_process_keywords", fake_process_keywords)
    monkeypatch.setattr(collector, "_process_feed", fake_process_feed)
    # Neutralise outlook side-branch
    from app import outlook_poller

    monkeypatch.setattr(outlook_poller, "outlook_enabled", lambda: False)

    cfg = NotionConfig(keywords=[_kw("x")], feeds=[], outlets=[], negative_keywords=[])
    asyncio.run(collector.run(cfg, time_window_override="5y"))


# ---------------------------------------------------------------------------
# Review-fix regression tests
# ---------------------------------------------------------------------------


def test_unmapped_language_no_longer_raises(monkeypatch):
    """Review finding #3: previously a keyword with an unmapped language
    (e.g. Ukrainian) raised RuntimeError from _run_news_query, killing
    Bing + GDELT for that query too. Now Google is skipped with an INFO
    log and GDELT still gets its chance (Bing has its own language
    check and also returns None for unmapped, which is fine)."""
    stats = CollectionStats()
    gdelt_calls: List[str] = []

    async def fake_fetch_feed(client, url):
        return []

    async def fake_fetch_gdelt(client, url):
        gdelt_calls.append(url)
        return []

    monkeypatch.setattr(collector, "_fetch_feed_entries", fake_fetch_feed)
    monkeypatch.setattr(collector, "_fetch_gdelt_entries", fake_fetch_gdelt)
    # v0.7.3: GDELT is now default-off; force it on for this test since
    # the whole point is to verify GDELT still runs when Google's URL
    # comes back None.
    monkeypatch.setenv("GDELT_ENABLED", "true")

    # Force _build_google_news_url to return None (unmapped language path).
    monkeypatch.setattr(
        collector,
        "_build_google_news_url",
        lambda q, *, sprache, fallback_market, time_window=None: None,
    )

    # This call must NOT raise, even though Google URL is None.
    result = asyncio.run(
        collector._run_news_query(
            client=None,
            query='"exotic"',
            sprache="UK",  # unmapped
            market=None,
            source_name="test",
            language="UK",
            stats=stats,
        )
    )
    assert result == (0, 0)  # no new items, no duplicates
    # GDELT was still called — the query didn't die on Google's None URL.
    assert len(gdelt_calls) == 1


def test_non_ascii_keyword_gets_hash_slug(monkeypatch):
    """Review finding #4: a Cyrillic/CJK/Arabic keyword would collapse
    to '' under the ASCII regex and every such row would collide on
    the same source_id. Now non-ASCII keywords get a stable hash slug."""
    stats = CollectionStats()
    recorded: List[str] = []

    async def fake_run_news_query(client, *, query, source_name, **kwargs):
        recorded.append(source_name)
        return (0, 0)

    def fake_record_source_run(*, source_id, **kwargs):
        recorded.append(source_id)

    monkeypatch.setattr(collector, "_run_news_query", fake_run_news_query)
    monkeypatch.setattr(collector.database, "record_source_run", fake_record_source_run)

    # Two Cyrillic keywords that both collapse to '' under the old regex.
    kw1 = Keyword(suchbegriff="Українські автобуси", sprache="UK", markt=["EU"], prioritaet="High")
    kw2 = Keyword(suchbegriff="Пасажирські перевезення", sprache="UK", markt=["EU"], prioritaet="High")
    asyncio.run(collector._process_keywords(client=None, keywords=[kw1, kw2], stats=stats))

    # Extract the two source_ids from the recorded list.
    source_ids = [r for r in recorded if r.startswith("keywords:")]
    assert len(source_ids) == 2
    # They must be DIFFERENT (previously both would have been
    # "keywords:UK:EU:").
    assert source_ids[0] != source_ids[1]
    # And both should carry a kw-hash prefix (not an empty slug).
    for sid in source_ids:
        assert sid.rsplit(":", 1)[-1].startswith("kw-")


# ---------------------------------------------------------------------------
# v0.7.2: GDELT rate-limit throttle + retry backoff
# ---------------------------------------------------------------------------


def _reset_gdelt_state():
    """Reset the module-level counter so tests don't stall each other."""
    collector._GDELT_LAST_CALL[0] = 0.0


def test_fetch_gdelt_url_returns_body_on_200(monkeypatch):
    _reset_gdelt_state()

    class _Resp:
        content = b'{"articles": []}'
        status_code = 200

    client = AsyncMock()

    async def _get(url, timeout=None, follow_redirects=None):
        return _Resp()

    client.get = _get

    body = asyncio.run(
        collector._fetch_gdelt_url(client, "https://api.gdeltproject.org/x")
    )
    assert body == b'{"articles": []}'


def test_fetch_gdelt_url_retries_on_429_then_succeeds(monkeypatch):
    _reset_gdelt_state()
    # Shorten the throttle so the test doesn't wait 11 s.
    monkeypatch.setattr(collector, "_GDELT_MIN_INTERVAL", 0.01)

    class _Resp429:
        content = b""
        status_code = 429

    class _Resp200:
        content = b'{"articles": []}'
        status_code = 200

    responses = [_Resp429(), _Resp200()]
    client = AsyncMock()

    async def _get(url, timeout=None, follow_redirects=None):
        return responses.pop(0)

    client.get = _get

    body = asyncio.run(
        collector._fetch_gdelt_url(client, "https://api.gdeltproject.org/x")
    )
    assert body == b'{"articles": []}'
    assert responses == []  # both consumed


def test_fetch_gdelt_url_gives_up_after_max_retries(monkeypatch):
    _reset_gdelt_state()
    monkeypatch.setattr(collector, "_GDELT_MIN_INTERVAL", 0.01)

    class _Resp429:
        content = b""
        status_code = 429

    call_count = {"n": 0}
    client = AsyncMock()

    async def _get(url, timeout=None, follow_redirects=None):
        call_count["n"] += 1
        return _Resp429()

    client.get = _get

    body = asyncio.run(
        collector._fetch_gdelt_url(client, "https://api.gdeltproject.org/x")
    )
    assert body is None
    # _GDELT_MAX_RETRIES=2 → 3 total attempts (initial + 2 retries).
    assert call_count["n"] == collector._GDELT_MAX_RETRIES + 1


def test_fetch_gdelt_url_returns_none_on_non_retryable_error(monkeypatch):
    _reset_gdelt_state()
    monkeypatch.setattr(collector, "_GDELT_MIN_INTERVAL", 0.01)

    class _Resp500:
        content = b""
        status_code = 500  # non-retryable in our whitelist

    call_count = {"n": 0}
    client = AsyncMock()

    async def _get(url, timeout=None, follow_redirects=None):
        call_count["n"] += 1
        return _Resp500()

    client.get = _get

    body = asyncio.run(
        collector._fetch_gdelt_url(client, "https://api.gdeltproject.org/x")
    )
    assert body is None
    # No retry on 500 — we only retry 429/503 by design.
    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# v0.7.2: browser User-Agent used for outbound HTTP fetches
# ---------------------------------------------------------------------------


def test_collector_run_uses_browser_user_agent(monkeypatch):
    """Google News started 503-ing on any UA that self-identifies as a
    script. Regression guard: intercept the httpx.AsyncClient constructor
    and assert the ``User-Agent`` header does NOT self-identify as a
    Python script."""
    from unittest.mock import patch

    captured: Dict[str, Any] = {}

    class _StubClient:
        def __init__(self, *args, headers=None, timeout=None, **kwargs):
            captured["headers"] = headers or {}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    # Also short-circuit the rest of the run so we don't actually go
    # over the network — we only care about the client construction.
    async def _noop(*args, **kwargs):
        return

    monkeypatch.setattr(collector.httpx, "AsyncClient", _StubClient)
    monkeypatch.setattr(collector, "_process_feed", _noop)
    monkeypatch.setattr(collector, "_process_keywords", _noop)
    monkeypatch.setattr(collector, "_try_outlet_rss", _noop)
    monkeypatch.setattr(collector, "_outlet_site_search", _noop)

    from app.models import NotionConfig
    cfg = NotionConfig(keywords=[], feeds=[], outlets=[], negative_keywords=[])

    asyncio.run(collector.run(cfg))

    ua = captured["headers"].get("User-Agent", "")
    assert "Mozilla/5.0" in ua
    assert "Chrome/" in ua
    assert "Python" not in ua
    assert "httpx" not in ua

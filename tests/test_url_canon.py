"""Tests for app.url_canon (F3 support module)."""

from __future__ import annotations

from app import url_canon


# ---------------------------------------------------------------------------
# _strip_trackers
# ---------------------------------------------------------------------------


def test_strip_utm_family():
    url = "https://example.com/article?utm_source=news&utm_medium=email&utm_campaign=foo"
    assert url_canon._strip_trackers(url) == "https://example.com/article"


def test_strip_fbclid_gclid():
    url = "https://example.com/p?fbclid=abc&gclid=xyz&mc_cid=42"
    assert url_canon._strip_trackers(url) == "https://example.com/p"


def test_strip_preserves_useful_params():
    url = "https://example.com/search?q=coach&page=2&utm_source=news"
    stripped = url_canon._strip_trackers(url)
    assert "q=coach" in stripped
    assert "page=2" in stripped
    assert "utm_source" not in stripped


def test_strip_is_case_insensitive_on_names():
    url = "https://example.com/p?UTM_SOURCE=x&Q=y"
    stripped = url_canon._strip_trackers(url)
    assert "UTM_SOURCE" not in stripped
    assert "Q=y" in stripped


def test_strip_preserves_fragment():
    url = "https://example.com/p?utm_source=x#section"
    assert url_canon._strip_trackers(url).endswith("#section")


def test_strip_noop_on_unparseable():
    assert url_canon._strip_trackers("not a url") == "not a url"


def test_strip_noop_when_no_query():
    assert url_canon._strip_trackers("https://example.com/x") == "https://example.com/x"


# ---------------------------------------------------------------------------
# _is_redirector
# ---------------------------------------------------------------------------


def test_redirector_hostname_matches_known_vendor():
    """After the F3 fix-loop tightening: only known-vendor suffixes match
    the label pattern. ``t.email.example.com`` is REJECTED because the
    tail ``example.com`` isn't a redirector vendor (was previously a
    latent SSRF gate — see F3 review)."""
    assert url_canon._is_redirector("click.mailerlite.com") is True
    assert url_canon._is_redirector("link.substack.com") is True
    # Nested vendor suffix (mg2.substack.com is in _REDIRECTOR_HOSTS)
    assert url_canon._is_redirector("email.mg2.substack.com") is True


def test_redirector_hostname_rejects_attacker_registered():
    """The regex used to admit any ``<label>.<anything>`` host. After the
    fix, an attacker-registerable domain like ``track.attacker.com`` is
    rejected because ``attacker.com`` is not a known vendor suffix."""
    assert url_canon._is_redirector("track.attacker.com") is False
    assert url_canon._is_redirector("mail.evil.tld") is False
    assert url_canon._is_redirector("t.co") is False  # too short — no known-vendor tail


def test_redirector_hostname_does_not_match_regular_domain():
    assert url_canon._is_redirector("example.com") is False
    assert url_canon._is_redirector("www.sustainable-bus.com") is False


def test_redirector_hostname_empty_returns_false():
    assert url_canon._is_redirector("") is False


# ---------------------------------------------------------------------------
# canonicalise_url end-to-end (no network in these — follow_redirects=False)
# ---------------------------------------------------------------------------


def test_canonicalise_strips_trackers_without_network():
    url = "https://sustainable-bus.com/coach?utm_source=nl&id=42"
    assert url_canon.canonicalise_url(url, follow_redirects=False) == (
        "https://sustainable-bus.com/coach?id=42"
    )


def test_canonicalise_returns_input_on_empty():
    assert url_canon.canonicalise_url("", follow_redirects=False) == ""


def test_canonicalise_preserves_useful_query():
    url = "https://example.com/p?q=elettrico&utm_medium=email"
    result = url_canon.canonicalise_url(url, follow_redirects=False)
    assert "q=elettrico" in result
    assert "utm_medium" not in result


def test_canonicalise_returns_input_when_not_a_redirector():
    """Even with follow_redirects=True, a non-allowlisted host should never
    be HEAD-requested — we return the tracker-stripped URL untouched."""
    url = "https://sustainable-bus.com/coach?utm_source=x"
    result = url_canon.canonicalise_url(url, follow_redirects=True)
    assert result == "https://sustainable-bus.com/coach"


# ---------------------------------------------------------------------------
# v0.7.1: _unwrap_bing_news — pure-string aggregator unwrap
# ---------------------------------------------------------------------------


def test_bing_unwrap_extracts_url_param():
    """The real target URL sits in the ``url=`` query param, URL-encoded."""
    wrapped = (
        "http://www.bing.com/news/apiclick.aspx"
        "?ref=FexRss&aid=&tid=6a4d3ece36524b32b50fc801ca0509bc"
        "&url=https%3A%2F%2Fexample.com%2Farticle"
        "&c=8095247579605366018&mkt=en-gb"
    )
    assert url_canon._unwrap_bing_news(wrapped) == "https://example.com/article"


def test_bing_unwrap_none_for_non_bing_host():
    assert url_canon._unwrap_bing_news("https://autoflotte.de/article") is None


def test_bing_unwrap_none_for_bing_non_apiclick_path():
    """Only /apiclick.aspx is a click-through wrapper — other Bing pages
    (e.g. /search) are left as-is."""
    assert url_canon._unwrap_bing_news("http://www.bing.com/search?q=foo") is None


def test_bing_unwrap_none_when_url_param_missing():
    """A malformed wrapper (no url= param) returns None, not a crash."""
    wrapped = "http://www.bing.com/news/apiclick.aspx?tid=abc&mkt=en-us"
    assert url_canon._unwrap_bing_news(wrapped) is None


def test_bing_unwrap_refuses_nested_bing_target():
    """Defensive: if the ``url=`` param itself points back into Bing, we
    do not unwrap into a loop."""
    wrapped = (
        "http://www.bing.com/news/apiclick.aspx"
        "?url=https%3A%2F%2Fwww.bing.com%2Fnews%2Fapiclick.aspx%3Ftid%3Dfoo"
    )
    assert url_canon._unwrap_bing_news(wrapped) is None


def test_bing_unwrap_case_insensitive_host():
    """Match Bing regardless of URL case."""
    wrapped = (
        "http://WWW.BING.COM/news/apiclick.aspx"
        "?url=https%3A%2F%2Fexample.com%2Fa"
    )
    assert url_canon._unwrap_bing_news(wrapped) == "https://example.com/a"


def test_canonicalise_bing_wrappers_collapse_across_tid_values():
    """The core dedup guarantee: same target, different tid= values must
    canonicalise to the same output URL so the DB's ``UNIQUE(url)``
    constraint fires."""
    a = "http://www.bing.com/news/apiclick.aspx?tid=AAA&url=https%3A%2F%2Fex.com%2Fa"
    b = "http://www.bing.com/news/apiclick.aspx?tid=BBB&url=https%3A%2F%2Fex.com%2Fa"
    assert url_canon.canonicalise_url(a, follow_redirects=False) == \
           url_canon.canonicalise_url(b, follow_redirects=False)


def test_canonicalise_publisher_url_with_cid_not_stripped():
    """``cid=`` on a real publisher URL is NOT stripped (some sites use it
    as a cache-buster or category filter). Only the aggregator wrapper's
    own tracker params get killed via the unwrap step; the publisher URL
    goes through the standard ALLOWLIST-only strip."""
    url = "https://bbc.co.uk/news?cid=uk"
    result = url_canon.canonicalise_url(url, follow_redirects=False)
    assert result == "https://bbc.co.uk/news?cid=uk"


# ---------------------------------------------------------------------------
# v0.7.1: _unwrap_google_news — HEAD-based unwrap detection
# ---------------------------------------------------------------------------


def test_google_news_unwrap_none_for_non_gnews_host():
    """Guard: no HEAD probe unless the URL is on news.google.com."""
    assert url_canon._unwrap_google_news("https://autoflotte.de/x") is None


def test_google_news_unwrap_none_for_gnews_but_non_rss_path():
    """news.google.com/foo (not /rss/articles/) is left alone."""
    assert url_canon._unwrap_google_news("https://news.google.com/search?q=foo") is None


def test_canonicalise_skips_google_news_when_follow_redirects_false():
    """The sync canonicalise_url path with follow_redirects=False leaves
    Google-News wrappers alone — the collector unwraps them via the
    async ``batch_unwrap_google_news`` pass before calling this."""
    gnews = "https://news.google.com/rss/articles/CBMi-AFAcbdefX"
    result = url_canon.canonicalise_url(gnews, follow_redirects=False)
    assert result == gnews  # unchanged in fast path


# ---------------------------------------------------------------------------
# v0.7.2: batch_unwrap_google_news — async batched HEAD-unwrap
# ---------------------------------------------------------------------------

import asyncio
from unittest.mock import AsyncMock, MagicMock


def _make_head_resp(status: int, location: str | None = None):
    r = MagicMock()
    r.status_code = status
    r.headers = {"location": location} if location else {}
    return r


def test_batch_unwrap_maps_wrapper_to_publisher():
    """Each wrapper URL that HEADs to a 302 with a Location: pointing at
    a real publisher ends up in the returned map."""
    client = AsyncMock()
    responses = {
        "https://news.google.com/rss/articles/CBM_A": _make_head_resp(
            302, "https://publisher.example/a"
        ),
        "https://news.google.com/rss/articles/CBM_B": _make_head_resp(
            302, "https://publisher.example/b"
        ),
    }

    async def _head(url, timeout=None, follow_redirects=None, headers=None):
        return responses[url]

    client.head = _head

    urls = list(responses.keys())
    result = asyncio.run(url_canon.batch_unwrap_google_news(client, urls))
    assert result == {
        "https://news.google.com/rss/articles/CBM_A": "https://publisher.example/a",
        "https://news.google.com/rss/articles/CBM_B": "https://publisher.example/b",
    }


def test_batch_unwrap_skips_non_gnews_urls():
    """Non-Google-News URLs are omitted from the result (falls back to
    original in the caller)."""
    client = AsyncMock()

    async def _head(url, timeout=None, follow_redirects=None, headers=None):
        raise AssertionError("HEAD must not be called on non-gnews URL")

    client.head = _head

    urls = ["https://autoflotte.de/article", "https://example.com/x"]
    result = asyncio.run(url_canon.batch_unwrap_google_news(client, urls))
    assert result == {}


def test_batch_unwrap_omits_failed_head():
    """If the HEAD errors or returns a non-3xx, that URL is left out —
    caller retains the wrapper URL."""
    client = AsyncMock()

    async def _head(url, timeout=None, follow_redirects=None, headers=None):
        return _make_head_resp(200)  # not a redirect → skip

    client.head = _head

    urls = ["https://news.google.com/rss/articles/CBM_no_redirect"]
    result = asyncio.run(url_canon.batch_unwrap_google_news(client, urls))
    assert result == {}


def test_batch_unwrap_deduplicates_input():
    """Identical wrapper URLs share one HEAD call — mostly to keep runs
    cheap when several keyword queries return the same story."""
    client = AsyncMock()
    calls = []

    async def _head(url, timeout=None, follow_redirects=None, headers=None):
        calls.append(url)
        return _make_head_resp(302, "https://publisher.example/x")

    client.head = _head

    url = "https://news.google.com/rss/articles/CBM_same"
    result = asyncio.run(
        url_canon.batch_unwrap_google_news(client, [url, url, url])
    )
    assert result == {url: "https://publisher.example/x"}
    assert len(calls) == 1


def test_batch_unwrap_refuses_gnews_to_gnews_redirect():
    """Defensive: if the Location: points BACK into Google News, we
    treat it as a failure and omit."""
    client = AsyncMock()

    async def _head(url, timeout=None, follow_redirects=None, headers=None):
        return _make_head_resp(302, "https://news.google.com/rss/loop")

    client.head = _head

    result = asyncio.run(
        url_canon.batch_unwrap_google_news(
            client, ["https://news.google.com/rss/articles/CBM_loop"]
        )
    )
    assert result == {}

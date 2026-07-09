"""Tests for market-scoped config loading (Task 3, item 4).

Rules:
  - load_config(markets=[...]) filters keywords/feeds/outlets/negative_keywords
    to rows whose markt list intersects the selection OR is empty (global).
  - The "All" sentinel ("Alle" legacy alias) in a markt list is global too.
  - markets=None or [] → unfiltered (previous behaviour).
  - Outlet-domain trust sync stays global regardless of market selection.
"""

from __future__ import annotations

from app import database, local_config


def _seed_keyword(term: str, markt: list) -> None:
    database.insert_config_row(
        "keywords",
        {
            "suchbegriff": term,
            "markt": markt,
            "themenbereich": [],
            "prioritaet": "High",
            "sprache": "DE",
            "notizen": None,
        },
    )


def _seed_feed(url: str, markt: list) -> None:
    database.insert_config_row(
        "feeds",
        {
            "rss_feed": url,
            "typ": "RSS-Feed",
            "markt": markt,
            "themenbereich": [],
            "prioritaet": None,
            "sprache": None,
            "notizen": None,
        },
    )


def _seed_outlet(name: str, url: str, markt: list) -> None:
    database.insert_config_row(
        "outlets",
        {
            "outlet": name,
            "url": url,
            "markt": markt,
            "themenbereich": [],
            "prioritaet": "High",
            "sprache": None,
            "outlet_typ": None,
            "hat_rss": False,
            "notizen": None,
        },
    )


def _seed_negative(term: str, markt: list) -> None:
    database.insert_config_row(
        "negative_keywords",
        {"begriff": term, "markt": markt, "kategorie": None, "notizen": None},
    )


def _seed_all() -> None:
    _seed_keyword("italienisch", ["Italy"])
    _seed_keyword("deutsch", ["DACH"])
    _seed_keyword("global-kw", [])
    _seed_keyword("multi", ["Italy", "France"])
    _seed_keyword("sentinel", ["All"])
    _seed_feed("https://it.example.com/rss", ["Italy"])
    _seed_feed("https://de.example.com/rss", ["DACH"])
    _seed_feed("https://global.example.com/rss", [])
    # Distinct registrable domains — extract_domain() reduces hosts to the
    # registered domain, so two subdomains of one domain would collapse.
    _seed_outlet("ItalyOutlet", "https://outlet-italia.it/", ["Italy"])
    _seed_outlet("DachOutlet", "https://outlet-dach.de/", ["DACH"])
    _seed_negative("spam-it", ["Italy"])
    _seed_negative("spam-global", [])
    _seed_negative("spam-de", ["DACH"])


def test_markets_none_returns_everything():
    _seed_all()
    cfg = local_config.load_config(markets=None)
    assert len(cfg.keywords) == 5
    assert len(cfg.feeds) == 3
    assert len(cfg.outlets) == 2
    assert len(cfg.negative_keywords) == 3


def test_empty_markets_list_returns_everything():
    _seed_all()
    cfg = local_config.load_config(markets=[])
    assert len(cfg.keywords) == 5


def test_filter_to_single_market_includes_global_rows():
    _seed_all()
    cfg = local_config.load_config(markets=["Italy"])
    terms = {k.suchbegriff for k in cfg.keywords}
    # Italy rows + empty-markt (global) + "All" sentinel + multi containing Italy
    assert terms == {"italienisch", "global-kw", "multi", "sentinel"}
    feeds = {f.rss_feed for f in cfg.feeds}
    assert feeds == {"https://it.example.com/rss", "https://global.example.com/rss"}
    outlets = {o.outlet for o in cfg.outlets}
    assert outlets == {"ItalyOutlet"}
    negatives = {n.begriff for n in cfg.negative_keywords}
    assert negatives == {"spam-it", "spam-global"}


def test_filter_multiple_markets():
    _seed_all()
    cfg = local_config.load_config(markets=["Italy", "DACH"])
    terms = {k.suchbegriff for k in cfg.keywords}
    assert terms == {"italienisch", "deutsch", "global-kw", "multi", "sentinel"}


def test_filter_no_matching_market_keeps_only_globals():
    _seed_all()
    cfg = local_config.load_config(markets=["Portugal"])
    terms = {k.suchbegriff for k in cfg.keywords}
    assert terms == {"global-kw", "sentinel"}


def test_outlet_domain_sync_stays_global():
    """Trust bonus may apply across markets: the outlet_domains lookup table
    must contain ALL outlets even when config is loaded for one market."""
    _seed_all()
    local_config.load_config(markets=["Italy"])
    assert database.lookup_outlet_domain("outlet-dach.de") is not None
    assert database.lookup_outlet_domain("outlet-italia.it") is not None


def test_markt_applies_helper():
    assert local_config.markt_applies([], ["Italy"]) is True
    assert local_config.markt_applies(["All"], ["Italy"]) is True
    assert local_config.markt_applies(["Alle"], ["Italy"]) is True
    assert local_config.markt_applies(["Italy"], ["Italy"]) is True
    assert local_config.markt_applies(["DACH"], ["Italy"]) is False
    assert local_config.markt_applies(["DACH", "Italy"], ["Italy"]) is True

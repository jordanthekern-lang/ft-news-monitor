"""Market-Edition: load configuration from local SQLite tables.

This module replaces ``notion_sync.load_config`` for the market-edition fork.
The Notion API is not used; instead, keywords / feeds / outlets / negative
keywords live in dedicated SQLite tables that the dashboard's admin pages
write to. Outlet-domain trust scoring is rebuilt on every load so that
``article_domain`` lookups stay in sync with the user's edits.

The public API (``load_config``, returning ``NotionConfig``) is intentionally
identical to the upstream Notion variant — keeping the rest of the pipeline
(``ai_filter``, ``collector``, ``run_collection``) untouched.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from . import database, domain_utils
from .models import (
    FeedSource,
    Keyword,
    NegativeKeyword,
    NotionConfig,
    Outlet,
)

logger = logging.getLogger(__name__)


def _row_to_keyword(row: Dict[str, Any]) -> Keyword:
    return Keyword(
        suchbegriff=row.get("suchbegriff") or "",
        markt=row.get("markt") or [],
        themenbereich=row.get("themenbereich") or [],
        prioritaet=row.get("prioritaet"),
        sprache=row.get("sprache"),
        notizen=row.get("notizen"),
    )


def _row_to_feed(row: Dict[str, Any]) -> FeedSource:
    return FeedSource(
        rss_feed=row.get("rss_feed") or "",
        typ=row.get("typ"),
        markt=row.get("markt") or [],
        themenbereich=row.get("themenbereich") or [],
        prioritaet=row.get("prioritaet"),
        sprache=row.get("sprache"),
        notizen=row.get("notizen"),
    )


def _row_to_outlet(row: Dict[str, Any]) -> Outlet:
    return Outlet(
        outlet=row.get("outlet") or "",
        url=row.get("url"),
        markt=row.get("markt") or [],
        themenbereich=row.get("themenbereich") or [],
        prioritaet=row.get("prioritaet"),
        sprache=row.get("sprache"),
        outlet_typ=row.get("outlet_typ"),
        hat_rss=bool(row.get("hat_rss")),
        notizen=row.get("notizen"),
    )


def _row_to_negative(row: Dict[str, Any]) -> NegativeKeyword:
    return NegativeKeyword(
        begriff=row.get("begriff") or "",
        markt=row.get("markt") or [],
        kategorie=row.get("kategorie"),
        notizen=row.get("notizen"),
    )


def sync_outlet_domains(outlets: List[Outlet]) -> int:
    """Refresh the outlet_domains lookup table used for trust scoring.

    Logic mirrors ``notion_sync.sync_outlet_domains`` so ``ai_filter`` keeps
    awarding the same trust bonus per article domain.
    """
    rows: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for outlet in outlets:
        if not outlet.url:
            continue
        domain = domain_utils.extract_domain(outlet.url)
        if not domain or domain in seen:
            continue
        seen.add(domain)
        market = ", ".join(outlet.markt) if outlet.markt else None
        rows.append(
            {
                "domain": domain,
                "outlet_name": outlet.outlet,
                "priority": outlet.prioritaet,
                "market": market,
                "outlet_type": outlet.outlet_typ,
            }
        )
    database.replace_outlet_domains(rows)
    logger.info("Synced %d outlet domains", len(rows))
    return len(rows)


# Sentinel markt values that mean "applies to all markets". "All" is
# canonical; "Alle" is the legacy German alias (same convention as prefilter).
_GLOBAL_MARKET_SENTINELS = {"All", "Alle"}


def markt_applies(row_markets: List[str], selected: List[str]) -> bool:
    """True when a config row applies to any of the ``selected`` markets.

    Convention everywhere: an empty ``markt`` list = global (applies to all
    markets); the "All"/"Alle" sentinel is global too.
    """
    if not row_markets:
        return True
    if any(m in _GLOBAL_MARKET_SENTINELS for m in row_markets):
        return True
    return any(m in selected for m in row_markets)


def load_config(
    *, force_refresh: bool = False, markets: Optional[List[str]] = None
) -> NotionConfig:
    """Load configuration from local SQLite tables.

    ``markets``: when a non-empty list, keywords/feeds/outlets/negative
    keywords are filtered to rows whose markt list intersects the selection
    OR is empty/"All" (global). ``None`` or ``[]`` → unfiltered.

    The ``force_refresh`` flag is accepted for API compatibility with the
    Notion-based loader but ignored — local DB reads are cheap and never cached.
    """
    del force_refresh  # noqa: F841

    keywords = [_row_to_keyword(r) for r in database.list_config("keywords")]
    feeds = [_row_to_feed(r) for r in database.list_config("feeds")]
    outlets = [_row_to_outlet(r) for r in database.list_config("outlets")]
    negative_keywords = [
        _row_to_negative(r) for r in database.list_config("negative_keywords")
    ]

    # Outlet-domain trust sync stays GLOBAL (before any market filtering):
    # the trust bonus may apply to articles from any market.
    sync_outlet_domains(outlets)

    if markets:
        keywords = [k for k in keywords if markt_applies(k.markt, markets)]
        feeds = [f for f in feeds if markt_applies(f.markt, markets)]
        outlets = [o for o in outlets if markt_applies(o.markt, markets)]
        negative_keywords = [
            n for n in negative_keywords if markt_applies(n.markt, markets)
        ]

    cfg = NotionConfig(
        keywords=keywords,
        feeds=feeds,
        outlets=outlets,
        negative_keywords=negative_keywords,
        loaded_at=datetime.now(timezone.utc),
    )

    logger.info(
        "Loaded local config (markets=%s): %d keywords, %d feeds, %d outlets, %d neg-keywords",
        markets or "all",
        len(cfg.keywords),
        len(cfg.feeds),
        len(cfg.outlets),
        len(cfg.negative_keywords),
    )
    return cfg


def diagnose() -> Dict[str, Any]:
    """Snapshot used by the dashboard's diagnose page."""
    counts = database.count_config_rows()
    return {
        "backend": "local",
        "counts": {
            "keywords": counts.get("keywords", 0),
            "feeds": counts.get("feeds", 0),
            "outlets": counts.get("outlets", 0),
            "negative_keywords": counts.get("negative_keywords", 0),
        },
        "last_loaded_at": datetime.now(timezone.utc).isoformat(),
        "error": None,
    }

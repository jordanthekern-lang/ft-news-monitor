"""Pre-filter stage: cheap eliminations before AI scoring."""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Iterable, List, Sequence

from . import database
from .config import get_settings
from .models import NegativeKeyword, NotionConfig

logger = logging.getLogger(__name__)


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_negkw_index(items: Iterable[NegativeKeyword]) -> dict[str, list[str]]:
    """Return {market_or_GLOBAL: [lowercased terms...]}."""
    idx: dict[str, list[str]] = {"__GLOBAL__": []}
    for nk in items:
        term = (nk.begriff or "").strip().lower()
        if not term:
            continue
        markets = nk.markt or []
        if not markets or "Alle" in markets or "All" in markets:
            # "All" is canonical; "Alle" is the legacy German alias
            idx["__GLOBAL__"].append(term)
        else:
            for m in markets:
                idx.setdefault(m, []).append(term)
    return idx


def _matches_negative(text: str, market: str | None, idx: dict[str, list[str]]) -> str | None:
    """Return the matching term or None."""
    haystack = text.lower()
    for term in idx["__GLOBAL__"]:
        if term in haystack:
            return term
    if market:
        for term in idx.get(market, []):
            if term in haystack:
                return term
    return None


def run(config: NotionConfig) -> dict:
    """Mark articles as filtered out if they're too old or hit a neg-keyword."""
    settings = get_settings()
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.max_age_days)
    neg_idx = _build_negkw_index(config.negative_keywords)

    filtered_age = 0
    filtered_neg = 0

    rows = database.fetch_articles_for_prefilter()
    for row in rows:
        published = _parse_dt(row["published_at"])
        if published and published < cutoff:
            database.mark_filtered(row["id"], reason=f"older than {settings.max_age_days} days")
            filtered_age += 1
            continue

        haystack = " ".join(filter(None, [row["title"], row["snippet"]]))
        match = _matches_negative(haystack, row["country"], neg_idx)
        if match:
            database.mark_filtered(row["id"], reason=f"negative keyword: {match}")
            filtered_neg += 1
            continue

    summary = {
        "candidates": len(rows),
        "filtered_age": filtered_age,
        "filtered_negative_keyword": filtered_neg,
        "passed": len(rows) - filtered_age - filtered_neg,
    }
    logger.info("Pre-filter summary: %s", summary)
    return summary

"""Adaptive scoring based on the user's star/ignore decisions.

Two mechanisms working together:

1. Few-shot examples: the user's most recent starred ("INTERESSANT") and
   ignored ("IRRELEVANT") articles are appended to Claude's prompt so the
   model has concrete reference points for this user's taste.

2. Per-domain bonus: if the user has clearly starred or ignored multiple
   articles from a given domain, a small score adjustment (-2..+2) is
   applied on top of the curated outlet trust bonus.

Both are no-ops until enough feedback exists, so they cannot make the
system worse before there's any signal.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import List, Optional

from . import database, domain_utils

logger = logging.getLogger(__name__)


# Tunables
MAX_FEW_SHOT_PER_KIND = 5
MIN_DOMAIN_VOTES = 3
MAX_FEEDBACK_BONUS = 2
MIN_FEEDBACK_BONUS = -2


@dataclass
class FeedbackExample:
    title: str
    snippet: str
    source: str
    market: Optional[str]
    score: int
    label: str  # "INTERESTING" or "IRRELEVANT"


def _row_to_example(row, label: str) -> FeedbackExample:
    score = (
        (row["relevance_score"] or 0)
        + (row["trust_bonus"] or 0)
        + (row["feedback_bonus"] or 0)
    )
    return FeedbackExample(
        title=(row["title"] or "")[:240],
        snippet=(row["snippet"] or "")[:200],
        source=row["source_name"] or "",
        market=row["country"],
        score=score,
        label=label,
    )


def recent_examples(
    limit_per_kind: int = MAX_FEW_SHOT_PER_KIND,
) -> List[FeedbackExample]:
    """Return recent starred + ignored articles, interleaved for variety."""
    starred_rows = database.fetch_feedback_examples(kind="starred", limit=limit_per_kind)
    ignored_rows = database.fetch_feedback_examples(kind="ignored", limit=limit_per_kind)
    examples: List[FeedbackExample] = []
    for row in starred_rows:
        examples.append(_row_to_example(row, "INTERESTING"))
    for row in ignored_rows:
        examples.append(_row_to_example(row, "IRRELEVANT"))
    return examples


def domain_adjustment(url: str) -> int:
    """Compute a -2..+2 score adjustment based on this domain's star/ignore history.

    Returns 0 until at least MIN_DOMAIN_VOTES votes have been cast for the
    domain, so we never punish or boost based on a single click.
    """
    domain = domain_utils.extract_domain(url)
    if not domain:
        return 0
    stars, ignores = database.domain_feedback_counts(domain)
    total = stars + ignores
    if total < MIN_DOMAIN_VOTES:
        return 0
    ratio = stars / total  # 0.0 (only ignored) … 1.0 (only starred)
    # Map: 0.0 → -2, 0.5 → 0, 1.0 → +2
    raw = round((ratio - 0.5) * 4)
    return max(MIN_FEEDBACK_BONUS, min(MAX_FEEDBACK_BONUS, raw))


def domain_summary(limit: int = 20) -> List[dict]:
    """For the dashboard's diagnose page: per-domain totals plus the adjustment."""
    rows = database.list_domain_feedback(limit=limit)
    out = []
    for r in rows:
        total = (r["stars"] or 0) + (r["ignores"] or 0)
        if total == 0:
            adjustment = 0
        elif total < MIN_DOMAIN_VOTES:
            adjustment = 0
        else:
            ratio = (r["stars"] or 0) / total
            adjustment = max(MIN_FEEDBACK_BONUS, min(MAX_FEEDBACK_BONUS, round((ratio - 0.5) * 4)))
        out.append({**r, "adjustment": adjustment})
    return out

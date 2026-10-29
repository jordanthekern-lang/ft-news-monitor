"""Offline evaluation harness for the AI filter.

Feature 1 (v0.2.0): Golden-Set Backtesting.

Re-scores the user's labeled article set (``golden_labels`` table) against
the *current* prompt + AI config and computes precision/recall/F1 versus
the labels. Never writes to ``articles`` — the eval-set stays clean.

Design contract:
- Pull labeled rows via :func:`database.fetch_labeled_articles_for_eval`.
- Re-score each row by calling :func:`app.ai_filter._score_one` directly.
  This bypasses the pipeline's DB short-circuit (``ai_filtered=0`` filter)
  and pre-screen — the eval measures raw model behaviour, not gating.
- Optional ``no_feedback`` flag empties the few-shot list, so the same
  labeled article can be scored both with and without the user's
  star/ignore signal and compared side-by-side.
- Result is a plain dict; caller decides where to persist it. The CLI in
  ``scripts/run_eval.py`` dumps JSON to ``DATA_DIR/eval_runs/``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import ai_filter, database, feedback, local_config
from .config import get_settings
from .models import AIScore, NotionConfig
from .providers import create_ai_client

logger = logging.getLogger(__name__)


# The threshold above which a scored article counts as a "positive prediction"
# for precision/recall accounting. Matches the dashboard's default min-score.
DEFAULT_MIN_SCORE = 6


@dataclass
class ArticleEvalResult:
    """Per-article eval outcome."""

    article_id: int
    url: str
    market: Optional[str]
    label: str  # "relevant" | "noise"
    score: Optional[int]  # None if the model call failed
    category: Optional[str] = None
    themes: List[str] = field(default_factory=list)
    reasoning: str = ""


def _keywords_hash(config: NotionConfig) -> str:
    """SHA-256 of the sorted keyword list — lets us detect config drift
    between two eval runs. Only ``suchbegriff`` is hashed; priority/market
    metadata change without changing the query surface."""
    phrases = sorted((kw.suchbegriff or "").strip() for kw in config.keywords)
    joined = "\n".join(phrases).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:16]


def compute_metrics(
    results: List[ArticleEvalResult], *, min_score: int = DEFAULT_MIN_SCORE
) -> Dict[str, Any]:
    """Precision / recall / F1 + confusion matrix.

    Ground truth:  label == 'relevant' → positive class.
    Prediction:    score >= min_score  → positive prediction.

    Articles with score=None (model failure) are counted as prediction=None
    and excluded from precision/recall — reported separately as ``errors``.
    """
    tp = fp = fn = tn = 0
    errors = 0
    per_market: Dict[str, Dict[str, int]] = {}

    for r in results:
        bucket = per_market.setdefault(
            r.market or "unknown",
            {"tp": 0, "fp": 0, "fn": 0, "tn": 0, "errors": 0, "total": 0},
        )
        bucket["total"] += 1

        if r.score is None:
            errors += 1
            bucket["errors"] += 1
            continue

        gt_pos = r.label == "relevant"
        pred_pos = r.score >= min_score
        if gt_pos and pred_pos:
            tp += 1
            bucket["tp"] += 1
        elif gt_pos and not pred_pos:
            fn += 1
            bucket["fn"] += 1
        elif not gt_pos and pred_pos:
            fp += 1
            bucket["fp"] += 1
        else:
            tn += 1
            bucket["tn"] += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if (precision + recall)
        else 0.0
    )

    return {
        "total": len(results),
        "errors": errors,
        "min_score_threshold": min_score,
        "confusion": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "f1": round(f1, 4),
        "per_market": per_market,
    }


async def _score_labeled_row(
    client, row, *, model: str, max_tokens: int, sem: asyncio.Semaphore,
    examples: List[feedback.FeedbackExample],
    effort: Optional[str] = None,
) -> Optional[AIScore]:
    return await ai_filter._score_one(
        client,
        model=model,
        max_tokens=max_tokens,
        row=row,
        sem=sem,
        examples=examples,
        effort=effort,
    )


async def run_eval(
    *,
    limit: Optional[int] = None,
    no_feedback: bool = False,
    min_score: int = DEFAULT_MIN_SCORE,
) -> Dict[str, Any]:
    """Re-score all labeled articles and return metrics + per-article results.

    Args:
        limit: cap on number of articles to score (None = all).
        no_feedback: if True, run without few-shot examples (baseline).
        min_score: threshold for the positive-prediction cut.

    Returns a plain dict with ``metrics``, ``articles``, ``model``,
    ``keywords_hash``, ``no_feedback`` — safe to JSON-dump. Does not write
    to the DB.
    """
    settings = get_settings()
    config = local_config.load_config()

    rows = database.fetch_labeled_articles_for_eval()
    if limit is not None and limit >= 0:
        rows = rows[:limit]

    if not rows:
        return {
            "model": settings.anthropic_model,
            "keywords_hash": _keywords_hash(config),
            "no_feedback": no_feedback,
            "min_score_threshold": min_score,
            "metrics": compute_metrics([], min_score=min_score),
            "articles": [],
        }

    try:
        client = create_ai_client(settings)
    except ValueError as exc:
        # AI client couldn't be constructed (missing credentials). Return
        # an all-errors result so the CLI surfaces the issue cleanly.
        logger.error("Eval cannot create AI client: %s", exc)
        results = [
            ArticleEvalResult(
                article_id=row["id"],
                url=row["url"],
                market=row["country"],
                label=row["golden_label"],
                score=None,
            )
            for row in rows
        ]
        return {
            "model": settings.anthropic_model,
            "keywords_hash": _keywords_hash(config),
            "no_feedback": no_feedback,
            "min_score_threshold": min_score,
            "error": f"AI client unavailable: {exc}",
            "metrics": compute_metrics(results, min_score=min_score),
            "articles": [_result_to_dict(r) for r in results],
        }

    examples: List[feedback.FeedbackExample]
    if no_feedback:
        examples = []
    else:
        examples = feedback.recent_examples()

    sem = asyncio.Semaphore(settings.ai_filter_max_parallel)
    tasks = [
        _score_labeled_row(
            client,
            row,
            model=settings.anthropic_model,
            max_tokens=settings.ai_max_tokens,
            sem=sem,
            examples=examples,
            effort=settings.ai_effort or None,
        )
        for row in rows
    ]
    scores = await asyncio.gather(*tasks)

    results: List[ArticleEvalResult] = []
    for row, score in zip(rows, scores):
        results.append(
            ArticleEvalResult(
                article_id=row["id"],
                url=row["url"],
                market=row["country"],
                label=row["golden_label"],
                score=score.relevance_score if score else None,
                category=score.category if score else None,
                themes=score.themes if score else [],
                reasoning=score.reasoning if score else "",
            )
        )

    metrics = compute_metrics(results, min_score=min_score)
    return {
        "model": settings.anthropic_model,
        "keywords_hash": _keywords_hash(config),
        "no_feedback": no_feedback,
        "min_score_threshold": min_score,
        "metrics": metrics,
        "articles": [_result_to_dict(r) for r in results],
    }


def _result_to_dict(r: ArticleEvalResult) -> Dict[str, Any]:
    return {
        "article_id": r.article_id,
        "url": r.url,
        "market": r.market,
        "label": r.label,
        "score": r.score,
        "category": r.category,
        "themes": r.themes,
        "reasoning": r.reasoning,
    }

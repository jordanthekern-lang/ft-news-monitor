"""Tests for the golden-set eval harness (F1)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import database, eval as evalmod
from app.models import AIScore


def _seed_labeled(url: str, *, label: str, country: str = "Italy") -> int:
    aid = database.insert_article(
        url=url,
        title="t",
        source_name="src",
        source_type="rss",
        country=country,
        published_at=datetime.now(timezone.utc),
        snippet="s",
    )
    assert aid is not None
    database.add_golden_label(aid, label)
    return aid


# ---------------------------------------------------------------------------
# compute_metrics
# ---------------------------------------------------------------------------


def _R(article_id, *, label, score, market="Italy"):
    return evalmod.ArticleEvalResult(
        article_id=article_id,
        url=f"https://x/{article_id}",
        market=market,
        label=label,
        score=score,
    )


def test_metrics_perfect_predictor():
    results = [
        _R(1, label="relevant", score=8),
        _R(2, label="relevant", score=9),
        _R(3, label="noise", score=2),
        _R(4, label="noise", score=1),
    ]
    m = evalmod.compute_metrics(results, min_score=6)
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["f1"] == 1.0
    assert m["confusion"] == {"tp": 2, "fp": 0, "fn": 0, "tn": 2}
    assert m["errors"] == 0
    assert m["total"] == 4


def test_metrics_worst_predictor():
    results = [
        _R(1, label="relevant", score=2),
        _R(2, label="relevant", score=1),
        _R(3, label="noise", score=8),
        _R(4, label="noise", score=9),
    ]
    m = evalmod.compute_metrics(results, min_score=6)
    assert m["precision"] == 0.0
    assert m["recall"] == 0.0
    assert m["f1"] == 0.0
    assert m["confusion"] == {"tp": 0, "fp": 2, "fn": 2, "tn": 0}


def test_metrics_mixed_and_errors():
    results = [
        _R(1, label="relevant", score=8),   # TP
        _R(2, label="relevant", score=3),   # FN
        _R(3, label="noise", score=7),      # FP
        _R(4, label="noise", score=1),      # TN
        _R(5, label="relevant", score=None),  # error
    ]
    m = evalmod.compute_metrics(results, min_score=6)
    assert m["confusion"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert m["errors"] == 1
    assert m["total"] == 5
    # precision = 1 / (1+1) = 0.5, recall = 1 / (1+1) = 0.5, F1 = 0.5
    assert m["precision"] == 0.5
    assert m["recall"] == 0.5
    assert m["f1"] == 0.5


def test_metrics_per_market_slice():
    results = [
        _R(1, label="relevant", score=8, market="Italy"),
        _R(2, label="noise", score=8, market="France"),
    ]
    m = evalmod.compute_metrics(results, min_score=6)
    assert m["per_market"]["Italy"]["tp"] == 1
    assert m["per_market"]["France"]["fp"] == 1


def test_metrics_boundary_min_score():
    """Score exactly at min_score counts as a positive prediction (>=)."""
    results = [_R(1, label="relevant", score=6)]
    m = evalmod.compute_metrics(results, min_score=6)
    assert m["confusion"]["tp"] == 1
    assert m["confusion"]["fn"] == 0


# ---------------------------------------------------------------------------
# run_eval — integration path via monkeypatched _score_one
# ---------------------------------------------------------------------------


class _FakeClient:
    """Placeholder — never called; the ai_filter._score_one patch intercepts."""


def test_run_eval_calls_score_one_and_never_writes_articles(monkeypatch):
    a_relevant = _seed_labeled("https://x/rel", label="relevant")
    a_noise = _seed_labeled("https://x/noi", label="noise")

    # Capture reasoning column before eval so we can assert no write happens.
    def _reasonings():
        with database.get_conn() as conn:
            return dict(
                conn.execute(
                    "SELECT id, relevance_reasoning FROM articles"
                ).fetchall()
            )

    before = _reasonings()

    # Patch create_ai_client so we don't need real AWS creds
    monkeypatch.setattr(evalmod, "create_ai_client", lambda settings: _FakeClient())

    async def fake_score(client, *, model, max_tokens, row, sem, examples, effort=None):
        # Give the relevant article a high score, the noise a low one.
        # examples must be empty when no_feedback=True.
        assert isinstance(sem, asyncio.Semaphore)
        if "rel" in row["url"]:
            return AIScore(relevance_score=9, category="Other", themes=[], reasoning="ok")
        return AIScore(relevance_score=1, category="Other", themes=[], reasoning="ok")

    monkeypatch.setattr(evalmod.ai_filter, "_score_one", fake_score)

    result = asyncio.run(evalmod.run_eval(no_feedback=True))
    m = result["metrics"]
    assert m["confusion"] == {"tp": 1, "fp": 0, "fn": 0, "tn": 1}
    assert m["precision"] == 1.0
    assert m["recall"] == 1.0
    assert m["errors"] == 0
    # Regression: no eval write on articles table.
    assert _reasonings() == before


def test_run_eval_uses_feedback_examples_when_enabled(monkeypatch):
    _seed_labeled("https://x/rel", label="relevant")

    seen_examples = []

    monkeypatch.setattr(evalmod, "create_ai_client", lambda settings: _FakeClient())

    async def fake_score(client, *, model, max_tokens, row, sem, examples, effort=None):
        seen_examples.append(list(examples))
        return AIScore(relevance_score=5, category="Other", themes=[], reasoning="")

    monkeypatch.setattr(evalmod.ai_filter, "_score_one", fake_score)

    # Seed a starred article so recent_examples returns something.
    star_id = database.insert_article(
        url="https://star.example",
        title="stellar",
        source_name="src",
        source_type="rss",
        country="Italy",
        published_at=datetime.now(timezone.utc),
        snippet="s",
    )
    database.update_ai_score(
        star_id,
        relevance_score=9, trust_bonus=0, feedback_bonus=0,
        category="Other", themes=[], reasoning="",
    )
    database.toggle_flag(star_id, "starred")

    # 1. with feedback: examples should be non-empty
    result = asyncio.run(evalmod.run_eval(no_feedback=False))
    assert seen_examples[-1]  # non-empty list
    # 2. without feedback: examples must be empty
    seen_examples.clear()
    result = asyncio.run(evalmod.run_eval(no_feedback=True))
    assert seen_examples[-1] == []
    _ = result  # silence unused


def test_run_eval_handles_model_failure_as_error(monkeypatch):
    _seed_labeled("https://x/rel", label="relevant")
    monkeypatch.setattr(evalmod, "create_ai_client", lambda settings: _FakeClient())

    async def fake_score(client, *, model, max_tokens, row, sem, examples, effort=None):
        return None  # simulates Claude returning nothing after retries

    monkeypatch.setattr(evalmod.ai_filter, "_score_one", fake_score)

    result = asyncio.run(evalmod.run_eval())
    assert result["metrics"]["errors"] == 1
    assert result["metrics"]["total"] == 1
    # Precision/recall stay at 0 when everything errored.
    assert result["metrics"]["confusion"] == {"tp": 0, "fp": 0, "fn": 0, "tn": 0}


def test_run_eval_empty_when_no_labels():
    result = asyncio.run(evalmod.run_eval())
    assert result["metrics"]["total"] == 0
    assert result["articles"] == []


def test_run_eval_client_unavailable(monkeypatch):
    _seed_labeled("https://x/rel", label="relevant")

    def _fail(_settings):
        raise ValueError("no AWS creds")

    monkeypatch.setattr(evalmod, "create_ai_client", _fail)
    result = asyncio.run(evalmod.run_eval())
    assert result["metrics"]["errors"] == 1
    assert result["error"].startswith("AI client unavailable")


def test_run_eval_respects_limit(monkeypatch):
    for i in range(5):
        _seed_labeled(f"https://x/{i}", label="relevant" if i % 2 == 0 else "noise")

    monkeypatch.setattr(evalmod, "create_ai_client", lambda settings: _FakeClient())

    async def fake_score(client, *, model, max_tokens, row, sem, examples, effort=None):
        return AIScore(relevance_score=5, category="Other", themes=[], reasoning="")

    monkeypatch.setattr(evalmod.ai_filter, "_score_one", fake_score)

    result = asyncio.run(evalmod.run_eval(limit=2))
    assert result["metrics"]["total"] == 2


def test_keywords_hash_changes_when_keywords_change(monkeypatch):
    from app import local_config
    from app.models import Keyword, NotionConfig

    cfg_a = NotionConfig(keywords=[Keyword(suchbegriff="autobus")])
    cfg_b = NotionConfig(keywords=[Keyword(suchbegriff="autobus"), Keyword(suchbegriff="autocar")])

    assert evalmod._keywords_hash(cfg_a) != evalmod._keywords_hash(cfg_b)
    # Order-independent
    cfg_c = NotionConfig(keywords=[Keyword(suchbegriff="autocar"), Keyword(suchbegriff="autobus")])
    assert evalmod._keywords_hash(cfg_b) == evalmod._keywords_hash(cfg_c)

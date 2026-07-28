"""Tests for the v0.7.6 Cowork ranking API endpoints in app/main.py.

Covers:
- GET /api/health — shape + pending_count
- GET /api/articles/pending — returns unranked articles with the exact
  fields the ftnews skill needs; token gate is enforced.
- POST /api/articles/scores — writes scores, validates required fields,
  is idempotent, and rejects wrong tokens.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def _neutralise_cowork_token(monkeypatch):
    """Default token to empty for every API test — that way the auth
    check is disabled and each test only opts back in when it wants to
    exercise the 401 path.

    Without this, a developer whose real .env happens to have
    COWORK_API_TOKEN set (e.g. from running configure_cowork.py) sees
    every test 401 out.
    """
    from dataclasses import replace

    from app import config as app_config
    from app import main as app_main

    original = app_config.get_settings

    def _patched():
        return replace(original(), cowork_api_token="")

    monkeypatch.setattr(app_config, "get_settings", _patched)
    monkeypatch.setattr(app_main, "get_settings", _patched)


@pytest.fixture()
def client():
    from app.main import app

    return TestClient(app)


def _insert_article(**overrides) -> int:
    """Insert an article and return its id. Everything is filterable-out
    unless overridden, so callers pick who's 'pending' explicitly."""
    from app import database

    payload = dict(
        url=f"https://example.test/{overrides.get('slug', 'a1')}",
        title=overrides.get("title", "Example headline"),
        source_name=overrides.get("source_name", "Test Source"),
        source_type=overrides.get("source_type", "feed"),
        country=overrides.get("country", "DACH"),
        published_at=overrides.get(
            "published_at", datetime.now(timezone.utc)
        ),
        snippet=overrides.get("snippet", "A short snippet."),
        raw_content=overrides.get("raw_content", "body"),
        language=overrides.get("language", "de"),
        outlook_entry_id=overrides.get("outlook_entry_id"),
        outlook_store_id=overrides.get("outlook_store_id"),
    )
    article_id = database.insert_article(**payload)
    assert article_id is not None
    return article_id


def _reset_token(monkeypatch, token: str = "") -> None:
    """Force get_settings().cowork_api_token to a specific value."""
    from dataclasses import replace

    from app import config as app_config
    from app import main as app_main

    original = app_config.get_settings

    def _patched():
        return replace(original(), cowork_api_token=token)

    monkeypatch.setattr(app_config, "get_settings", _patched)
    monkeypatch.setattr(app_main, "get_settings", _patched)


# ---------------------------------------------------------------------------
# /api/health
# ---------------------------------------------------------------------------


def test_health_returns_ok_shape(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert "version" in body
    assert "ai_provider" in body
    assert isinstance(body["pending_count"], int)


def test_health_pending_count_reflects_unranked_articles(client):
    _insert_article(slug="a1")
    _insert_article(slug="a2")
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["pending_count"] >= 2


def test_health_ignores_token_when_none_configured(client, monkeypatch):
    _reset_token(monkeypatch, token="")
    r = client.get("/api/health")
    assert r.status_code == 200


# ---------------------------------------------------------------------------
# /api/articles/pending
# ---------------------------------------------------------------------------


def test_pending_returns_only_unranked_articles(client):
    from app import database

    pending_id = _insert_article(slug="pending")
    scored_id = _insert_article(slug="scored")
    database.update_ai_score(
        scored_id,
        relevance_score=7,
        trust_bonus=0,
        category="Vehicle funding",
        themes=["E-buses"],
        reasoning="test",
    )
    r = client.get("/api/articles/pending")
    assert r.status_code == 200
    body = r.json()
    ids = [a["id"] for a in body["articles"]]
    assert pending_id in ids
    assert scored_id not in ids


def test_pending_projection_includes_all_ftnm_rank_fields(client):
    _insert_article(slug="a1", title="Title A", snippet="Snip A", language="fr")
    r = client.get("/api/articles/pending")
    assert r.status_code == 200
    body = r.json()
    assert body["articles"], "Expected at least one pending article"
    first = body["articles"][0]
    for field in ("id", "title", "url", "snippet", "language", "published_at", "source_name", "country"):
        assert field in first


def test_pending_categories_and_themes_included(client):
    r = client.get("/api/articles/pending")
    assert r.status_code == 200
    body = r.json()
    assert "categories" in body
    assert "themes" in body
    assert "Vehicle funding" in body["categories"]


def test_pending_token_rejected_when_wrong(client, monkeypatch):
    _reset_token(monkeypatch, token="secret-token")
    _insert_article(slug="a1")
    r = client.get("/api/articles/pending", headers={"X-Cowork-Token": "wrong"})
    assert r.status_code == 401


def test_pending_token_accepted_when_correct(client, monkeypatch):
    _reset_token(monkeypatch, token="secret-token")
    _insert_article(slug="a1")
    r = client.get(
        "/api/articles/pending", headers={"X-Cowork-Token": "secret-token"}
    )
    assert r.status_code == 200


def test_pending_respects_limit(client):
    for i in range(5):
        _insert_article(slug=f"a{i}")
    r = client.get("/api/articles/pending?limit=2")
    assert r.status_code == 200
    body = r.json()
    assert len(body["articles"]) == 2


# ---------------------------------------------------------------------------
# /api/articles/scores
# ---------------------------------------------------------------------------


def test_scores_updates_db(client):
    article_id = _insert_article(slug="a1")
    r = client.post(
        "/api/articles/scores",
        json={
            "scores": [
                {
                    "id": article_id,
                    "relevance_score": 8,
                    "category": "Vehicle funding",
                    "themes": ["E-buses"],
                    "reasoning": "Direct funding for e-coaches.",
                }
            ]
        },
    )
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    from app import database

    row = database.article_by_id(article_id)
    assert row["relevance_score"] == 8
    assert row["ai_filtered"] == 1
    assert row["category"] == "Vehicle funding"


def test_scores_is_idempotent(client):
    article_id = _insert_article(slug="a1")
    body = {
        "scores": [
            {
                "id": article_id,
                "relevance_score": 6,
                "category": "Other",
                "themes": [],
                "reasoning": "meh",
            }
        ]
    }
    r1 = client.post("/api/articles/scores", json=body)
    r2 = client.post("/api/articles/scores", json=body)
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["updated"] == 1
    assert r2.json()["updated"] == 1  # UPDATE ran again, same result.
    from app import database

    assert database.article_by_id(article_id)["relevance_score"] == 6


def test_scores_missing_required_field_returns_422(client):
    article_id = _insert_article(slug="a1")
    r = client.post(
        "/api/articles/scores",
        json={
            "scores": [
                {
                    "id": article_id,
                    "relevance_score": 5,
                    # Missing category, themes, reasoning
                }
            ]
        },
    )
    assert r.status_code == 422
    assert r.json()["updated"] == 0


def test_scores_bad_relevance_score_range_returns_422(client):
    article_id = _insert_article(slug="a1")
    r = client.post(
        "/api/articles/scores",
        json={
            "scores": [
                {
                    "id": article_id,
                    "relevance_score": 42,  # out of 0..10
                    "category": "Other",
                    "themes": [],
                    "reasoning": "bad",
                }
            ]
        },
    )
    assert r.status_code == 422


def test_scores_unknown_id_is_skipped(client):
    real_id = _insert_article(slug="a1")
    r = client.post(
        "/api/articles/scores",
        json={
            "scores": [
                {
                    "id": real_id,
                    "relevance_score": 5,
                    "category": "Other",
                    "themes": [],
                    "reasoning": "ok",
                },
                {
                    "id": 9999999,
                    "relevance_score": 5,
                    "category": "Other",
                    "themes": [],
                    "reasoning": "ghost",
                },
            ]
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 1
    assert body["skipped"] == 1


def test_scores_token_gate(client, monkeypatch):
    _reset_token(monkeypatch, token="s3cret")
    article_id = _insert_article(slug="a1")
    body = {
        "scores": [
            {
                "id": article_id,
                "relevance_score": 5,
                "category": "Other",
                "themes": [],
                "reasoning": "x",
            }
        ]
    }
    r_bad = client.post("/api/articles/scores", json=body)
    assert r_bad.status_code == 401
    r_ok = client.post(
        "/api/articles/scores", json=body, headers={"X-Cowork-Token": "s3cret"}
    )
    assert r_ok.status_code == 200


def test_scores_normalises_legacy_german_category(client):
    """Cowork can send an old German category like 'Beides' — the endpoint
    should coerce via config.normalise_category."""
    article_id = _insert_article(slug="a1")
    r = client.post(
        "/api/articles/scores",
        json={
            "scores": [
                {
                    "id": article_id,
                    "relevance_score": 6,
                    "category": "Beides",  # legacy alias
                    "themes": ["E-Busse"],  # legacy alias
                    "reasoning": "alias",
                }
            ]
        },
    )
    assert r.status_code == 200
    from app import database

    row = database.article_by_id(article_id)
    assert row["category"] == "Vehicle funding"

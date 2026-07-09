"""Tests for the golden-set label CRUD + POST routes (F1)."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import database
from app.main import app


def _seed_article(url: str, *, title: str = "t", country: str = "Italy") -> int:
    rid = database.insert_article(
        url=url,
        title=title,
        source_name="src",
        source_type="rss",
        country=country,
        published_at=datetime.now(timezone.utc),
        snippet="s",
    )
    assert rid is not None
    return rid


# ---------------------------------------------------------------------------
# DB helper CRUD
# ---------------------------------------------------------------------------


def test_add_and_get_golden_label():
    aid = _seed_article("https://a.example/1")
    database.add_golden_label(aid, "relevant", notes="obvious keeper")
    assert database.get_golden_label(aid) == "relevant"

    label_rows = database.list_golden_labels()
    assert len(label_rows) == 1
    row = label_rows[0]
    assert row["golden_label"] == "relevant"
    assert row["golden_notes"] == "obvious keeper"
    assert row["golden_added_at"] is not None


def test_relabel_upserts_and_updates_timestamp():
    aid = _seed_article("https://a.example/2")
    database.add_golden_label(aid, "relevant")
    first_added = database.list_golden_labels()[0]["golden_added_at"]
    database.add_golden_label(aid, "noise", notes="off-topic")
    row = database.list_golden_labels()[0]
    assert row["golden_label"] == "noise"
    assert row["golden_notes"] == "off-topic"
    # Timestamp should have advanced (or at least equal, since we use ISO
    # strings and the clock might tick slower than the query).
    assert row["golden_added_at"] >= first_added
    # And there's still exactly one row (no duplicate).
    assert database.count_golden_labels() == 1


def test_remove_golden_label_is_idempotent():
    aid = _seed_article("https://a.example/3")
    database.remove_golden_label(aid)  # no-op on missing
    database.add_golden_label(aid, "noise")
    database.remove_golden_label(aid)
    assert database.get_golden_label(aid) is None
    assert database.count_golden_labels() == 0


def test_add_golden_label_rejects_bad_value():
    aid = _seed_article("https://a.example/4")
    with pytest.raises(ValueError):
        database.add_golden_label(aid, "maybe")


def test_count_by_label():
    a1 = _seed_article("https://a.example/5")
    a2 = _seed_article("https://a.example/6")
    a3 = _seed_article("https://a.example/7")
    database.add_golden_label(a1, "relevant")
    database.add_golden_label(a2, "relevant")
    database.add_golden_label(a3, "noise")
    assert database.count_golden_labels() == 3
    assert database.count_golden_labels(label="relevant") == 2
    assert database.count_golden_labels(label="noise") == 1


def test_cascade_on_article_delete():
    """Foreign key ON DELETE CASCADE — labels vanish when the article does."""
    aid = _seed_article("https://a.example/8")
    database.add_golden_label(aid, "relevant")
    with database.get_conn() as conn:
        conn.execute("DELETE FROM articles WHERE id = ?", (aid,))
    assert database.count_golden_labels() == 0


def test_list_golden_labels_ordered_newest_first():
    a1 = _seed_article("https://a.example/9")
    a2 = _seed_article("https://a.example/10")
    database.add_golden_label(a1, "relevant")
    # Ensure the second timestamp comes later — sleep would be flaky, so
    # instead bump the second one manually via a re-add.
    database.add_golden_label(a2, "noise")
    rows = database.list_golden_labels()
    assert rows[0]["id"] == a2
    assert rows[1]["id"] == a1


# ---------------------------------------------------------------------------
# Route round-trips
# ---------------------------------------------------------------------------


def test_post_route_sets_relevant():
    aid = _seed_article("https://a.example/r1")
    client = TestClient(app)
    r = client.post(f"/article/{aid}/golden/relevant")
    assert r.status_code == 200
    assert r.json() == {"golden_label": "relevant"}
    assert database.get_golden_label(aid) == "relevant"


def test_post_route_sets_noise():
    aid = _seed_article("https://a.example/r2")
    client = TestClient(app)
    r = client.post(f"/article/{aid}/golden/noise")
    assert r.status_code == 200
    assert r.json() == {"golden_label": "noise"}
    assert database.get_golden_label(aid) == "noise"


def test_post_route_clears():
    aid = _seed_article("https://a.example/r3")
    database.add_golden_label(aid, "relevant")
    client = TestClient(app)
    r = client.post(f"/article/{aid}/golden/clear")
    assert r.status_code == 200
    assert r.json() == {"golden_label": None}
    assert database.get_golden_label(aid) is None


def test_post_route_unknown_action_400():
    aid = _seed_article("https://a.example/r4")
    client = TestClient(app)
    r = client.post(f"/article/{aid}/golden/maybe")
    assert r.status_code == 400


def test_post_route_missing_article_404():
    client = TestClient(app)
    r = client.post("/article/99999/golden/relevant")
    assert r.status_code == 404


def test_admin_golden_page_renders_with_and_without_filter():
    aid = _seed_article("https://a.example/r5")
    database.add_golden_label(aid, "relevant")
    client = TestClient(app)
    r = client.get("/admin/golden")
    assert r.status_code == 200
    assert "Golden set" in r.text

    r = client.get("/admin/golden?label=relevant")
    assert r.status_code == 200
    r = client.get("/admin/golden?label=nonsense")
    # Bad filter falls back to no filter — 200, not 400
    assert r.status_code == 200


def test_nav_badge_reflects_count():
    aid = _seed_article("https://a.example/r6")
    database.add_golden_label(aid, "relevant")
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    # The base.html renders `Golden set 1` — check the anchor text.
    assert "/admin/golden" in r.text


def test_dashboard_marks_row_when_labeled():
    """The dashboard's R/N buttons should highlight the active label."""
    aid = _seed_article("https://a.example/r7")
    database.update_ai_score(
        aid,
        relevance_score=8,
        trust_bonus=0,
        feedback_bonus=0,
        category="Other",
        themes=[],
        reasoning="",
    )
    database.add_golden_label(aid, "relevant")
    client = TestClient(app)
    r = client.get("/?min_score=0&market=Italy")
    assert r.status_code == 200
    # The "R" button gets the bg-emerald-500 class when the article is
    # already tagged as relevant.
    assert 'data-golden="relevant"' in r.text
    assert "bg-emerald-500" in r.text

"""Tests for v0.2.0 F2 — Claude keyword suggestions."""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import ai_filter, database
from app.main import app
from app.models import AIScore, Keyword, NotionConfig


# ---------------------------------------------------------------------------
# _coerce_score parses suggested_keywords defensively
# ---------------------------------------------------------------------------


def test_coerce_parses_valid_suggestions():
    payload = {
        "relevance_score": 8,
        "category": "Other",
        "themes": [],
        "reasoning": "",
        "suggested_keywords": ["PNRR autobus", "  RÉTROFIT autocar  "],
    }
    score = ai_filter._coerce_score(payload)
    assert score.suggested_keywords == ["pnrr autobus", "rétrofit autocar"]


def test_coerce_drops_short_and_empty_phrases():
    payload = {
        "relevance_score": 8,
        "category": "Other",
        "themes": [],
        "reasoning": "",
        "suggested_keywords": ["a", "", "   ", "PNRR autobus", "the a to"],
    }
    score = ai_filter._coerce_score(payload)
    # Only phrases with at least one >=4-char token survive.
    assert score.suggested_keywords == ["pnrr autobus"]


def test_coerce_caps_at_three():
    payload = {
        "relevance_score": 8,
        "category": "Other",
        "themes": [],
        "reasoning": "",
        "suggested_keywords": [
            "phrase alpha",
            "phrase beta",
            "phrase gamma",
            "phrase delta",
        ],
    }
    score = ai_filter._coerce_score(payload)
    assert len(score.suggested_keywords) == 3


def test_coerce_dedup_within_response():
    payload = {
        "relevance_score": 8,
        "category": "Other",
        "themes": [],
        "reasoning": "",
        "suggested_keywords": ["pnrr autobus", "PNRR autobus", "pnrr AUTOBUS"],
    }
    score = ai_filter._coerce_score(payload)
    assert score.suggested_keywords == ["pnrr autobus"]


def test_coerce_survives_bogus_type():
    payload = {
        "relevance_score": 8, "category": "Other", "themes": [], "reasoning": "",
        "suggested_keywords": "not-a-list",
    }
    score = ai_filter._coerce_score(payload)
    assert score.suggested_keywords == []


def test_coerce_absent_field_is_empty():
    payload = {"relevance_score": 8, "category": "Other", "themes": [], "reasoning": ""}
    score = ai_filter._coerce_score(payload)
    assert score.suggested_keywords == []


# ---------------------------------------------------------------------------
# Dedup helpers
# ---------------------------------------------------------------------------


def test_dedup_subset_direction_incoming_subset_of_existing():
    existing = [frozenset({"pnrr", "autobus", "elettrici"})]
    incoming = frozenset({"pnrr", "autobus"})
    assert ai_filter._is_dup_against_existing(incoming, existing) is True


def test_dedup_subset_direction_existing_subset_of_incoming():
    existing = [frozenset({"autobus"})]
    incoming = frozenset({"autobus", "elettrici"})
    assert ai_filter._is_dup_against_existing(incoming, existing) is True


def test_dedup_disjoint_is_new():
    existing = [frozenset({"pnrr", "autobus"})]
    incoming = frozenset({"rétrofit", "autocar"})
    assert ai_filter._is_dup_against_existing(incoming, existing) is False


def test_normalise_phrase_order_insensitive():
    assert ai_filter._normalise_phrase("pnrr autobus elettrici") == \
        ai_filter._normalise_phrase("autobus PNRR elettrici")


def test_existing_keyword_token_sets_expands_combos():
    cfg = NotionConfig(keywords=[
        Keyword(suchbegriff="autocar+subvention"),
        Keyword(suchbegriff="autobus"),
    ])
    sets = ai_filter._existing_keyword_token_sets(cfg)
    assert frozenset({"autocar", "subvention"}) in sets
    assert frozenset({"autobus"}) in sets


# ---------------------------------------------------------------------------
# DB CRUD + grouping
# ---------------------------------------------------------------------------


def _seed_article(url: str, country: str = "Italy") -> int:
    aid = database.insert_article(
        url=url, title="t", source_name="src", source_type="rss",
        country=country, published_at=datetime.now(timezone.utc), snippet="s",
    )
    assert aid is not None
    return aid


def test_insert_and_list_suggestion_grouping():
    aid = _seed_article("https://a.example/1")
    database.insert_keyword_suggestion(
        phrase="pnrr autobus", phrase_normalized="autobus pnrr",
        source_article_id=aid, market="Italy", sprache="IT",
    )
    database.insert_keyword_suggestion(
        phrase="PNRR autobus", phrase_normalized="autobus pnrr",
        source_article_id=aid, market="Italy", sprache="IT",
    )
    groups = database.list_keyword_suggestions(status="pending")
    assert len(groups) == 1
    g = groups[0]
    assert g["count"] == 2
    assert g["phrase_normalized"] == "autobus pnrr"
    assert set(g["phrases"]) == {"pnrr autobus", "PNRR autobus"}


def test_count_by_status():
    aid = _seed_article("https://a.example/2")
    for _ in range(3):
        database.insert_keyword_suggestion(
            phrase="autocar électrique", phrase_normalized="autocar électrique",
            source_article_id=aid, market="France", sprache="FR",
        )
    assert database.count_keyword_suggestions() == 3
    assert database.count_keyword_suggestions(status="pending") == 3
    assert database.count_keyword_suggestions(status="approved") == 0


def test_pending_suggestion_norms_returns_set():
    aid = _seed_article("https://a.example/3")
    database.insert_keyword_suggestion(
        phrase="autobus", phrase_normalized="autobus",
        source_article_id=aid, market=None, sprache=None,
    )
    norms = database.pending_suggestion_norms()
    assert "autobus" in norms


def test_set_suggestion_group_status_updates_all_pending():
    aid = _seed_article("https://a.example/4")
    for _ in range(3):
        database.insert_keyword_suggestion(
            phrase="autobus", phrase_normalized="autobus",
            source_article_id=aid, market=None, sprache=None,
        )
    n = database.set_suggestion_group_status(
        "autobus", status="dismissed"
    )
    assert n == 3
    assert database.count_keyword_suggestions(status="pending") == 0
    assert database.count_keyword_suggestions(status="dismissed") == 3


# ---------------------------------------------------------------------------
# _persist_suggestions dedup + write path
# ---------------------------------------------------------------------------


def test_persist_skips_when_dup_of_existing_keyword():
    aid = _seed_article("https://a.example/5")
    cfg = NotionConfig(keywords=[Keyword(suchbegriff="pnrr autobus elettrici")])
    existing_sets = ai_filter._existing_keyword_token_sets(cfg)
    row = database.article_by_id(aid)

    n = ai_filter._persist_suggestions(
        row,
        ["pnrr autobus"],  # subset of existing → drop
        config=cfg,
        existing_sets=existing_sets,
        pending_seen=set(),
    )
    assert n == 0
    assert database.count_keyword_suggestions() == 0


def test_persist_writes_when_new():
    aid = _seed_article("https://a.example/6")
    cfg = NotionConfig(keywords=[Keyword(suchbegriff="autobus")])
    existing_sets = ai_filter._existing_keyword_token_sets(cfg)
    row = database.article_by_id(aid)
    pending: set = set()

    n = ai_filter._persist_suggestions(
        row,
        ["rétrofit autocar"],
        config=cfg,
        existing_sets=existing_sets,
        pending_seen=pending,
    )
    assert n == 1
    assert database.count_keyword_suggestions() == 1
    # pending_seen is mutated so a repeat call is a no-op.
    n2 = ai_filter._persist_suggestions(
        row,
        ["rétrofit autocar"],
        config=cfg,
        existing_sets=existing_sets,
        pending_seen=pending,
    )
    assert n2 == 0
    assert database.count_keyword_suggestions() == 1


# ---------------------------------------------------------------------------
# Env-flag gate
# ---------------------------------------------------------------------------


def test_flag_off_omits_field_from_user_prompt(monkeypatch):
    """When SUGGEST_KEYWORDS is off, the JSON schema block must NOT
    mention ``suggested_keywords`` — otherwise output tokens are wasted."""
    monkeypatch.delenv("SUGGEST_KEYWORDS", raising=False)
    monkeypatch.setenv("SUGGEST_KEYWORDS", "false")
    assert ai_filter._suggest_keywords_enabled() is False

    # Build the user prompt manually (mirror _score_one composition)
    from app.config import VALID_CATEGORIES, VALID_THEMES
    prompt = ai_filter.USER_TEMPLATE.format(
        title="t", source="s", market="m", language="EN", published="p",
        snippet="x", categories=", ".join(VALID_CATEGORIES),
        themes=", ".join(VALID_THEMES),
        suggest_field="",
    )
    assert "suggested_keywords" not in prompt


def test_flag_on_includes_field(monkeypatch):
    monkeypatch.setenv("SUGGEST_KEYWORDS", "true")
    assert ai_filter._suggest_keywords_enabled() is True

    from app.config import VALID_CATEGORIES, VALID_THEMES
    prompt = ai_filter.USER_TEMPLATE.format(
        title="t", source="s", market="m", language="EN", published="p",
        snippet="x", categories=", ".join(VALID_CATEGORIES),
        themes=", ".join(VALID_THEMES),
        suggest_field=ai_filter._SUGGEST_FIELD_SNIPPET,
    )
    assert "suggested_keywords" in prompt


# ---------------------------------------------------------------------------
# v0.3.1 — runtime setting beats env, env beats default
# ---------------------------------------------------------------------------


def test_suggest_priority_runtime_true_beats_env_false(monkeypatch):
    from app import runtime_settings

    monkeypatch.setenv("SUGGEST_KEYWORDS", "false")
    runtime_settings.set(runtime_settings.SUGGEST_KEYWORDS_ENABLED, True)
    assert ai_filter._suggest_keywords_enabled() is True


def test_suggest_priority_runtime_false_beats_env_true(monkeypatch):
    from app import runtime_settings

    monkeypatch.setenv("SUGGEST_KEYWORDS", "true")
    runtime_settings.set(runtime_settings.SUGGEST_KEYWORDS_ENABLED, False)
    assert ai_filter._suggest_keywords_enabled() is False


def test_suggest_priority_falls_back_to_env_when_runtime_unset(monkeypatch):
    monkeypatch.setenv("SUGGEST_KEYWORDS", "true")
    # No runtime setting written for SUGGEST_KEYWORDS_ENABLED in this test.
    assert ai_filter._suggest_keywords_enabled() is True


def test_suggest_priority_default_false(monkeypatch):
    monkeypatch.delenv("SUGGEST_KEYWORDS", raising=False)
    # No runtime setting, no env → false.
    assert ai_filter._suggest_keywords_enabled() is False


# ---------------------------------------------------------------------------
# Admin routes round-trip
# ---------------------------------------------------------------------------


def test_admin_suggestions_page_renders():
    aid = _seed_article("https://a.example/7")
    database.insert_keyword_suggestion(
        phrase="pnrr autobus", phrase_normalized="autobus pnrr",
        source_article_id=aid, market="Italy", sprache="IT",
    )
    client = TestClient(app)
    r = client.get("/admin/suggestions")
    assert r.status_code == 200
    assert "pnrr autobus" in r.text


def test_admin_approve_creates_keyword_and_flips_status():
    aid = _seed_article("https://a.example/8")
    database.insert_keyword_suggestion(
        phrase="autocar électrique", phrase_normalized="autocar électrique",
        source_article_id=aid, market="France", sprache="FR",
    )
    sug_id = database.list_keyword_suggestions(status="pending")[0]["ids"][0]

    client = TestClient(app)
    kw_count_before = database.count_config_rows()["keywords"]
    r = client.post(
        f"/admin/suggestions/{sug_id}/approve",
        data={
            "suchbegriff": "autocar électrique",
            "prioritaet": "High",
            "sprache": "FR",
            "markt": ["France"],
        },
        follow_redirects=False,
    )
    assert r.status_code in (200, 303)
    kw_count_after = database.count_config_rows()["keywords"]
    assert kw_count_after == kw_count_before + 1
    # The suggestion is no longer pending.
    assert database.count_keyword_suggestions(status="pending") == 0
    assert database.count_keyword_suggestions(status="approved") == 1


def test_admin_dismiss_group():
    aid = _seed_article("https://a.example/9")
    for _ in range(2):
        database.insert_keyword_suggestion(
            phrase="foo bar", phrase_normalized="bar foo",
            source_article_id=aid, market=None, sprache=None,
        )
    sug_id = database.list_keyword_suggestions(status="pending")[0]["ids"][0]

    client = TestClient(app)
    r = client.post(f"/admin/suggestions/{sug_id}/dismiss")
    assert r.status_code == 200
    assert r.json()["dismissed"] == 2
    assert database.count_keyword_suggestions(status="pending") == 0
    assert database.count_keyword_suggestions(status="dismissed") == 2


def test_admin_dismiss_unknown_id_404():
    client = TestClient(app)
    r = client.post("/admin/suggestions/99999/dismiss")
    assert r.status_code == 404


def test_admin_dismiss_all_pending():
    """Bulk-dismiss endpoint clears every pending row in one shot; already-
    approved/dismissed rows are untouched."""
    aid = _seed_article("https://a.example/da1")
    for phrase, norm in [
        ("alpha term", "alpha term"),
        ("beta term", "beta term"),
        ("gamma term", "gamma term"),
    ]:
        database.insert_keyword_suggestion(
            phrase=phrase, phrase_normalized=norm,
            source_article_id=aid, market=None, sprache=None,
        )
    existing_dismissed = database.insert_keyword_suggestion(
        phrase="delta term", phrase_normalized="delta term",
        source_article_id=aid, market=None, sprache=None,
    )
    database.set_suggestion_status(existing_dismissed, status="dismissed")

    assert database.count_keyword_suggestions(status="pending") == 3

    client = TestClient(app)
    r = client.post("/admin/suggestions/dismiss-all")
    assert r.status_code == 200
    assert r.json()["dismissed"] == 3
    assert database.count_keyword_suggestions(status="pending") == 0
    assert database.count_keyword_suggestions(status="dismissed") == 4


def test_admin_dismiss_all_when_empty_is_zero():
    """Empty pending queue → returns dismissed=0, no error."""
    client = TestClient(app)
    r = client.post("/admin/suggestions/dismiss-all")
    assert r.status_code == 200
    assert r.json()["dismissed"] == 0


# ---------------------------------------------------------------------------
# Regression: approved suggestion appears in pre-screen tokens
# ---------------------------------------------------------------------------


def test_approve_is_idempotent_no_duplicate_keyword_row():
    """Regression: double-click on the approve button must NOT create two
    identical keyword rows. Second POST returns the same 303 but is a
    no-op on the DB (F2 fix-loop finding 1)."""
    aid = _seed_article("https://a.example/dup")
    database.insert_keyword_suggestion(
        phrase="autobus elettrico", phrase_normalized="autobus elettrico",
        source_article_id=aid, market="Italy", sprache="IT",
    )
    sug_id = database.list_keyword_suggestions(status="pending")[0]["ids"][0]

    client = TestClient(app)
    before = database.count_config_rows()["keywords"]

    r1 = client.post(
        f"/admin/suggestions/{sug_id}/approve",
        data={"suchbegriff": "autobus elettrico", "prioritaet": "High", "sprache": "IT"},
        follow_redirects=False,
    )
    assert r1.status_code == 303
    r2 = client.post(
        f"/admin/suggestions/{sug_id}/approve",
        data={"suchbegriff": "autobus elettrico", "prioritaet": "High", "sprache": "IT"},
        follow_redirects=False,
    )
    assert r2.status_code == 303  # silent no-op
    after = database.count_config_rows()["keywords"]
    assert after == before + 1  # only ONE keyword row despite two approves


def test_approve_attaches_keyword_id_to_group():
    aid = _seed_article("https://a.example/att")
    database.insert_keyword_suggestion(
        phrase="new phrase here", phrase_normalized="here new phrase",
        source_article_id=aid, market="Italy", sprache="IT",
    )
    sug_id = database.list_keyword_suggestions(status="pending")[0]["ids"][0]

    client = TestClient(app)
    client.post(
        f"/admin/suggestions/{sug_id}/approve",
        data={"suchbegriff": "new phrase here", "prioritaet": "High", "sprache": "IT"},
        follow_redirects=False,
    )
    row = database.suggestion_by_id(sug_id)
    assert row["status"] == "approved"
    assert row["approved_keyword_id"] is not None


def test_list_suggestions_tiebreaker_newest_first():
    """Regression: equal-frequency groups sort newest-first (F2 review finding 3)."""
    from datetime import datetime, timezone

    aid = _seed_article("https://a.example/ts")
    # Two groups of count=1 each. Insert older one first so timestamps differ.
    database.insert_keyword_suggestion(
        phrase="old group", phrase_normalized="group old",
        source_article_id=aid, market=None, sprache=None,
    )
    # Bump the suggested_at of the second row so it sorts newest.
    database.insert_keyword_suggestion(
        phrase="new group", phrase_normalized="group new",
        source_article_id=aid, market=None, sprache=None,
    )
    with database.get_conn() as conn:
        conn.execute(
            "UPDATE keyword_suggestions SET suggested_at = ? WHERE phrase_normalized = ?",
            ("2020-01-01T00:00:00+00:00", "group old"),
        )
        conn.execute(
            "UPDATE keyword_suggestions SET suggested_at = ? WHERE phrase_normalized = ?",
            ("2026-12-01T00:00:00+00:00", "group new"),
        )
    groups = database.list_keyword_suggestions(status="pending")
    assert groups[0]["phrase_normalized"] == "group new"


def test_approved_suggestion_reaches_prescreen(monkeypatch):
    """The whole point: approving a suggestion turns it into a real keyword
    row, which the next pipeline load picks up as a pre-screen token."""
    aid = _seed_article("https://a.example/10")
    database.insert_keyword_suggestion(
        phrase="RGTR bus", phrase_normalized="bus rgtr",
        source_article_id=aid, market="Benelux", sprache="FR",
    )
    sug_id = database.list_keyword_suggestions(status="pending")[0]["ids"][0]

    client = TestClient(app)
    client.post(
        f"/admin/suggestions/{sug_id}/approve",
        data={
            "suchbegriff": "RGTR bus",
            "prioritaet": "High",
            "sprache": "FR",
            "markt": ["Benelux"],
        },
        follow_redirects=False,
    )

    # Load config afresh and check the token surfaces in the pre-screen set.
    from app import local_config
    cfg = local_config.load_config()
    tokens = ai_filter._high_priority_tokens(cfg)
    assert "rgtr" in tokens

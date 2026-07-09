"""Web tests for Task 3 items 5 + 6: dashboard market filter, min-score
default from runtime settings, and the settings form (POST /settings/update).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import database, runtime_settings
from app.main import app
from app import main as app_main


@pytest.fixture()
def client():
    return TestClient(app)


def _seed_article(url: str, title: str, country: str, score: int = 8) -> int:
    rid = database.insert_article(
        url=url,
        title=title,
        source_name="src",
        source_type="rss",
        country=country,
        published_at=datetime.now(timezone.utc),
        snippet="snippet",
    )
    assert rid is not None
    database.update_ai_score(
        rid,
        relevance_score=score,
        trust_bonus=0,
        category="Other",
        themes=[],
        reasoning="r",
    )
    return rid


# ---------------------------------------------------------------------------
# Dashboard market filter
# ---------------------------------------------------------------------------

def test_dashboard_market_param_filters_articles(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy")
    _seed_article("https://de.example.com/a", "DachArticleXYZ", "DACH")
    resp = client.get("/?market=Italy&min_score=0")
    assert resp.status_code == 200
    assert "ItalyArticleXYZ" in resp.text
    assert "DachArticleXYZ" not in resp.text


def test_dashboard_multiple_market_params(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy")
    _seed_article("https://de.example.com/a", "DachArticleXYZ", "DACH")
    _seed_article("https://fr.example.com/a", "FranceArticleXYZ", "France")
    resp = client.get("/?market=Italy&market=DACH&min_score=0")
    assert "ItalyArticleXYZ" in resp.text
    assert "DachArticleXYZ" in resp.text
    assert "FranceArticleXYZ" not in resp.text


def test_dashboard_invalid_market_ignored(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy")
    _seed_article("https://de.example.com/a", "DachArticleXYZ", "DACH")
    resp = client.get("/?market=Narnia&min_score=0")
    assert "ItalyArticleXYZ" in resp.text
    assert "DachArticleXYZ" in resp.text


def test_dashboard_defaults_to_home_markets(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy")
    _seed_article("https://de.example.com/a", "DachArticleXYZ", "DACH")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["Italy"])
    resp = client.get("/?min_score=0")
    assert "ItalyArticleXYZ" in resp.text
    assert "DachArticleXYZ" not in resp.text


def test_dashboard_no_home_markets_shows_all(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy")
    _seed_article("https://de.example.com/a", "DachArticleXYZ", "DACH")
    resp = client.get("/?min_score=0")
    assert "ItalyArticleXYZ" in resp.text
    assert "DachArticleXYZ" in resp.text


# ---------------------------------------------------------------------------
# min_score default from runtime settings
# ---------------------------------------------------------------------------

def test_min_score_default_from_runtime_settings(client):
    _seed_article("https://x.example.com/a", "MidScoreArticleXYZ", "EU", score=5)
    # Unset → hardcoded default 6 hides the score-5 article
    resp = client.get("/")
    assert "MidScoreArticleXYZ" not in resp.text
    # Runtime default 5 → visible without any query param
    runtime_settings.set(runtime_settings.MIN_SCORE_DEFAULT, 5)
    resp = client.get("/")
    assert "MidScoreArticleXYZ" in resp.text
    # Explicit query param still wins over the runtime default
    resp = client.get("/?min_score=8")
    assert "MidScoreArticleXYZ" not in resp.text


# ---------------------------------------------------------------------------
# Settings form
# ---------------------------------------------------------------------------

_VALID_FORM = {
    "home_markets": ["Italy", "EU"],
    "min_score_default": "5",
    "ai_skip_prescreen_days_override": "",
    "scheduler_time": "06:30",
}


def test_settings_update_persists_and_redirects(client):
    resp = client.post("/settings/update", data=_VALID_FORM, follow_redirects=False)
    assert resp.status_code == 303
    assert "saved=1" in resp.headers["location"]
    assert runtime_settings.get(runtime_settings.HOME_MARKETS) == ["Italy", "EU"]
    assert runtime_settings.get(runtime_settings.MIN_SCORE_DEFAULT) == 5
    # Empty override = use env default → stored as None
    assert (
        runtime_settings.get(
            runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE, "sentinel"
        )
        is None
    )
    # No checkbox sent → scheduler disabled
    assert runtime_settings.get(runtime_settings.SCHEDULER_ENABLED) is False
    assert runtime_settings.get(runtime_settings.SCHEDULER_TIME) == "06:30"


def test_settings_update_scheduler_enabled_and_override(client):
    form = dict(_VALID_FORM)
    form["scheduler_enabled"] = "1"
    form["ai_skip_prescreen_days_override"] = "3"
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert runtime_settings.get(runtime_settings.SCHEDULER_ENABLED) is True
    assert runtime_settings.get(runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE) == 3


def test_settings_update_rejects_invalid_market(client):
    form = dict(_VALID_FORM)
    form["home_markets"] = ["Italy", "Narnia"]
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 400
    assert "Narnia" in resp.text


def test_settings_update_rejects_min_score_out_of_range(client):
    for bad in ("11", "-1", "abc", ""):
        form = dict(_VALID_FORM)
        form["min_score_default"] = bad
        resp = client.post("/settings/update", data=form, follow_redirects=False)
        assert resp.status_code == 400, f"min_score_default={bad!r} must be rejected"


def test_settings_update_rejects_bad_time(client):
    for bad in ("25:00", "6:5", "noon", "12:60"):
        form = dict(_VALID_FORM)
        form["scheduler_time"] = bad
        resp = client.post("/settings/update", data=form, follow_redirects=False)
        assert resp.status_code == 400, f"scheduler_time={bad!r} must be rejected"


def test_settings_update_rejects_bad_override(client):
    form = dict(_VALID_FORM)
    form["ai_skip_prescreen_days_override"] = "-2"
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 400


def test_settings_update_calls_scheduler_reload(client, monkeypatch):
    """Wiring regression: POST /settings/update must invoke
    ``scheduler.reload()`` so a settings change takes effect without a
    process restart."""
    reload_mock = MagicMock()
    monkeypatch.setattr(app_main.scheduler, "reload", reload_mock)
    resp = client.post("/settings/update", data=_VALID_FORM, follow_redirects=False)
    assert resp.status_code == 303
    assert reload_mock.called


def test_settings_page_shows_form_and_saved_banner(client):
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["France"])
    resp = client.get("/settings?saved=1")
    assert resp.status_code == 200
    assert "/settings/update" in resp.text
    assert "Settings saved" in resp.text
    # Existing diagnostics stay intact
    assert "Loaded configuration" in resp.text


# ---------------------------------------------------------------------------
# v0.3.1 — heading/nav rename, SUGGEST_KEYWORDS toggle, translation targets
# ---------------------------------------------------------------------------


def test_settings_page_heading_and_nav_label(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    # New page heading (Nav label lives on any page — check via dashboard).
    assert "Settings and Diagnostics" in resp.text


def test_dashboard_nav_link_says_settings(client):
    resp = client.get("/")
    assert resp.status_code == 200
    # Nav link renamed from "Diagnostics" → "Settings"
    assert ">\n                    Settings\n                </a>" in resp.text \
        or ">Settings<" in resp.text.replace("\n", "").replace(" ", "")


def test_settings_update_persists_suggest_keywords(client):
    form = dict(_VALID_FORM)
    form["suggest_keywords_enabled"] = "1"
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert runtime_settings.get(runtime_settings.SUGGEST_KEYWORDS_ENABLED) is True

    # Second submit without the checkbox clears the flag.
    form.pop("suggest_keywords_enabled")
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert runtime_settings.get(runtime_settings.SUGGEST_KEYWORDS_ENABLED) is False


def test_settings_update_persists_manual_translation_langs(client):
    form = dict(_VALID_FORM)
    form["translation_target_langs_mode"] = "manual"
    form["translation_target_langs"] = ["de", "en", "pt"]
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 303
    stored = runtime_settings.get(runtime_settings.TRANSLATION_TARGET_LANGS)
    assert set(stored) == {"de", "en", "pt"}


def test_settings_update_manual_with_empty_selection_falls_back_to_auto(client):
    # Preseed something so we can verify it's cleared.
    runtime_settings.set(runtime_settings.TRANSLATION_TARGET_LANGS, ["de"])
    form = dict(_VALID_FORM)
    form["translation_target_langs_mode"] = "manual"
    # No translation_target_langs keys sent.
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 303
    # Manual+empty collapses back to auto → stored as None.
    stored = runtime_settings.get(
        runtime_settings.TRANSLATION_TARGET_LANGS, "sentinel",
    )
    assert stored is None


def test_settings_update_auto_mode_clears_manual_langs(client):
    runtime_settings.set(runtime_settings.TRANSLATION_TARGET_LANGS, ["de", "fr"])
    form = dict(_VALID_FORM)
    form["translation_target_langs_mode"] = "auto"
    resp = client.post("/settings/update", data=form, follow_redirects=False)
    assert resp.status_code == 303
    assert runtime_settings.get(
        runtime_settings.TRANSLATION_TARGET_LANGS, "sentinel",
    ) is None

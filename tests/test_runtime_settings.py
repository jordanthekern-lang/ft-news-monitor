"""Tests for app.runtime_settings — JSON-encoded user settings in app_settings.

Storage contract (Task 3, item 1):
  - get(key, default=None) / set(key, value), values JSON-encoded in SQLite.
  - Known keys documented as module constants (home_markets, min_score_default,
    ai_skip_prescreen_days_override, scheduler_enabled, scheduler_time,
    last_run_at, last_run_summary, setup_completed).
  - home_markets unset or [] means "all markets".
"""

from __future__ import annotations

from app import runtime_settings


def test_get_returns_default_when_unset():
    assert runtime_settings.get("home_markets") is None
    assert runtime_settings.get("home_markets", ["DACH"]) == ["DACH"]
    assert runtime_settings.get("min_score_default", 6) == 6


def test_set_get_roundtrip_list():
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["Italy", "EU"])
    assert runtime_settings.get(runtime_settings.HOME_MARKETS) == ["Italy", "EU"]


def test_set_get_roundtrip_bool():
    runtime_settings.set(runtime_settings.SETUP_COMPLETED, True)
    assert runtime_settings.get(runtime_settings.SETUP_COMPLETED) is True
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, False)
    assert runtime_settings.get(runtime_settings.SCHEDULER_ENABLED) is False


def test_set_get_roundtrip_int_and_string():
    runtime_settings.set(runtime_settings.MIN_SCORE_DEFAULT, 5)
    assert runtime_settings.get(runtime_settings.MIN_SCORE_DEFAULT) == 5
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:30")
    assert runtime_settings.get(runtime_settings.SCHEDULER_TIME) == "06:30"


def test_set_none_is_stored_explicitly():
    # None must roundtrip (override key: None = "use env default")
    runtime_settings.set(runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE, None)
    assert (
        runtime_settings.get(runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE, "sentinel")
        is None
    )


def test_overwrite_updates_value():
    runtime_settings.set(runtime_settings.MIN_SCORE_DEFAULT, 4)
    runtime_settings.set(runtime_settings.MIN_SCORE_DEFAULT, 7)
    assert runtime_settings.get(runtime_settings.MIN_SCORE_DEFAULT) == 7


def test_dict_roundtrip_last_run_summary():
    summary = {
        "status": "completed",
        "markets": ["Italy"],
        "collector": {"total_new": 3},
        "duration_seconds": 1.5,
    }
    runtime_settings.set(runtime_settings.LAST_RUN_SUMMARY, summary)
    assert runtime_settings.get(runtime_settings.LAST_RUN_SUMMARY) == summary


def test_known_key_constants_match_contract():
    """The setup wizard (later task) depends on these exact key names."""
    assert runtime_settings.HOME_MARKETS == "home_markets"
    assert runtime_settings.MIN_SCORE_DEFAULT == "min_score_default"
    assert (
        runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE
        == "ai_skip_prescreen_days_override"
    )
    assert runtime_settings.SCHEDULER_ENABLED == "scheduler_enabled"
    assert runtime_settings.SCHEDULER_TIME == "scheduler_time"
    assert runtime_settings.LAST_RUN_AT == "last_run_at"
    assert runtime_settings.LAST_RUN_SUMMARY == "last_run_summary"
    assert runtime_settings.SETUP_COMPLETED == "setup_completed"

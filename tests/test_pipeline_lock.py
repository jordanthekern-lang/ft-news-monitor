"""Tests for app.pipeline (Task 3, item 3): single entrypoint, run lock,
market resolution, prescreen-days override plumbing, CLI --markets parsing.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import pipeline, runtime_settings
from app.models import NotionConfig


@pytest.fixture(autouse=True)
def fresh_lock():
    """asyncio primitives bind to the first loop that awaits them contended —
    tests each run in their own asyncio.run() loop, so reset the module lock."""
    pipeline._run_lock = asyncio.Lock()
    yield
    pipeline._run_lock = asyncio.Lock()


class _FakeStats:
    total_new = 2
    errors = 0

    def as_dict(self):
        return {"total_new": 2, "errors": 0}


def _patch_stages(monkeypatch, record: dict, *, collector_delay: float = 0.0):
    """Replace all pipeline stages with fast no-ops that record their inputs."""

    def fake_load_config(*, force_refresh: bool = False, markets=None):
        record["load_config_markets"] = markets
        return NotionConfig()

    async def fake_collector(cfg, *, time_window_override=None):
        record["collector_time_window"] = time_window_override
        if collector_delay:
            await asyncio.sleep(collector_delay)
        return _FakeStats()

    async def fake_ai(cfg, *, skip_prescreen_days=None):
        record["skip_prescreen_days"] = skip_prescreen_days
        return {"sent_to_ai": 0, "skipped_pre_screen": 0, "scored": 1}

    monkeypatch.setattr(pipeline.local_config, "load_config", fake_load_config)
    monkeypatch.setattr(pipeline.collector, "run", fake_collector)
    monkeypatch.setattr(pipeline.prefilter, "run", lambda cfg: {"candidates": 0})
    monkeypatch.setattr(pipeline.translator, "run", lambda: {"translated": 0})
    monkeypatch.setattr(pipeline.ai_filter, "run", fake_ai)


# ---------------------------------------------------------------------------
# Lock behaviour
# ---------------------------------------------------------------------------

def test_concurrent_second_run_returns_already_running(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record, collector_delay=0.1)

    async def scenario():
        first, second = await asyncio.gather(
            pipeline.run_pipeline(), pipeline.run_pipeline()
        )
        return first, second

    first, second = asyncio.run(scenario())
    statuses = sorted([first["status"], second["status"]])
    assert statuses == ["already_running", "completed"]
    # The already_running response must be immediate and minimal
    losing = first if first["status"] == "already_running" else second
    assert losing == {"status": "already_running"}


def test_lock_released_after_run(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)

    async def scenario():
        r1 = await pipeline.run_pipeline()
        assert not pipeline.is_running()
        r2 = await pipeline.run_pipeline()
        return r1, r2

    r1, r2 = asyncio.run(scenario())
    assert r1["status"] == "completed"
    assert r2["status"] == "completed"


def test_lock_released_when_stage_raises(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)

    async def boom(cfg, *, time_window_override=None):
        raise RuntimeError("collector exploded")

    monkeypatch.setattr(pipeline.collector, "run", boom)

    async def scenario():
        with pytest.raises(RuntimeError):
            await pipeline.run_pipeline()
        assert not pipeline.is_running()

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# Market resolution + summary + persistence
# ---------------------------------------------------------------------------

def test_explicit_markets_passed_to_load_config(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)
    summary = asyncio.run(pipeline.run_pipeline(markets=["Italy", "EU"]))
    assert record["load_config_markets"] == ["Italy", "EU"]
    assert summary["markets"] == ["Italy", "EU"]


def test_default_markets_resolve_from_home_markets(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["France"])
    asyncio.run(pipeline.run_pipeline())
    assert record["load_config_markets"] == ["France"]


def test_default_markets_unset_means_unrestricted(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)
    asyncio.run(pipeline.run_pipeline())
    assert record["load_config_markets"] is None


def test_home_markets_empty_list_means_unrestricted(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)
    runtime_settings.set(runtime_settings.HOME_MARKETS, [])
    asyncio.run(pipeline.run_pipeline())
    assert record["load_config_markets"] is None


def test_summary_counts_and_last_run_persisted(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)
    summary = asyncio.run(pipeline.run_pipeline(markets=["Italy"]))
    assert summary["status"] == "completed"
    assert summary["collector"] == {"total_new": 2, "errors": 0}
    assert summary["ai_filter"]["scored"] == 1
    assert summary["prefilter"] == {"candidates": 0}
    assert summary["translator"] == {"translated": 0}
    assert "duration_seconds" in summary

    assert runtime_settings.get(runtime_settings.LAST_RUN_AT) is not None
    persisted = runtime_settings.get(runtime_settings.LAST_RUN_SUMMARY)
    assert persisted["status"] == "completed"
    assert persisted["markets"] == ["Italy"]


# ---------------------------------------------------------------------------
# ai_skip_prescreen_days override plumbing
# ---------------------------------------------------------------------------

def test_prescreen_override_passed_to_ai_filter(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)
    runtime_settings.set(runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE, 3)
    asyncio.run(pipeline.run_pipeline())
    assert record["skip_prescreen_days"] == 3


def test_prescreen_override_unset_passes_none(monkeypatch):
    record: dict = {}
    _patch_stages(monkeypatch, record)
    asyncio.run(pipeline.run_pipeline())
    assert record["skip_prescreen_days"] is None


# ---------------------------------------------------------------------------
# /run-collection endpoint routes through the pipeline (409 when locked)
# ---------------------------------------------------------------------------

def test_endpoint_returns_409_when_already_running(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import main as main_module

    async def fake_run_pipeline(markets=None, *, dry_run=False):
        return {"status": "already_running"}

    monkeypatch.setattr(main_module.pipeline, "run_pipeline", fake_run_pipeline)
    resp = TestClient(app).post("/run-collection")
    assert resp.status_code == 409
    assert resp.json() == {"status": "already_running"}


def test_endpoint_returns_summary_on_success(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    from app import main as main_module

    async def fake_run_pipeline(markets=None, *, dry_run=False):
        return {
            "status": "completed",
            "collector": {"total_new": 1},
            "ai_filter": {"scored": 1},
        }

    monkeypatch.setattr(main_module.pipeline, "run_pipeline", fake_run_pipeline)
    resp = TestClient(app).post("/run-collection")
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"


# ---------------------------------------------------------------------------
# CLI --markets parsing (scripts/run_collection.py)
# ---------------------------------------------------------------------------

def test_parse_markets_valid():
    from scripts.run_collection import _parse_markets

    assert _parse_markets("Italy,EU") == ["Italy", "EU"]
    assert _parse_markets("Italy, EU") == ["Italy", "EU"]  # spaces tolerated
    assert _parse_markets("DACH") == ["DACH"]


def test_parse_markets_invalid_raises():
    from scripts.run_collection import _parse_markets

    with pytest.raises(argparse.ArgumentTypeError):
        _parse_markets("Italy,Atlantis")
    with pytest.raises(argparse.ArgumentTypeError):
        _parse_markets("")


def test_cli_invalid_markets_exits_2(capsys):
    from scripts.run_collection import _parse_args

    with pytest.raises(SystemExit) as exc:
        _parse_args(["--markets", "Atlantis"])
    assert exc.value.code == 2
    assert "Atlantis" in capsys.readouterr().err


def test_cli_valid_markets_parsed():
    from scripts.run_collection import _parse_args

    args = _parse_args(["--markets", "Italy,EU", "--dry-run"])
    assert args.markets == ["Italy", "EU"]
    assert args.dry_run is True
    args_default = _parse_args([])
    assert args_default.markets is None


# ---------------------------------------------------------------------------
# v0.5.1 adaptive time-window
# ---------------------------------------------------------------------------


def _stamp_last_run(gap_days: float) -> None:
    """Helper: pretend the last non-dry run happened ``gap_days`` ago."""
    when = datetime.now(timezone.utc) - timedelta(days=gap_days)
    runtime_settings.set(runtime_settings.LAST_NON_DRY_RUN_AT, when.isoformat())


def test_first_run_uses_1y_window(monkeypatch):
    """No LAST_NON_DRY_RUN_AT → the very first run does a 1y sweep (capped
    at the prefilter's 180-day cutoff × 2, so a bit of headroom for
    stale-metadata articles)."""
    record: dict = {}
    _patch_stages(monkeypatch, record)

    async def scenario():
        summary = await pipeline.run_pipeline()
        assert summary["time_window"] == "1y"
        assert summary["time_window_reason"] == "first-run"
        assert record["collector_time_window"] == "1y"

    asyncio.run(scenario())


def test_recent_run_uses_1m_window(monkeypatch):
    """Last run was 5 days ago → 1m window covers the gap with headroom."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    _stamp_last_run(gap_days=5)

    async def scenario():
        summary = await pipeline.run_pipeline()
        assert summary["time_window"] == "1m"
        assert summary["time_window_reason"].startswith("gap=")
        assert record["collector_time_window"] == "1m"

    asyncio.run(scenario())


def test_mid_gap_uses_3m_window(monkeypatch):
    """60-day gap → 3m bucket."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    _stamp_last_run(gap_days=60)

    async def scenario():
        summary = await pipeline.run_pipeline()
        assert summary["time_window"] == "3m"
        assert record["collector_time_window"] == "3m"

    asyncio.run(scenario())


def test_long_gap_uses_6m_window(monkeypatch):
    """150-day gap → 6m bucket."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    _stamp_last_run(gap_days=150)

    async def scenario():
        summary = await pipeline.run_pipeline()
        assert summary["time_window"] == "6m"
        assert record["collector_time_window"] == "6m"

    asyncio.run(scenario())


def test_very_long_gap_caps_at_1y(monkeypatch):
    """400-day gap → 1y bucket (the cap — beyond this, the prefilter would
    throw everything away anyway)."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    _stamp_last_run(gap_days=400)

    async def scenario():
        summary = await pipeline.run_pipeline()
        assert summary["time_window"] == "1y"
        assert "capped" in summary["time_window_reason"]
        assert record["collector_time_window"] == "1y"

    asyncio.run(scenario())


def test_dry_run_uses_env_default(monkeypatch):
    """dry_run=True must NOT trigger adaptive selection — dry runs pass
    None through so collector.run inherits COLLECTOR_TIME_WINDOW from the
    environment. Otherwise a smoke-test would slurp 3 months of GDELT
    every time."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    _stamp_last_run(gap_days=200)  # would normally trigger 1y-cap

    async def scenario():
        summary = await pipeline.run_pipeline(dry_run=True)
        assert summary["time_window"] is None
        assert summary["time_window_reason"] is None
        assert record["collector_time_window"] is None

    asyncio.run(scenario())


def test_explicit_override_wins(monkeypatch):
    """An explicit ``time_window_override`` (e.g. from scripts/backfill.py
    or a test) must bypass the adaptive selection entirely."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    _stamp_last_run(gap_days=5)  # would otherwise pick 1m

    async def scenario():
        summary = await pipeline.run_pipeline(time_window_override="5y")
        assert summary["time_window"] == "5y"
        # No adaptive reason because the override short-circuited it.
        assert summary["time_window_reason"] is None
        assert record["collector_time_window"] == "5y"

    asyncio.run(scenario())


def test_adaptive_bypassed_when_setup_incomplete(monkeypatch):
    """Wizard-not-yet-done: run_pipeline must not adapt the window, because
    without keywords/feeds/markets the collector has nothing to fetch and
    we'd pointlessly hit Google/GDELT rate limits."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    runtime_settings.set(runtime_settings.SETUP_COMPLETED, False)

    async def scenario():
        summary = await pipeline.run_pipeline()
        assert summary["time_window"] is None
        assert summary["time_window_reason"] is None
        assert record["collector_time_window"] is None

    asyncio.run(scenario())


def test_pre_wizard_run_does_not_stamp_last_non_dry_run_at(monkeypatch):
    """Review-fix regression: a run that fires before SETUP_COMPLETED must
    NOT stamp LAST_NON_DRY_RUN_AT, otherwise the first real post-wizard
    run would adaptively pick '1m' instead of '1y' and permanently miss
    articles 30-180 days old.

    Failure scenario: fresh install, scheduler-catch-up fires two minutes
    after startup (before wizard completed). Without this guard, the stamp
    lands and _adaptive_time_window() returns '1m' on the FIRST post-wizard
    run. With the guard, LAST_NON_DRY_RUN_AT stays unset → first real run
    correctly gets '1y'.
    """
    record: dict = {}
    _patch_stages(monkeypatch, record)
    runtime_settings.set(runtime_settings.SETUP_COMPLETED, False)

    async def scenario():
        await pipeline.run_pipeline()

    asyncio.run(scenario())
    # LAST_NON_DRY_RUN_AT must stay unset — this is what protects the 1y
    # first-run behaviour on the subsequent post-wizard run.
    assert runtime_settings.get(runtime_settings.LAST_NON_DRY_RUN_AT) is None
    # LAST_RUN_AT can still be stamped — the scheduler uses that one for
    # its own scheduling telemetry, not for adaptive-window computation.
    assert runtime_settings.get(runtime_settings.LAST_RUN_AT) is not None


def test_corrupt_last_run_falls_back_to_1y(monkeypatch):
    """If LAST_NON_DRY_RUN_AT is a malformed string (should not happen, but
    defensive), the adaptive helper falls back to 1y — same as first-run."""
    record: dict = {}
    _patch_stages(monkeypatch, record)
    runtime_settings.set(runtime_settings.LAST_NON_DRY_RUN_AT, "not-an-iso-date")

    async def scenario():
        summary = await pipeline.run_pipeline()
        assert summary["time_window"] == "1y"
        assert summary["time_window_reason"] == "first-run"

    asyncio.run(scenario())

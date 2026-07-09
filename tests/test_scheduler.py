"""Tests for the in-process scheduler (Task 5).

APScheduler's ``AsyncIOScheduler.start()`` requires a running asyncio loop —
we therefore drive each test through :func:`asyncio.run` and inspect
``_SCHEDULER.get_jobs()`` from inside that loop. The scheduler is stopped
before the loop closes so no zombie tasks leak between tests.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app import runtime_settings, scheduler


@pytest.fixture(autouse=True)
def _stop_scheduler_after_test():
    """Belt-and-braces: stop even if the test forgot / an assertion raised."""
    yield
    scheduler.stop()


def _run(coro):
    """Convenience wrapper — every test uses this pattern."""
    return asyncio.run(coro)


def _cron_fields(job):
    return {f.name: str(f) for f in job.trigger.fields}


# ---------------------------------------------------------------------------
# start / disabled
# ---------------------------------------------------------------------------


def test_scheduler_uses_settings_timezone(monkeypatch):
    """The AsyncIOScheduler and each CronTrigger must be constructed with
    the timezone from ``settings.timezone`` (so the daily HH:MM fires in
    local wall time, not UTC)."""
    monkeypatch.setenv("TIMEZONE", "Europe/Lisbon")
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")

    async def scenario():
        scheduler.start()
        daily = scheduler._SCHEDULER.get_job("daily_run")
        return (
            str(scheduler._SCHEDULER.timezone),
            str(daily.trigger.timezone),
        )

    sched_tz, trigger_tz = _run(scenario())
    assert "Lisbon" in sched_tz
    assert "Lisbon" in trigger_tz


def test_scheduler_start_registers_job_when_enabled():
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")

    async def scenario():
        scheduler.start()
        job = scheduler._SCHEDULER.get_job("daily_run")
        return job.id, _cron_fields(job)

    job_id, fields = _run(scenario())
    assert job_id == "daily_run"
    assert fields["hour"] == "6"
    assert fields["minute"] == "0"


def test_scheduler_start_skips_when_disabled():
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, False)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")

    async def scenario():
        scheduler.start()
        return [j for j in scheduler._SCHEDULER.get_jobs() if j.id == "daily_run"]

    assert _run(scenario()) == []


# ---------------------------------------------------------------------------
# reload
# ---------------------------------------------------------------------------


def test_reload_swaps_job_when_time_changes():
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")

    async def scenario():
        scheduler.start()
        first = scheduler._SCHEDULER.get_job("daily_run")
        assert first is not None
        runtime_settings.set(runtime_settings.SCHEDULER_TIME, "07:30")
        scheduler.reload()
        job = scheduler._SCHEDULER.get_job("daily_run")
        daily_count = len([j for j in scheduler._SCHEDULER.get_jobs() if j.id == "daily_run"])
        return _cron_fields(job), daily_count

    fields, count = _run(scenario())
    assert fields["hour"] == "7"
    assert fields["minute"] == "30"
    # Reload must NOT stack duplicates.
    assert count == 1


def test_reload_removes_job_when_disabled():
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")

    async def scenario():
        scheduler.start()
        assert scheduler._SCHEDULER.get_job("daily_run") is not None
        runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, False)
        scheduler.reload()
        return scheduler._SCHEDULER.get_job("daily_run")

    assert _run(scenario()) is None


def test_reload_starts_scheduler_when_none_and_enabled():
    """Fresh install path: the app booted with SCHEDULER_ENABLED=False so no
    scheduler exists; the user toggles it on via the wizard and reload() is
    called. reload() must start the scheduler so the daily job is live
    without a restart."""
    # Ensure no scheduler is running.
    scheduler.stop()
    assert scheduler._SCHEDULER is None
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "08:00")

    async def scenario():
        scheduler.reload()
        return (
            scheduler._SCHEDULER,
            [j.id for j in scheduler._SCHEDULER.get_jobs()]
            if scheduler._SCHEDULER
            else [],
        )

    running, job_ids = _run(scenario())
    assert running is not None
    assert "daily_run" in job_ids


def test_disable_removes_catchup_job():
    """When the user disables the scheduler, both the daily job AND any
    pending catch-up must be removed — otherwise a stale-run auto-catchup
    survives the disable."""
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "08:00")
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    runtime_settings.set(runtime_settings.LAST_NON_DRY_RUN_AT, stale)

    async def scenario():
        scheduler.start()
        assert any(j.id == "catchup" for j in scheduler._SCHEDULER.get_jobs())
        runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, False)
        scheduler.reload()
        return [j.id for j in scheduler._SCHEDULER.get_jobs()]

    remaining = _run(scenario())
    assert "catchup" not in remaining
    assert "daily_run" not in remaining


# ---------------------------------------------------------------------------
# Catch-up
# ---------------------------------------------------------------------------


def test_catchup_scheduled_when_last_run_stale():
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")
    stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    runtime_settings.set(runtime_settings.LAST_NON_DRY_RUN_AT, stale)

    async def scenario():
        scheduler.start()
        return scheduler._SCHEDULER.get_job("catchup")

    catchup = _run(scenario())
    assert catchup is not None


def test_catchup_skipped_when_last_run_fresh():
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    runtime_settings.set(runtime_settings.LAST_NON_DRY_RUN_AT, fresh)

    async def scenario():
        scheduler.start()
        return scheduler._SCHEDULER.get_job("catchup")

    assert _run(scenario()) is None


def test_catchup_scheduled_when_only_dry_runs_recorded():
    """Dry runs stamp LAST_RUN_AT but NOT LAST_NON_DRY_RUN_AT — so if only
    dry runs have ever fired, catch-up should still kick in."""
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")
    recent_dry = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    runtime_settings.set(runtime_settings.LAST_RUN_AT, recent_dry)
    runtime_settings.set(runtime_settings.LAST_RUN_SUMMARY, {"dry_run": True})
    # LAST_NON_DRY_RUN_AT deliberately unset.

    async def scenario():
        scheduler.start()
        return scheduler._SCHEDULER.get_job("catchup")

    assert _run(scenario()) is not None


def test_catchup_skipped_when_scheduler_disabled_even_if_stale():
    """A user who never opted in shouldn't get a surprise auto-run."""
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, False)
    stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    runtime_settings.set(runtime_settings.LAST_NON_DRY_RUN_AT, stale)

    async def scenario():
        scheduler.start()
        return scheduler._SCHEDULER.get_job("catchup")

    assert _run(scenario()) is None


# ---------------------------------------------------------------------------
# Job body
# ---------------------------------------------------------------------------


def test_scheduled_run_calls_pipeline(monkeypatch):
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    fake = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(scheduler.pipeline, "run_pipeline", fake)

    asyncio.run(scheduler._scheduled_run())

    fake.assert_awaited_once_with(markets=None, dry_run=False)


def test_scheduled_run_swallows_exceptions(monkeypatch):
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)

    async def boom(*args, **kwargs):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr(scheduler.pipeline, "run_pipeline", boom)

    # No exception should escape.
    asyncio.run(scheduler._scheduled_run())


def test_scheduled_run_bails_when_disabled(monkeypatch):
    """Guard: a fire-scheduled catch-up that races with a disable must
    short-circuit before hitting the pipeline."""
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, False)
    fake = AsyncMock(return_value={"status": "completed"})
    monkeypatch.setattr(scheduler.pipeline, "run_pipeline", fake)

    asyncio.run(scheduler._scheduled_run())

    fake.assert_not_awaited()


# ---------------------------------------------------------------------------
# next_run_at helper
# ---------------------------------------------------------------------------


def test_next_run_at_returns_none_when_not_started():
    assert scheduler.next_run_at() is None


def test_next_run_at_returns_datetime_when_scheduled():
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, True)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, "06:00")

    async def scenario():
        scheduler.start()
        return scheduler.next_run_at()

    result = _run(scenario())
    assert isinstance(result, datetime)


# ---------------------------------------------------------------------------
# Optional-dependency degradation
# ---------------------------------------------------------------------------


def test_start_stop_reload_are_noops_when_apscheduler_unavailable(monkeypatch):
    """Simulate `import apscheduler` failing at module load — all public
    entrypoints should become no-ops without raising."""
    monkeypatch.setattr(scheduler, "_APSCHEDULER_AVAILABLE", False)
    monkeypatch.setattr(scheduler, "_SCHEDULER", None)

    scheduler.start()
    assert scheduler._SCHEDULER is None
    scheduler.reload()  # no-op — must not raise
    assert scheduler.next_run_at() is None
    scheduler.stop()  # no-op — must not raise


# ---------------------------------------------------------------------------
# Pipeline persistence — Option A contract check
# ---------------------------------------------------------------------------


def test_pipeline_non_dry_run_stamps_last_non_dry_run_at(monkeypatch):
    """The single write the scheduler catch-up relies on."""
    from app import pipeline as pipeline_module
    from app.models import NotionConfig

    async def fake_collector(cfg, *, time_window_override=None):
        class _S:
            total_new = 0
            errors = 0
            def as_dict(self):
                return {"total_new": 0, "errors": 0}

        return _S()

    monkeypatch.setattr(
        pipeline_module.local_config,
        "load_config",
        lambda *, force_refresh=False, markets=None: NotionConfig(),
    )
    monkeypatch.setattr(pipeline_module.collector, "run", fake_collector)
    monkeypatch.setattr(pipeline_module.prefilter, "run", lambda cfg: {"candidates": 0})
    monkeypatch.setattr(pipeline_module.translator, "run", lambda: {"translated": 0})

    async def fake_ai(cfg, *, skip_prescreen_days=None):
        return {"scored": 0}

    monkeypatch.setattr(pipeline_module.ai_filter, "run", fake_ai)
    pipeline_module._run_lock = asyncio.Lock()

    asyncio.run(pipeline_module.run_pipeline(dry_run=False))

    assert runtime_settings.get(runtime_settings.LAST_NON_DRY_RUN_AT) is not None


def test_pipeline_dry_run_does_not_stamp_last_non_dry_run_at(monkeypatch):
    from app import pipeline as pipeline_module
    from app.models import NotionConfig

    async def fake_collector(cfg, *, time_window_override=None):
        class _S:
            total_new = 0
            errors = 0
            def as_dict(self):
                return {"total_new": 0, "errors": 0}

        return _S()

    monkeypatch.setattr(
        pipeline_module.local_config,
        "load_config",
        lambda *, force_refresh=False, markets=None: NotionConfig(),
    )
    monkeypatch.setattr(pipeline_module.collector, "run", fake_collector)
    pipeline_module._run_lock = asyncio.Lock()

    asyncio.run(pipeline_module.run_pipeline(dry_run=True))

    # LAST_RUN_AT stamped, LAST_NON_DRY_RUN_AT NOT stamped.
    assert runtime_settings.get(runtime_settings.LAST_RUN_AT) is not None
    assert runtime_settings.get(runtime_settings.LAST_NON_DRY_RUN_AT) is None

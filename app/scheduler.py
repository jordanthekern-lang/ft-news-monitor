"""In-process daily scheduler for the FT News Monitor pipeline (Task 5).

Wires an APScheduler AsyncIOScheduler into the FastAPI event loop so the
pipeline fires at the configured ``HH:MM`` time whenever the app is running.
On startup we also check whether the previous non-dry run is stale (> 20h)
and, if so, kick off a one-shot "catch-up" run ~2 minutes later.

Design constraints (see Task 5 brief):

* **Optional dependency.** The ``apscheduler`` import is wrapped in
  ``try/except ImportError`` — if it's missing, all public functions become
  no-ops (only a warning is logged). This future-proofs the Task 10
  packaging step: if apscheduler turns out to be problematic on Windows we
  can ship without it and the app still runs, just without the daily job.
* **Idempotent reload.** :func:`reload` removes the existing job (stable
  ID ``daily_run``) before adding the fresh one so a settings change never
  stacks two identical triggers.
* **Dry runs don't suppress catch-up.** Catch-up uses
  :data:`runtime_settings.LAST_NON_DRY_RUN_AT`, which the pipeline only
  writes on ``dry_run=False`` runs.
* **No double-execution.** :func:`_scheduled_run` calls into
  :func:`pipeline.run_pipeline`, which owns its own asyncio lock — if the
  user clicks the Run button while the scheduled job is executing, the
  second caller gets ``already_running`` back.

The public API — :func:`start`, :func:`stop`, :func:`reload`,
:func:`next_run_at` — is intentionally tiny so callers (FastAPI lifespan,
wizard/settings POST handlers) don't have to know about APScheduler at all.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from . import pipeline, runtime_settings
from .config import get_settings

logger = logging.getLogger(__name__)

# --- Optional apscheduler import --------------------------------------------
# If the package isn't installed the whole module degrades to no-ops so the
# rest of the app keeps working. Packaging (Task 10) may deliberately ship
# without apscheduler on Windows if it turns out to be problematic.
try:  # pragma: no cover - trivial import guard
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger
    from apscheduler.triggers.date import DateTrigger

    _APSCHEDULER_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised by test mocking
    AsyncIOScheduler = None  # type: ignore[assignment,misc]
    CronTrigger = None  # type: ignore[assignment,misc]
    DateTrigger = None  # type: ignore[assignment,misc]
    _APSCHEDULER_AVAILABLE = False
    logger.warning(
        "apscheduler is not installed — in-app scheduler disabled; "
        "start/stop/reload/next_run_at are no-ops."
    )


# Module-level singleton so start/stop/reload cooperate.
_SCHEDULER: Optional["AsyncIOScheduler"] = None

# Stable job IDs — reload() looks these up to swap or remove them.
_DAILY_JOB_ID = "daily_run"
_CATCHUP_JOB_ID = "catchup"

# A non-dry run older than this counts as "stale" → schedule a catch-up.
_CATCHUP_STALE_HOURS = 20
# How long after startup the catch-up fires (gives the app time to settle).
_CATCHUP_DELAY_MINUTES = 2


# ---------------------------------------------------------------------------
# Job body
# ---------------------------------------------------------------------------


async def _scheduled_run() -> None:
    """APScheduler entrypoint — runs the pipeline and logs, never raises.

    Uses ``markets=None`` so :func:`pipeline.run_pipeline` resolves the
    installation's ``home_markets`` (unset/empty → unrestricted). Runs are
    never dry from the scheduler — dry runs are a manual/CLI concept.
    """
    # Belt-and-braces: a fire-scheduled catch-up job may still be in the
    # queue when the user disables the scheduler; short-circuit here so the
    # pipeline never runs while ``SCHEDULER_ENABLED`` is falsy.
    if not runtime_settings.get(runtime_settings.SCHEDULER_ENABLED, False):
        return
    try:
        summary = await pipeline.run_pipeline(markets=None, dry_run=False)
        logger.info("Scheduled pipeline run finished: %s", summary.get("status"))
    except Exception:
        # Swallow: an APScheduler job that raises would be logged by
        # APScheduler but might disrupt subsequent fires depending on
        # misfire_grace_time. Belt-and-braces log + return.
        logger.exception("Scheduled pipeline run failed")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def start() -> None:
    """Instantiate + start the scheduler if enabled; run catch-up check.

    Idempotent: calling ``start()`` twice is safe — the second call is a
    no-op because the singleton is still running.
    """
    global _SCHEDULER
    if not _APSCHEDULER_AVAILABLE:
        return
    if _SCHEDULER is not None:
        return

    _SCHEDULER = AsyncIOScheduler(timezone=ZoneInfo(get_settings().timezone))

    # Schedule the daily job from current settings (adds nothing if disabled).
    _apply_current_settings(_SCHEDULER)
    _maybe_schedule_catchup(_SCHEDULER)

    _SCHEDULER.start()
    logger.info(
        "Scheduler started (enabled=%s time=%s)",
        runtime_settings.get(runtime_settings.SCHEDULER_ENABLED, False),
        runtime_settings.get(runtime_settings.SCHEDULER_TIME) or "unset",
    )


def stop() -> None:
    """Shutdown the scheduler and drop the singleton so start() works again."""
    global _SCHEDULER
    if not _APSCHEDULER_AVAILABLE:
        return
    if _SCHEDULER is None:
        return
    try:
        _SCHEDULER.shutdown(wait=False)
    except Exception:  # pragma: no cover - defensive
        logger.exception("Scheduler shutdown raised")
    finally:
        _SCHEDULER = None
        logger.info("Scheduler stopped")


def reload() -> None:
    """Re-read runtime settings and swap the daily job in place.

    Safe to call any time — from the wizard's POST /setup/schedule handler,
    from POST /settings/update, or by hand. Always removes the existing
    daily job before adding a fresh one so a settings change never stacks
    duplicates.
    """
    if not _APSCHEDULER_AVAILABLE:
        return
    if _SCHEDULER is None:
        # Fresh-install path: the lifespan startup ran with SCHEDULER_ENABLED
        # falsy, so no scheduler exists. If the user has just enabled it via
        # the wizard/settings form, kick the scheduler off now so the daily
        # job takes effect without a process restart.
        if runtime_settings.get(runtime_settings.SCHEDULER_ENABLED, False):
            start()
        return
    _apply_current_settings(_SCHEDULER)
    logger.info(
        "Scheduler reloaded (enabled=%s time=%s)",
        runtime_settings.get(runtime_settings.SCHEDULER_ENABLED, False),
        runtime_settings.get(runtime_settings.SCHEDULER_TIME) or "unset",
    )


def next_run_at() -> Optional[datetime]:
    """Return the daily job's next fire time (local tz-aware), or None.

    Consumed by the header chip in ``base.html``. Returns None when the
    scheduler isn't running, is disabled, or apscheduler isn't installed.
    """
    if not _APSCHEDULER_AVAILABLE or _SCHEDULER is None:
        return None
    job = _SCHEDULER.get_job(_DAILY_JOB_ID)
    if job is None:
        return None
    return job.next_run_time


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _apply_current_settings(scheduler: "AsyncIOScheduler") -> None:
    """Remove any existing daily job, then re-add per current settings.

    Kept private so callers only see start/stop/reload — the "remove then
    add" contract lives here so both paths share it.
    """
    existing = scheduler.get_job(_DAILY_JOB_ID)
    if existing is not None:
        scheduler.remove_job(_DAILY_JOB_ID)

    if not runtime_settings.get(runtime_settings.SCHEDULER_ENABLED, False):
        # Disable-path: also drop any pending catch-up so a fresh install
        # that toggles the scheduler off doesn't get a surprise auto-run
        # from an earlier stale-run detection.
        existing_catchup = scheduler.get_job(_CATCHUP_JOB_ID)
        if existing_catchup is not None:
            scheduler.remove_job(_CATCHUP_JOB_ID)
        return

    time_value = runtime_settings.get(runtime_settings.SCHEDULER_TIME) or "06:00"
    try:
        hour_str, minute_str = time_value.split(":", 1)
        hour = int(hour_str)
        minute = int(minute_str)
    except (ValueError, AttributeError):
        logger.warning("Invalid scheduler_time %r — daily job not scheduled", time_value)
        return

    tz = ZoneInfo(get_settings().timezone)
    scheduler.add_job(
        _scheduled_run,
        trigger=CronTrigger(hour=hour, minute=minute, timezone=tz),
        id=_DAILY_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,  # allow up to 1h late fire (laptop was asleep)
        coalesce=True,  # don't stack pending fires
        max_instances=1,
    )


def _maybe_schedule_catchup(scheduler: "AsyncIOScheduler") -> None:
    """Fire a one-shot run shortly after startup if the last non-dry run is stale.

    "Stale" means older than :data:`_CATCHUP_STALE_HOURS` hours, or never
    completed at all (setting is None). Only runs when the scheduler is
    enabled — a user who hasn't opted in shouldn't get an auto-run just for
    launching the app.
    """
    if not runtime_settings.get(runtime_settings.SCHEDULER_ENABLED, False):
        return

    last_iso = runtime_settings.get(runtime_settings.LAST_NON_DRY_RUN_AT)
    if last_iso:
        try:
            last_dt = datetime.fromisoformat(last_iso)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - last_dt
            if age < timedelta(hours=_CATCHUP_STALE_HOURS):
                return  # fresh — no catch-up needed
        except ValueError:
            # Corrupt timestamp — err on the side of catching up.
            logger.warning("Corrupt LAST_NON_DRY_RUN_AT %r; scheduling catch-up", last_iso)

    run_at = datetime.now(timezone.utc) + timedelta(minutes=_CATCHUP_DELAY_MINUTES)
    scheduler.add_job(
        _scheduled_run,
        trigger=DateTrigger(run_date=run_at),
        id=_CATCHUP_JOB_ID,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    logger.info("Catch-up run scheduled for %s", run_at.isoformat())

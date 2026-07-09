"""Single pipeline entrypoint: collector → prefilter → translator → ai_filter.

Every trigger (CLI, dashboard button, future scheduler / setup wizard) goes
through :func:`run_pipeline` so market scoping, the overlap lock and last-run
bookkeeping behave identically everywhere.
"""

from __future__ import annotations

import asyncio
import errno
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from . import (
    ai_filter,
    collector,
    config as _config,
    database,
    local_config,
    prefilter,
    runtime_settings,
    translator,
)

logger = logging.getLogger(__name__)

# In-process guard: fast reject on button-mashing without touching disk.
_run_lock = asyncio.Lock()

# Cross-process guard: named lockfile in DATA_DIR. Prevents the dashboard
# server, `scripts/run_collection.py` CLI, and APScheduler catch-up from
# racing on SQLite writes when the user has two ways to trigger a run
# active at once. The in-process lock alone is not enough — each of those
# entrypoints lives in its own Python interpreter with its own event loop.
_PIPELINE_LOCK_FILENAME = "pipeline.lock"
_STALE_LOCK_AFTER_HOURS = 6


def _lock_path() -> Path:
    return _config.DATA_DIR / _PIPELINE_LOCK_FILENAME


def _acquire_process_lock() -> Optional[int]:
    """Try to grab the cross-process lockfile. Returns the fd on success,
    None if another process is holding it. Writes ``pid=<pid>\nts=<iso>``
    to the file so an admin can identify the holder.

    Stale-lock recovery: if the file exists but is older than
    ``_STALE_LOCK_AFTER_HOURS`` and its PID no longer resolves, we treat
    it as abandoned and take it over. Prevents an indelible lock after a
    crash / kill / hibernate.
    """
    path = _lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno != errno.EEXIST:
            raise
        # Existing lock — check if stale.
        if _is_lock_stale(path):
            try:
                os.unlink(path)
            except OSError:
                return None
            try:
                fd = os.open(path, flags)
            except OSError:
                return None
        else:
            return None
    payload = f"pid={os.getpid()}\nts={datetime.now(timezone.utc).isoformat()}\n"
    try:
        os.write(fd, payload.encode("utf-8"))
    except OSError:  # pragma: no cover
        pass
    return fd


def _is_lock_stale(path: Path) -> bool:
    """Best-effort stale-lock detection. Returns True when either:
    - the mtime is older than _STALE_LOCK_AFTER_HOURS, OR
    - the recorded PID no longer exists (kill 0 test on POSIX; on Windows
      we use os.kill(pid, 0) which raises OSError for unknown PIDs).
    Failure to read/parse the file treats it as stale (we prefer breaking
    a mis-encoded lock over an indelible one)."""
    try:
        stat = path.stat()
    except OSError:
        return True
    age_hours = (time.time() - stat.st_mtime) / 3600.0
    if age_hours > _STALE_LOCK_AFTER_HOURS:
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    pid = None
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                pid = int(line.split("=", 1)[1])
            except (IndexError, ValueError):
                pass
            break
    if pid is None:
        return True
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        # Different user's process (PermissionError) — treat as live.
        return isinstance(_ := None, ProcessLookupError)
    except OSError:
        return True
    return False


def _release_process_lock(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:  # pragma: no cover
        pass
    try:
        os.unlink(_lock_path())
    except OSError:  # pragma: no cover
        pass


def is_running() -> bool:
    """True while a pipeline run is active in THIS process. Cross-process
    activity is not queryable without acquiring the lock; the run_pipeline
    entrypoint reports ``already_running`` when it can't get it."""
    return _run_lock.locked()


def _resolve_markets(markets: Optional[List[str]]) -> Optional[List[str]]:
    """Apply the home-markets default: None → runtime setting; empty → None."""
    if markets is None:
        return runtime_settings.effective_home_markets()
    return list(markets) or None


def _prescreen_days_override() -> Optional[int]:
    """User override for the prescreen bypass window; None = use env default."""
    raw = runtime_settings.get(runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE)
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0:
        return raw
    return None


def _adaptive_time_window() -> tuple[str, str]:
    """Pick a collector time-window based on time since the last non-dry run.

    Returns ``(window, reason)`` where ``window`` is one of GDELT's discrete
    buckets (``"1m"``, ``"3m"``, ``"6m"``, ``"1y"``) and ``reason`` is a short
    human-readable log token.

    Rationale (v0.5.1): the previous implementation always used ``"5y"`` on the
    first run, but the prefilter throws away everything older than
    ``MAX_AGE_DAYS`` (default 180) — so ~40% of the sweep was wasted work.
    Capping at ``1y`` matches the prefilter's semantics, and shrinking the
    window on subsequent runs keeps daily sweeps cheap while still covering
    the gap if the tool wasn't run for a while.
    """
    last_iso = runtime_settings.get(runtime_settings.LAST_NON_DRY_RUN_AT)
    if not isinstance(last_iso, str) or not last_iso:
        return "1y", "first-run"
    try:
        last = datetime.fromisoformat(last_iso)
    except ValueError:
        return "1y", "first-run"
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    gap_days = (datetime.now(timezone.utc) - last).total_seconds() / 86400.0
    if gap_days <= 30:
        return "1m", f"gap={gap_days:.1f}d"
    if gap_days <= 90:
        return "3m", f"gap={gap_days:.1f}d"
    if gap_days <= 180:
        return "6m", f"gap={gap_days:.1f}d"
    return "1y", f"gap={gap_days:.1f}d (capped)"


async def run_pipeline(
    markets: Optional[List[str]] = None,
    *,
    dry_run: bool = False,
    time_window_override: Optional[str] = None,
) -> dict:
    """Run the full collection pipeline; returns a summary dict.

    ``markets=None`` resolves to the installation's ``home_markets`` runtime
    setting (unset/empty → unrestricted). If a run is already active the call
    returns ``{"status": "already_running"}`` immediately.

    ``dry_run=True`` stops after the collector (no pre-filter, no AI tokens).

    ``time_window_override`` forces a specific collector time-window (e.g.
    ``"5y"`` for the one-off backfill). Regular runs pass ``None`` and let
    :func:`_adaptive_time_window` pick a value based on time since the last
    non-dry run.
    """
    if _run_lock.locked():
        return {"status": "already_running"}

    async with _run_lock:
        # v0.6.1 M6: cross-process guard. When the dashboard server and
        # the CLI script are both running, only one may execute the
        # pipeline at a time — else they'd race on SQLite writes.
        lock_fd = _acquire_process_lock()
        if lock_fd is None:
            return {"status": "already_running"}
        try:
            database.init_db()
            markets = _resolve_markets(markets)

            # v0.5.1: the collector time-window adapts to time-since-last-run.
            # Explicit override (backfill script, tests) wins. Dry runs pass
            # None through so the collector inherits the env default without
            # touching last_non_dry_run_at. The setup_completed gate matches
            # v0.5.0's guardrail #7: don't sweep the world before the wizard
            # has produced any keywords/feeds/markets.
            effective_time_window = time_window_override
            time_window_reason: Optional[str] = None
            setup_done = bool(runtime_settings.get(runtime_settings.SETUP_COMPLETED, False))
            if effective_time_window is None and not dry_run and setup_done:
                effective_time_window, time_window_reason = _adaptive_time_window()
                logger.info(
                    "Adaptive time-window: %s (%s)",
                    effective_time_window,
                    time_window_reason,
                )

            started = datetime.now(timezone.utc)
            t0 = time.monotonic()
            logger.info(
                "Pipeline start (markets=%s, dry_run=%s, time_window=%s)",
                markets or "all",
                dry_run,
                effective_time_window or "env-default",
            )

            cfg = local_config.load_config(markets=markets)
            coll_stats = await collector.run(cfg, time_window_override=effective_time_window)

            summary: dict = {
                "status": "completed",
                "markets": markets,
                "dry_run": dry_run,
                "started_at": started.isoformat(),
                "collector": coll_stats.as_dict(),
                "time_window": effective_time_window,
                "time_window_reason": time_window_reason,
                "prefilter": None,
                "translator": None,
                "ai_filter": None,
            }

            if not dry_run:
                summary["prefilter"] = prefilter.run(cfg)
                summary["translator"] = translator.run()
                summary["ai_filter"] = await ai_filter.run(
                    cfg, skip_prescreen_days=_prescreen_days_override()
                )

            summary["duration_seconds"] = round(time.monotonic() - t0, 2)

            iso_now = datetime.now(timezone.utc).isoformat()
            runtime_settings.set(runtime_settings.LAST_RUN_AT, iso_now)
            # Scheduler catch-up logic (see app/scheduler.py) uses this separate
            # timestamp so that dry runs (which don't touch prefilter/AI) do not
            # suppress a real catch-up. Full runs stamp both keys, dry runs only
            # touch LAST_RUN_AT + LAST_RUN_SUMMARY.
            #
            # v0.5.1 review-fix: also skip the stamp when setup_completed is False.
            # Otherwise a pre-wizard trigger (scheduler catch-up, POST /run-collection,
            # CLI) writes LAST_NON_DRY_RUN_AT even though the run collected nothing —
            # and the first REAL post-wizard run then adaptively picks "1m" instead
            # of "1y", permanently missing 30-180-day-old articles the prefilter
            # would otherwise accept.
            if not dry_run and setup_done:
                runtime_settings.set(runtime_settings.LAST_NON_DRY_RUN_AT, iso_now)
            runtime_settings.set(runtime_settings.LAST_RUN_SUMMARY, summary)
            logger.info("Pipeline complete: %s", summary)
            return summary
        finally:
            _release_process_lock(lock_fd)

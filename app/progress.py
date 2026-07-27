"""Live pipeline progress state (v0.7.4).

The dashboard polls ``/pipeline-status`` every ~2s while a collection
run is active. To answer that endpoint cheaply and without touching the
DB, the pipeline stages write their current phase + counters into an
in-process singleton exposed here.

Design constraints:

- No thread-locking on the read path — the endpoint MUST be fast (the
  UI polls it while the pipeline holds the DB), so we accept eventual
  consistency: a reader may see a mid-write snapshot where phase says
  ``collecting`` but current/total lag by 1. Not worth a lock.
- Never raise on caller errors. Reporters are best-effort telemetry;
  the pipeline must not crash because a UI helper had a typo.
- Reset explicitly on run start via :func:`start_run`. If a run crashes
  without calling :func:`finish_run`, the last stale state remains
  visible until the next run starts — that's fine; the UI shows the
  crash timestamp and lets the user re-trigger.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Optional


# The five ordered phases the UI shows a bar-segment for. Kept
# separate from the collector's more granular "step" strings.
PHASES = ("idle", "collecting", "prefiltering", "translating", "ai_scoring", "done")


@dataclass
class _ProgressState:
    """Snapshot returned to the UI."""

    phase: str = "idle"          # one of PHASES
    step: str = ""               # human-readable sub-step ("Feeds 12/31")
    current: int = 0             # done in current phase
    total: int = 0               # total items in current phase
    started_at: float = 0.0      # time.monotonic() when the run started
    finished_at: float = 0.0     # 0 while running; monotonic time when done
    last_error: str = ""         # populated only when a run finishes with an exception
    markets: list = field(default_factory=list)


# Module-level singleton — one pipeline runs at a time (guarded by the
# pipeline's asyncio.Lock + cross-process file lock), so a shared object
# is safe.
_state = _ProgressState()


def snapshot() -> dict:
    """Return the current state as a JSON-serialisable dict.

    Additionally exposes ``elapsed_seconds`` so the UI doesn't have to
    do its own math against the server clock.
    """
    now = time.monotonic()
    running = _state.phase not in ("idle", "done")
    if _state.started_at:
        if running:
            elapsed = now - _state.started_at
        else:
            elapsed = max(0.0, _state.finished_at - _state.started_at)
    else:
        elapsed = 0.0
    return {**asdict(_state), "elapsed_seconds": round(elapsed, 1), "running": running}


def start_run(markets: Optional[list] = None) -> None:
    """Reset the state for a fresh pipeline run."""
    _state.phase = "collecting"
    _state.step = ""
    _state.current = 0
    _state.total = 0
    _state.started_at = time.monotonic()
    _state.finished_at = 0.0
    _state.last_error = ""
    _state.markets = list(markets) if markets else []


def set_phase(phase: str, *, step: str = "", current: int = 0, total: int = 0) -> None:
    """Move to a new phase. Callers pass explicit sub-step + counters."""
    if phase not in PHASES:
        # Ignore silently — never raise from telemetry.
        return
    _state.phase = phase
    _state.step = step
    _state.current = current
    _state.total = total


def set_step(step: str, *, current: Optional[int] = None, total: Optional[int] = None) -> None:
    """Update the sub-step within the current phase.

    ``current`` and ``total`` are optional so callers can e.g. bump only
    the current count as items complete.
    """
    _state.step = step
    if current is not None:
        _state.current = current
    if total is not None:
        _state.total = total


def bump_current(delta: int = 1) -> None:
    """Advance the current-item counter by ``delta``."""
    _state.current += delta


def finish_run(*, error: Optional[str] = None) -> None:
    """Mark the run finished. Sets phase to 'done' regardless of success."""
    _state.phase = "done"
    _state.finished_at = time.monotonic()
    _state.last_error = error or ""

"""One-time historical backfill: run the collector with a broad time-window.

Usage:
    python scripts/backfill.py                        # 5y sweep, home markets
    python scripts/backfill.py --window 1y            # narrower sweep
    python scripts/backfill.py --markets Italy,EU     # limit markets
    python scripts/backfill.py --dry-run              # collector only, no AI

Purpose:
    Regular daily runs use ``COLLECTOR_TIME_WINDOW="3m"`` — enough headroom
    to catch fresh items without hammering Google/Bing/GDELT every day. On
    a fresh install (or after a big keyword change) that 3-month window
    misses everything older. This script does a **one-off** wider sweep,
    typically ``5y``, to populate historical bestand from the aggregators'
    indexes. Runtime: ~15-30 minutes (92 keywords × 3 sources).

Concurrency:
    Uses the same in-process pipeline lock as scheduled runs, so calling
    this while the scheduler is firing safely returns ``already_running``.
    An additional file lock (``backfill.lock``) prevents two backfills
    from stepping on each other.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional

# Make the project importable when run as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import pipeline  # noqa: E402
from app.config import DATA_DIR, LOG_PATH, VALID_MARKETS  # noqa: E402

# Same set the collector validates. Keep in sync with app/collector.py.
VALID_WINDOWS = ("1h", "1d", "7d", "1m", "3m", "6m", "1y", "5y")


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(
        LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)


def _parse_markets(raw: str) -> List[str]:
    values = [v.strip() for v in raw.split(",") if v.strip()]
    if not values:
        raise argparse.ArgumentTypeError("no market given")
    invalid = [v for v in values if v not in VALID_MARKETS]
    if invalid:
        raise argparse.ArgumentTypeError(
            f"invalid market(s): {', '.join(invalid)} "
            f"(valid: {', '.join(VALID_MARKETS)})"
        )
    return values


def _parse_window(raw: str) -> str:
    if raw not in VALID_WINDOWS:
        raise argparse.ArgumentTypeError(
            f"invalid window {raw!r} (valid: {', '.join(VALID_WINDOWS)})"
        )
    return raw


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run a one-time historical backfill collection. This is NOT a "
            "regular scheduled run — it uses a much wider time-window "
            "(default 5y) to populate historical article bestand."
        )
    )
    parser.add_argument(
        "--window",
        type=_parse_window,
        default="5y",
        help=f"collector time-window (default: 5y; valid: {', '.join(VALID_WINDOWS)})",
    )
    parser.add_argument(
        "--markets",
        type=_parse_markets,
        default=None,
        metavar="M1,M2",
        help=(
            "comma-separated markets to collect for, e.g. --markets Italy,EU "
            f"(valid: {', '.join(VALID_MARKETS)}; default: home markets from settings)"
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="collector only, skip pre-filter and AI filter (saves Bedrock cost)",
    )
    return parser.parse_args(argv)


_STALE_BACKFILL_LOCK_HOURS = 6


def _is_backfill_lock_stale(path: Path) -> bool:
    """Detect a lock file left behind by a crashed / killed / hibernated
    backfill. Returns True when (a) the file is older than
    ``_STALE_BACKFILL_LOCK_HOURS`` OR (b) the recorded PID no longer exists.
    A malformed lock file (e.g. from v0.5.0's placeholder ``pid=?``) is
    treated as stale — we prefer breaking a mis-encoded lock over an
    indelible one."""
    try:
        stat = path.stat()
    except OSError:
        return True
    age_hours = (time.time() - stat.st_mtime) / 3600.0
    if age_hours > _STALE_BACKFILL_LOCK_HOURS:
        return True
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return True
    pid: Optional[int] = None
    for line in text.splitlines():
        if line.startswith("pid="):
            try:
                pid = int(line.split("=", 1)[1].strip())
            except (IndexError, ValueError):
                pass
            break
    if pid is None or pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (OSError, PermissionError):
        # Different user's process or system-managed PID — treat as live.
        return False
    return False


def main() -> int:
    _setup_logging()
    args = _parse_args()
    log = logging.getLogger("backfill")

    lock_path = DATA_DIR / "backfill.lock"
    # v0.6.1 M9: PID- and age-aware stale-lock detection. A killed or
    # hibernated backfill used to leave an indelible lock the user
    # couldn't find; now we auto-clear if either (a) the recorded PID
    # doesn't exist any more or (b) the lock is older than 6h.
    if lock_path.exists():
        if _is_backfill_lock_stale(lock_path):
            log.warning(
                "backfill.lock at %s is stale — treating as abandoned "
                "and taking over.",
                lock_path,
            )
            try:
                lock_path.unlink()
            except OSError as exc:
                log.error("Could not clear stale backfill.lock: %s", exc)
                return 1
        else:
            log.error(
                "backfill.lock exists at %s — another backfill is running. "
                "If you know it isn't, delete the file to reset.",
                lock_path,
            )
            return 1

    log.warning(
        "=== One-time historical backfill starting ==="
        " window=%s markets=%s dry_run=%s. "
        "Estimated runtime: 15-30 minutes. Regular daily runs will use "
        "the 3m window (COLLECTOR_TIME_WINDOW) unchanged.",
        args.window,
        args.markets or "home_markets",
        args.dry_run,
    )

    try:
        lock_path.write_text(
            f"pid={os.getpid()}\n"
            f"started_at={datetime.now(timezone.utc).isoformat()}\n"
            f"window={args.window}\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.error("Could not create backfill.lock: %s", exc)
        return 1

    try:
        summary = asyncio.run(
            pipeline.run_pipeline(
                markets=args.markets,
                dry_run=args.dry_run,
                time_window_override=args.window,
            )
        )
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("Unhandled error during backfill")
        return 1
    finally:
        try:
            lock_path.unlink()
        except OSError:
            pass

    if summary.get("status") == "already_running":
        log.warning(
            "The pipeline lock was busy — regular scheduled run overlapping. "
            "Try again in a few minutes."
        )
        return 1

    coll = summary.get("collector") or {}
    ai = summary.get("ai_filter") or {}

    # v0.5.1: no flag flip. The pipeline's adaptive time-window logic keys off
    # `last_non_dry_run_at`, which run_pipeline stamps on every non-dry run —
    # including this backfill. Nothing extra to persist here.

    log.warning(
        "=== Backfill complete === new=%s scored=%s duration=%ss. "
        "Raw counts: google=%s bing=%s gdelt=%s. Duplicates=%s. "
        "Check the dashboard for the new article count.",
        coll.get("total_new", 0),
        ai.get("scored", 0) if isinstance(ai, dict) else 0,
        summary.get("duration_seconds"),
        coll.get("raw_google_news", 0),
        coll.get("raw_bing_news", 0),
        coll.get("raw_gdelt", 0),
        coll.get("duplicates", 0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""CLI entry point for a full collection run (thin wrapper around app.pipeline).

Usage:
    python scripts/run_collection.py                     # full pipeline
    python scripts/run_collection.py --dry-run           # collector only, no AI
    python scripts/run_collection.py --markets Italy,EU  # restrict to markets

Without --markets the run uses the installation's home markets
(runtime setting; unset = all markets).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import List, Optional

# Make the project importable when run as a script
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import pipeline  # noqa: E402
from app.config import DATA_DIR, LOG_PATH, VALID_MARKETS  # noqa: E402


def _setup_logging() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    file_handler = RotatingFileHandler(LOG_PATH, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.setLevel(logging.INFO)

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.setLevel(logging.WARNING)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avoid double handlers when re-imported
    root.handlers.clear()
    root.addHandler(file_handler)
    root.addHandler(console)


def _parse_markets(raw: str) -> List[str]:
    """argparse ``type=`` hook: "Italy,EU" → ["Italy", "EU"].

    Raises ArgumentTypeError on unknown or empty market lists — argparse
    turns that into exit code 2 plus a usage message.
    """
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


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a Förder-Monitor collection cycle.")
    parser.add_argument("--dry-run", action="store_true", help="collector only, skip pre-filter and AI filter")
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
    return parser.parse_args(argv)


def main() -> int:
    _setup_logging()
    args = _parse_args()
    log = logging.getLogger("run_collection")
    try:
        summary = asyncio.run(
            pipeline.run_pipeline(markets=args.markets, dry_run=args.dry_run)
        )
    except KeyboardInterrupt:
        return 130
    except Exception:
        log.exception("Unhandled error during collection run")
        return 1

    if summary.get("status") == "already_running":
        log.warning("A collection run is already active; nothing started.")
        return 1

    collector_stats = summary.get("collector") or {}
    ai_stats = summary.get("ai_filter") or {}
    log.info(
        "Run complete · new=%s · scored=%s · duration=%ss",
        collector_stats.get("total_new", 0),
        ai_stats.get("scored", 0),
        summary.get("duration_seconds"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

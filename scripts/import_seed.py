"""Thin CLI wrapper around ``app.seed_import.import_seed_file``.

Usage:
    python scripts/import_seed.py seeds/dach.json
    python scripts/import_seed.py seeds/dach.json seeds/eu.json
    python scripts/import_seed.py seeds/dach.json --reset

By default the import is IDEMPOTENT MERGE — running twice is a no-op on the
second run, and user-edited rows survive. ``--reset`` wipes each config table
before importing (dev-only).

Exit codes:
    0 — success (all files imported)
    1 — one or more files missing or contained invalid JSON
    2 — bad arguments
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import seed_import  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Import one or more seed packs into the local SQLite config tables.",
    )
    parser.add_argument("seeds", nargs="+", type=Path, help="Seed JSON files to import")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Wipe each config table before the first import (dev-only)",
    )
    args = parser.parse_args()

    # Validate all files exist up-front so we don't half-import.
    missing = [p for p in args.seeds if not p.exists()]
    if missing:
        for p in missing:
            print(f"Seed file not found: {p}", file=sys.stderr)
        return 1

    mode = "reset" if args.reset else "merge"
    exit_code = 0

    for i, seed_path in enumerate(args.seeds):
        # Only the FIRST file uses the requested mode; subsequent files always
        # merge (otherwise `--reset a.json b.json` would drop a.json's rows).
        effective_mode = mode if i == 0 else "merge"
        try:
            report = seed_import.import_seed_file(seed_path, mode=effective_mode)
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"Failed to import {seed_path}: {exc}", file=sys.stderr)
            exit_code = 1
            continue

        print(f"\n{seed_path} (mode={effective_mode}):")
        for table in ("keywords", "feeds", "outlets", "negative_keywords"):
            r = report.get(table, {})
            print(
                f"  {table:<18} inserted={r.get('inserted', 0):>4}  "
                f"skipped={r.get('skipped', 0):>4}"
            )
        total = report.get("total", {})
        print(
            f"  {'TOTAL':<18} inserted={total.get('inserted', 0):>4}  "
            f"skipped={total.get('skipped', 0):>4}"
        )

    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

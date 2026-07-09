"""One-shot cleanup of URL-wrapper duplicates in an existing monitor.db.

Background
----------
Before v0.7.1 the collector inserted Bing-News click-through URLs
(``bing.com/apiclick.aspx?url=<real>&tid=<random>``) verbatim. Every
keyword query produced a fresh ``tid=`` value for the same underlying
article, so the DB's ``UNIQUE(url)`` constraint never fired. On Jordan's
first live install this inflated the DB by ~5× and made the same story
appear 30+ times in the dashboard.

v0.7.1 fixes the collector so new inserts are already unwrapped. This
script cleans up the historical rows: it maps every wrapper URL to its
canonical target, keeps the earliest row per canonical URL, and marks
the rest ``ignored=1`` with a ``filter_reason='v0.7.1 dedup'`` so they
disappear from the dashboard without being physically deleted (fully
reversible).

Usage
-----
::

    # Dry-run: print what would happen, no writes.
    python scripts/dedupe_existing.py --dry-run

    # Apply: mark duplicate rows as ignored + record filter_reason.
    python scripts/dedupe_existing.py --apply

    # Undo (marks rows with filter_reason='v0.7.1 dedup' back to ignored=0):
    python scripts/dedupe_existing.py --undo

Only ``ignored`` + ``filter_reason`` are touched — no physical DELETE,
no schema change. Starred / read / feedback / golden-set data is
untouched even on rows that get hidden.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Make ``app`` importable when the script is run directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from app import url_canon  # noqa: E402


_MARKER = "v0.7.1 dedup"


def _db_path() -> Path:
    """Resolve the DB the app itself would use, respecting FM_DATA_DIR.

    Falls back to ``<repo>/data/monitor.db`` if FM_DATA_DIR is unset.
    """
    override = os.environ.get("FM_DATA_DIR")
    if override:
        return Path(override) / "monitor.db"
    return _REPO_ROOT / "data" / "monitor.db"


def _canon(url: str) -> str:
    """Best-effort canonicalisation, network-free (Bing only).

    Google-News URLs pass through unchanged — matching what the v0.7.1
    collector does.
    """
    return url_canon.canonicalise_url(url, follow_redirects=False)


def _plan(conn: sqlite3.Connection) -> tuple[dict[str, list[int]], int, int]:
    """Return (canonical_url -> [row_ids in insert order], total_touched,
    total_hidden)."""
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        """
        SELECT id, url, collected_at, is_filtered_out, filter_reason, starred
        FROM articles
        ORDER BY collected_at ASC, id ASC
        """
    )
    groups: dict[str, list[int]] = {}
    already_marked = 0
    for row in cur:
        canonical = _canon(row["url"])
        groups.setdefault(canonical, []).append(row["id"])
        if row["filter_reason"] == _MARKER:
            already_marked += 1

    to_hide = sum(len(ids) - 1 for ids in groups.values() if len(ids) > 1)
    return groups, already_marked, to_hide


def _dry_run(conn: sqlite3.Connection) -> None:
    groups, already, to_hide = _plan(conn)
    total = sum(len(v) for v in groups.values())
    distinct = len(groups)
    print(f"Rows in articles table       : {total}")
    print(f"Distinct canonical URLs      : {distinct}")
    print(f"Rows already marked v0.7.1   : {already}")
    print(f"Rows that would be hidden    : {to_hide}")
    print()
    print("Top 10 collision groups (canonical URL, hidden-count):")
    groups_sorted = sorted(groups.items(), key=lambda kv: len(kv[1]), reverse=True)[:10]
    for canon, ids in groups_sorted:
        if len(ids) <= 1:
            continue
        print(f"  keep #{ids[0]}  hide {len(ids) - 1:3d}  {canon[:100]}")
    print()
    print("Dry-run only. Re-run with --apply to write.")


def _apply(conn: sqlite3.Connection) -> None:
    groups, already, to_hide = _plan(conn)
    if to_hide == 0:
        print("Nothing to do — no wrapper duplicates found. Exiting.")
        return

    # Also update the article URL of the survivor to its canonical form
    # so the dashboard's "Open" links point to the publisher, not the
    # aggregator wrapper. Guarded by ``INSERT OR IGNORE`` semantics:
    # if a canonical URL already exists in the DB pointing at a different
    # row (rare — happens when a non-wrapper row was collected earlier
    # from an RSS feed of the same article), we don't crash the run.
    updated_survivor_urls = 0
    updated_hidden_rows = 0

    for canonical, ids in groups.items():
        if len(ids) <= 1:
            continue
        survivor_id, *dupe_ids = ids

        # Try to update the survivor's URL to the canonical form.
        try:
            cur = conn.execute(
                "UPDATE articles SET url = ? WHERE id = ? AND url != ?",
                (canonical, survivor_id, canonical),
            )
            if cur.rowcount:
                updated_survivor_urls += 1
        except sqlite3.IntegrityError:
            # UNIQUE(url) — another row already has this canonical URL.
            # Leave the survivor URL as-is; the dupe-hiding step still runs.
            pass

        # Mark all dupes as filtered out.
        placeholders = ",".join("?" for _ in dupe_ids)
        conn.execute(
            f"""
            UPDATE articles
            SET is_filtered_out = 1,
                filter_reason = ?
            WHERE id IN ({placeholders})
              AND (filter_reason IS NULL OR filter_reason != ?)
            """,
            (_MARKER, *dupe_ids, _MARKER),
        )
        updated_hidden_rows += len(dupe_ids)

    conn.commit()
    print(f"Rows already marked v0.7.1     : {already}")
    print(f"Rows marked now (duplicates)   : {updated_hidden_rows}")
    print(f"Survivor URLs canonicalised    : {updated_survivor_urls}")
    print(f"Groups processed               : {sum(1 for ids in groups.values() if len(ids) > 1)}")
    print()
    print("Done. Re-launch the app; the dashboard row count will drop accordingly.")
    print(f"To reverse: python scripts/dedupe_existing.py --undo")


def _undo(conn: sqlite3.Connection) -> None:
    """Clear the ``v0.7.1 dedup`` marker so the hidden rows re-appear."""
    cur = conn.execute(
        """
        UPDATE articles
        SET is_filtered_out = 0,
            filter_reason = NULL
        WHERE filter_reason = ?
        """,
        (_MARKER,),
    )
    conn.commit()
    print(f"Reverted {cur.rowcount} rows to visible state.")
    print("Note: this does NOT restore the original wrapper URLs on survivor rows.")
    print("If that matters, restore from your DB backup instead.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--dry-run", action="store_true", help="Print plan, no writes.")
    grp.add_argument("--apply", action="store_true", help="Mark duplicates as filtered_out.")
    grp.add_argument("--undo", action="store_true", help="Revert a prior --apply run.")
    ap.add_argument("--db", type=Path, default=None, help="Override DB path (defaults to FM_DATA_DIR/monitor.db).")
    args = ap.parse_args()

    db = args.db or _db_path()
    if not db.exists():
        sys.exit(f"error: DB not found at {db}")

    print(f"DB: {db}")
    print()
    with sqlite3.connect(db) as conn:
        if args.dry_run:
            _dry_run(conn)
        elif args.apply:
            _apply(conn)
        elif args.undo:
            _undo(conn)


if __name__ == "__main__":
    main()

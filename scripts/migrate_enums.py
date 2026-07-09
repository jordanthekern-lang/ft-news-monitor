"""Enum migration: rewrite German enum values to canonical English.

Idempotent: running twice produces the same result — the second run reports
all rows as 'unchanged'.

Migrates:
  articles.category          — via _CATEGORY_ALIASES
  articles.themes            — JSON list, each entry via _THEME_ALIASES
  keywords.prioritaet        — via _PRIORITY_ALIASES
  keywords.themenbereich     — JSON list, via _THEME_ALIASES
  feeds.prioritaet           — via _PRIORITY_ALIASES
  feeds.themenbereich        — JSON list, via _THEME_ALIASES
  outlets.prioritaet         — via _PRIORITY_ALIASES
  outlets.themenbereich      — JSON list, via _THEME_ALIASES
  outlets.outlet_typ         — via _OUTLET_TYPE_ALIASES
  negative_keywords.kategorie — via _NEGATIVE_CATEGORY_ALIASES
  negative_keywords.markt    — JSON list; 'Alle' sentinel → 'All'

Usage:
  python scripts/migrate_enums.py [--db-path PATH]
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from pathlib import Path

# Make project importable when run as a script
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.config import DB_PATH
from app.config import _CATEGORY_ALIASES, _THEME_ALIASES
from app.enums import _PRIORITY_ALIASES, _OUTLET_TYPE_ALIASES, _NEGATIVE_CATEGORY_ALIASES

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Market sentinel migration: "Alle" → "All"
# ---------------------------------------------------------------------------

_MARKET_ALIASES: dict[str, str] = {
    "Alle": "All",
}


def _migrate_scalar(value: str | None, alias_map: dict[str, str]) -> tuple[str | None, bool]:
    """Return (migrated_value, changed). Uses case-insensitive lookup on alias keys."""
    if value is None:
        return value, False
    # Try exact match first, then case-insensitive
    if value in alias_map:
        new = alias_map[value]
        return new, new != value
    lower = value.strip().lower()
    for k, v in alias_map.items():
        if k.lower() == lower:
            return v, v != value
    return value, False


def _migrate_json_list(raw: str | None, alias_map: dict[str, str]) -> tuple[str | None, bool]:
    """Migrate each element of a JSON-encoded list through alias_map."""
    if not raw:
        return raw, False
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw, False
    if not isinstance(items, list):
        return raw, False

    new_items = []
    changed = False
    for item in items:
        migrated, item_changed = _migrate_scalar(str(item), alias_map)
        new_items.append(migrated)
        if item_changed:
            changed = True
    if not changed:
        return raw, False
    return json.dumps(new_items, ensure_ascii=False), True


# ---------------------------------------------------------------------------
# Per-table migration plans
# ---------------------------------------------------------------------------

# Each plan: (table, [(column, mode, alias_map), ...])
# mode: "scalar" | "json_list"
# alias_map: dict used for migration

def _build_plans() -> list[tuple[str, list[tuple[str, str, dict]]]]:
    return [
        ("articles", [
            ("category", "scalar", _CATEGORY_ALIASES),
            ("themes", "json_list", _THEME_ALIASES),
        ]),
        ("keywords", [
            ("prioritaet", "scalar", _PRIORITY_ALIASES),
            ("themenbereich", "json_list", _THEME_ALIASES),
        ]),
        ("feeds", [
            ("prioritaet", "scalar", _PRIORITY_ALIASES),
            ("themenbereich", "json_list", _THEME_ALIASES),
        ]),
        ("outlets", [
            ("prioritaet", "scalar", _PRIORITY_ALIASES),
            ("themenbereich", "json_list", _THEME_ALIASES),
            ("outlet_typ", "scalar", _OUTLET_TYPE_ALIASES),
        ]),
        ("negative_keywords", [
            ("kategorie", "scalar", _NEGATIVE_CATEGORY_ALIASES),
            ("markt", "json_list", _MARKET_ALIASES),
        ]),
    ]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    )
    return cur.fetchone() is not None


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    cur = conn.execute(f"PRAGMA table_info({table})")
    return any(row[1] == column for row in cur.fetchall())


def migrate(db_path: Path = DB_PATH) -> dict[str, dict[str, int]]:
    """Run the migration. Returns per-table {updated, unchanged} counts."""
    results: dict[str, dict[str, int]] = {}

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        for table, columns in _build_plans():
            if not _table_exists(conn, table):
                logger.info("migrate_enums: table '%s' not found — skipping", table)
                results[table] = {"updated": 0, "unchanged": 0}
                continue

            updated = 0
            unchanged = 0

            # Fetch all rows — migration touches only enum-valued columns
            rows = conn.execute(f"SELECT id, {', '.join(c for c, _, _ in columns)} FROM {table}").fetchall()
            for row in rows:
                row_id = row["id"]
                new_vals: dict[str, str | None] = {}
                any_changed = False

                for col, mode, alias_map in columns:
                    if not _column_exists(conn, table, col):
                        continue
                    raw = row[col]
                    if mode == "scalar":
                        new_val, changed = _migrate_scalar(raw, alias_map)
                    else:  # json_list
                        new_val, changed = _migrate_json_list(raw, alias_map)
                    new_vals[col] = new_val
                    if changed:
                        any_changed = True

                if any_changed:
                    set_clause = ", ".join(f"{c} = ?" for c in new_vals)
                    values = list(new_vals.values()) + [row_id]
                    conn.execute(f"UPDATE {table} SET {set_clause} WHERE id = ?", values)
                    updated += 1
                else:
                    unchanged += 1

            results[table] = {"updated": updated, "unchanged": unchanged}
            logger.info(
                "migrate_enums: %s — updated=%d, unchanged=%d",
                table, updated, unchanged,
            )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return results


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Migrate German enum values to canonical English.")
    parser.add_argument("--db-path", type=Path, default=DB_PATH, help="Path to monitor.db")
    args = parser.parse_args()

    print(f"Migrating: {args.db_path}")
    results = migrate(args.db_path)
    for table, counts in results.items():
        print(f"  {table}: updated={counts['updated']}, unchanged={counts['unchanged']}")
    print("Done.")


if __name__ == "__main__":
    main()

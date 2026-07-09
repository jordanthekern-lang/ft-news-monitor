"""Idempotent seed importer used by the setup wizard and the CLI helper.

The wizard calls ``import_seed_file(seeds/<market>.json, mode='merge')`` when
the user picks a market. Filenames are lowercase and match a member of
``app.config.VALID_MARKETS`` (dach, benelux, france, italy, nordics, portugal,
eu).

Two modes:

- ``merge`` (default): only INSERT rows whose natural key isn't already in the
  target table. User edits to existing rows are preserved — the importer never
  UPDATEs or DELETEs. Safe to re-run.
- ``reset``: wipe each of the four config tables, then bulk-insert everything
  from the seed. Dev-only.

Return shape (contract; the wizard reads this):

    {
      "keywords":          {"inserted": N, "skipped": N, "markt_unioned": N},
      "feeds":             {"inserted": N, "skipped": N, "markt_unioned": N},
      "outlets":           {"inserted": N, "skipped": N, "markt_unioned": N},
      "negative_keywords": {"inserted": N, "skipped": N, "markt_unioned": N},
      "total":             {"inserted": N, "skipped": N, "markt_unioned": N},
    }

``markt_unioned`` counts rows where the incoming seed row's natural key
matched an existing stored row and its ``markt`` list contributed new
markets to the stored row (a UPDATE was issued). Only ``keywords`` and
``negative_keywords`` ever produce non-zero ``markt_unioned``; ``feeds``
and ``outlets`` keep pure-skip merge semantics. A markt-unioned row still
counts as ``skipped`` (nothing was inserted).

Unknown top-level keys in the seed (``market``, ``seed_version``,
``exported_at``, …) are ignored.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Tuple

from . import config as _config
from . import database


TABLE_KEYS: Tuple[str, ...] = ("keywords", "feeds", "outlets", "negative_keywords")


# ---------------------------------------------------------------------------
# Per-table normalisation + natural-key helpers
# ---------------------------------------------------------------------------

def _norm_keyword(row: dict) -> dict:
    return {
        "suchbegriff": (row.get("suchbegriff") or "").strip(),
        "markt": row.get("markt") or [],
        "themenbereich": row.get("themenbereich") or [],
        "prioritaet": row.get("prioritaet"),
        "sprache": row.get("sprache"),
        "notizen": row.get("notizen"),
    }


def _norm_feed(row: dict) -> dict:
    return {
        "rss_feed": (row.get("rss_feed") or "").strip(),
        "typ": row.get("typ"),
        "markt": row.get("markt") or [],
        "themenbereich": row.get("themenbereich") or [],
        "prioritaet": row.get("prioritaet"),
        "sprache": row.get("sprache"),
        "notizen": row.get("notizen"),
    }


def _norm_outlet(row: dict) -> dict:
    return {
        "outlet": (row.get("outlet") or "").strip(),
        "url": (row.get("url") or "").strip() or None,
        "markt": row.get("markt") or [],
        "themenbereich": row.get("themenbereich") or [],
        "prioritaet": row.get("prioritaet"),
        "sprache": row.get("sprache"),
        "outlet_typ": row.get("outlet_typ"),
        "hat_rss": bool(row.get("hat_rss")),
        "notizen": row.get("notizen"),
    }


def _norm_negative(row: dict) -> dict:
    return {
        "begriff": (row.get("begriff") or "").strip(),
        "markt": row.get("markt") or [],
        "kategorie": row.get("kategorie"),
        "notizen": row.get("notizen"),
    }


_NORMALISERS: Dict[str, Callable[[dict], dict]] = {
    "keywords": _norm_keyword,
    "feeds": _norm_feed,
    "outlets": _norm_outlet,
    "negative_keywords": _norm_negative,
}


def _row_has_key(table: str, row: dict) -> bool:
    """A seed row is skipped silently if it lacks the primary identifier."""
    if table == "keywords":
        return bool(row.get("suchbegriff"))
    if table == "feeds":
        return bool(row.get("rss_feed"))
    if table == "outlets":
        return bool(row.get("outlet"))
    if table == "negative_keywords":
        return bool(row.get("begriff"))
    return False


# Natural keys — used to detect duplicates during merge import. Case-insensitive
# where it makes sense (search terms) so "E-Bus" and "e-bus" don't co-exist.

def _natural_key_keyword(row: dict) -> Tuple[str, str]:
    return ((row.get("suchbegriff") or "").strip().lower(),
            (row.get("sprache") or "").strip().upper())


def _natural_key_feed(row: dict) -> str:
    return (row.get("rss_feed") or "").strip()


def _natural_key_outlet(row: dict) -> str:
    """URL when present, otherwise fall back to the outlet name (both lowered).

    Case-insensitive and trailing-slash-insensitive on the URL so
    ``"https://Example.com/"`` and ``"https://example.com"`` hash identical.
    """
    url = (row.get("url") or "").strip().lower().rstrip("/")
    if url:
        return f"url:{url}"
    return f"name:{(row.get('outlet') or '').strip().lower()}"


def _natural_key_negative(row: dict) -> str:
    return (row.get("begriff") or "").strip().lower()


_KEY_FUNCS: Dict[str, Callable[[dict], Any]] = {
    "keywords": _natural_key_keyword,
    "feeds": _natural_key_feed,
    "outlets": _natural_key_outlet,
    "negative_keywords": _natural_key_negative,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def import_seed_file(
    path: str | Path,
    mode: str = "merge",
    *,
    db_path: Path | None = None,
) -> Dict[str, Dict[str, int]]:
    """Import a seed pack into the local SQLite config tables.

    ``mode``:
        - ``"merge"`` — INSERT rows whose natural key isn't already stored;
          existing rows are preserved untouched. Idempotent.
        - ``"reset"`` — wipe each config table, then bulk-INSERT the seed.

    Returns per-table ``{"inserted": n, "skipped": n, "markt_unioned": n}``
    plus a ``"total"`` aggregate. ``markt_unioned`` counts collisions on
    ``keywords`` / ``negative_keywords`` where the stored row's ``markt`` was
    updated to include markets from the incoming row.
    """
    if mode not in {"merge", "reset"}:
        raise ValueError(f"Unknown import mode: {mode!r}")

    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Seed file must contain a JSON object, got {type(payload).__name__}")

    # Resolve DB_PATH lazily so pytest's monkeypatched value is picked up.
    active_db_path = db_path if db_path is not None else _config.DB_PATH
    database.init_db(active_db_path)

    if mode == "reset":
        _reset_tables(active_db_path)

    report: Dict[str, Dict[str, int]] = {}
    for table in TABLE_KEYS:
        rows = payload.get(table) or []
        report[table] = _import_table(table, rows, mode=mode, db_path=active_db_path)

    total_inserted = sum(v["inserted"] for v in report.values())
    total_skipped = sum(v["skipped"] for v in report.values())
    total_unioned = sum(v.get("markt_unioned", 0) for v in report.values())
    report["total"] = {
        "inserted": total_inserted,
        "skipped": total_skipped,
        "markt_unioned": total_unioned,
    }
    return report


def _reset_tables(db_path: Path) -> None:
    """Wipe all four config tables in one transaction."""
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        for table in TABLE_KEYS:
            conn.execute(f"DELETE FROM {table}")
        conn.commit()


# ---------------------------------------------------------------------------
# v0.4.0 — Auto-reseed on startup
# ---------------------------------------------------------------------------

def _read_seed_version(path: Path) -> int:
    """Return the ``seed_version`` int from a seed file, or 0 if missing/invalid.

    The importer itself ignores the field; only the auto-reseeder uses it to
    decide whether a merge run is needed.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    raw = payload.get("seed_version") if isinstance(payload, dict) else None
    if isinstance(raw, int) and raw >= 0:
        return raw
    return 0


def auto_reseed_home_markets(
    *,
    seeds_dir: Path | None = None,
    db_path: Path | None = None,
) -> Dict[str, Dict[str, Any]]:
    """Merge seed packs for every home market whose shipped ``seed_version``
    exceeds what we've already imported.

    Called from the FastAPI lifespan after ``database.init_db()``. No-op unless:

    * ``setup_completed`` is truthy — a Fresh install goes through the wizard,
      which runs its own explicit import step. We don't want to sneak seeds in
      before the user has confirmed their market selection.
    * ``home_markets`` is non-empty.
    * At least one seed file has a higher ``seed_version`` than the value
      stored under :data:`runtime_settings.IMPORTED_SEED_VERSIONS`.

    Merge semantics are inherited from :func:`import_seed_file` (``mode='merge'``):
    only new natural keys are inserted, existing rows stay untouched. User edits
    survive. The ``imported_seed_versions`` marker is only bumped when a file was
    actually processed.

    Returns a per-market report ``{"dach": {"seed_version": 2, "report": {...}}}``.
    Missing files are reported as ``{"skipped": "not available"}`` (helpful when
    a user picks a market with no shipped seed).
    """
    from . import runtime_settings

    if not runtime_settings.get(runtime_settings.SETUP_COMPLETED):
        return {}

    home = runtime_settings.effective_home_markets(db_path=db_path)
    if not home:
        return {}

    seeds_dir = seeds_dir or (_config.PROJECT_ROOT / "seeds")
    imported: dict = dict(
        runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS, {}, db_path=db_path) or {}
    )

    result: Dict[str, Dict[str, Any]] = {}
    changed = False

    for market in home:
        slug = market.lower()
        seed_path = seeds_dir / f"{slug}.json"
        if not seed_path.exists():
            result[slug] = {"status": "not_available"}
            continue

        shipped_version = _read_seed_version(seed_path)
        stored_version = int(imported.get(slug) or 0)
        if shipped_version <= stored_version:
            result[slug] = {
                "status": "up_to_date",
                "seed_version": shipped_version,
            }
            continue

        report = import_seed_file(seed_path, mode="merge", db_path=db_path)
        imported[slug] = shipped_version
        changed = True
        result[slug] = {
            "status": "imported",
            "seed_version": shipped_version,
            "previous_version": stored_version,
            "report": report,
        }

    if changed:
        runtime_settings.set(
            runtime_settings.IMPORTED_SEED_VERSIONS, imported, db_path=db_path
        )
    return result


def mark_seed_imported(
    market: str,
    seed_version: int,
    *,
    db_path: Path | None = None,
) -> None:
    """Record that ``seeds/<market>.json`` version ``seed_version`` was imported.

    Called from the setup wizard's manual import step so the auto-reseeder
    doesn't re-run the same merge on the next startup. Safe to call with
    ``seed_version=0`` (no-op) or a value lower than what's already stored
    (kept at max).
    """
    from . import runtime_settings

    if seed_version <= 0:
        return
    slug = market.strip().lower()
    if not slug:
        return
    imported: dict = dict(
        runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS, {}, db_path=db_path) or {}
    )
    current = int(imported.get(slug) or 0)
    if seed_version > current:
        imported[slug] = seed_version
        runtime_settings.set(
            runtime_settings.IMPORTED_SEED_VERSIONS, imported, db_path=db_path
        )


def _existing_rows_by_key(table: str, db_path: Path) -> Dict[Any, dict]:
    """Return a natural-key → stored-row map for ``table``."""
    key_fn = _KEY_FUNCS[table]
    out: Dict[Any, dict] = {}
    for row in database.list_config(table, db_path=db_path):
        out[key_fn(row)] = row
    return out


# Tables where a natural-key collision unions the incoming ``markt`` into the
# stored row instead of silently dropping it. Kept narrow on purpose:
# ``feeds`` are per-URL and ``outlets`` are globally-shared entities, so
# unioning markets there would be surprising.
_MARKT_UNION_TABLES = frozenset({"keywords", "negative_keywords"})


def _import_table(
    table: str,
    rows: List[dict],
    *,
    mode: str,
    db_path: Path,
) -> Dict[str, int]:
    normaliser = _NORMALISERS[table]
    key_fn = _KEY_FUNCS[table]

    inserted = 0
    skipped = 0
    markt_unioned = 0

    existing_by_key: Dict[Any, dict] = (
        _existing_rows_by_key(table, db_path) if mode == "merge" else {}
    )

    # Track keys inserted during THIS run so a seed with dupes doesn't
    # double-insert them.
    seen_this_run: set = set()

    for raw in rows:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        row = normaliser(raw)
        if not _row_has_key(table, row):
            skipped += 1
            continue
        key = key_fn(row)
        if key in existing_by_key or key in seen_this_run:
            skipped += 1
            # For keywords / negative_keywords, union the incoming markt into
            # the stored row so per-market seed research (T8/T9) doesn't lose
            # its market tag when it collides with an already-stored term.
            if (
                mode == "merge"
                and table in _MARKT_UNION_TABLES
                and key in existing_by_key
            ):
                stored = existing_by_key[key]
                stored_markt = list(stored.get("markt") or [])
                incoming_markt = list(row.get("markt") or [])
                if incoming_markt:
                    # Preserve stored order; append any new entries in the
                    # order they appear in the incoming row.
                    merged = list(stored_markt)
                    for m in incoming_markt:
                        if m not in merged:
                            merged.append(m)
                    if merged != stored_markt:
                        updated_row = dict(stored)
                        updated_row["markt"] = merged
                        # Strip fields that aren't stored via update_config_row
                        # helper (it re-adds updated_at itself).
                        updated_row.pop("id", None)
                        updated_row.pop("created_at", None)
                        updated_row.pop("updated_at", None)
                        database.update_config_row(
                            table, int(stored["id"]), updated_row, db_path=db_path
                        )
                        # Keep our local view in sync so a second collision in
                        # the same run doesn't re-issue an identical UPDATE.
                        stored["markt"] = merged
                        markt_unioned += 1
            continue
        database.insert_config_row(table, row, db_path=db_path)
        seen_this_run.add(key)
        inserted += 1

    return {"inserted": inserted, "skipped": skipped, "markt_unioned": markt_unioned}

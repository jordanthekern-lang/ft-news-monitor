"""Tests for the v0.4.0 startup auto-reseeder.

Contract: on FastAPI lifespan, ``seed_import.auto_reseed_home_markets`` merges
every shipped seed pack whose ``seed_version`` exceeds the value stored under
``runtime_settings.IMPORTED_SEED_VERSIONS``. User edits are preserved (delegated
to the underlying merge-mode importer).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import database, runtime_settings, seed_import


def _write_seed(path: Path, market: str, version: int, keyword: str) -> None:
    payload = {
        "market": market,
        "seed_version": version,
        "keywords": [
            {
                "suchbegriff": keyword,
                "markt": [market],
                "themenbereich": ["E-buses"],
                "prioritaet": "High",
                "sprache": "DE" if market == "DACH" else "EN",
            }
        ],
        "feeds": [],
        "outlets": [],
        "negative_keywords": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_seeds_dir(tmp_path: Path) -> Path:
    seeds = tmp_path / "seeds"
    seeds.mkdir()
    return seeds


def test_auto_reseed_noop_when_setup_incomplete(tmp_path, fresh_setup):
    """Fresh installs (wizard not finished) must not sneak seeds in."""
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "Test DACH keyword")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH"])

    result = seed_import.auto_reseed_home_markets(seeds_dir=seeds)

    assert result == {}
    assert database.list_config("keywords") == []


def test_auto_reseed_noop_when_home_markets_empty(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "Test DACH keyword")
    # No home_markets set → auto-reseed is a no-op.

    result = seed_import.auto_reseed_home_markets(seeds_dir=seeds)
    assert result == {}
    assert database.list_config("keywords") == []


def test_auto_reseed_imports_when_version_higher(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "Fresh DACH keyword")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH"])

    result = seed_import.auto_reseed_home_markets(seeds_dir=seeds)

    assert result["dach"]["status"] == "imported"
    assert result["dach"]["seed_version"] == 2
    assert result["dach"]["previous_version"] == 0
    assert result["dach"]["report"]["total"]["inserted"] == 1
    rows = database.list_config("keywords")
    assert len(rows) == 1
    assert rows[0]["suchbegriff"] == "Fresh DACH keyword"
    # Marker persisted for next startup.
    stored = runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS)
    assert stored == {"dach": 2}


def test_auto_reseed_skips_up_to_date(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "DACH kw")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH"])
    runtime_settings.set(runtime_settings.IMPORTED_SEED_VERSIONS, {"dach": 2})

    result = seed_import.auto_reseed_home_markets(seeds_dir=seeds)
    assert result["dach"]["status"] == "up_to_date"
    # No merge attempted → DB stays empty.
    assert database.list_config("keywords") == []


def test_auto_reseed_second_run_after_version_bump(tmp_path):
    """Simulates a v0.4.0 → v0.5.0 update: seed_version bumped, one new row
    added. Auto-reseeder should merge the new row without touching existing
    ones, and bump the marker."""
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "Original DACH kw")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH"])

    seed_import.auto_reseed_home_markets(seeds_dir=seeds)
    assert len(database.list_config("keywords")) == 1

    # v3 payload: keeps the first row and adds a second.
    payload_v3 = {
        "market": "DACH",
        "seed_version": 3,
        "keywords": [
            {
                "suchbegriff": "Original DACH kw",
                "markt": ["DACH"],
                "themenbereich": ["E-buses"],
                "prioritaet": "High",
                "sprache": "DE",
            },
            {
                "suchbegriff": "Added in v3",
                "markt": ["DACH"],
                "themenbereich": ["E-buses"],
                "prioritaet": "Medium",
                "sprache": "DE",
            },
        ],
        "feeds": [],
        "outlets": [],
        "negative_keywords": [],
    }
    (seeds / "dach.json").write_text(json.dumps(payload_v3), encoding="utf-8")

    result = seed_import.auto_reseed_home_markets(seeds_dir=seeds)
    assert result["dach"]["status"] == "imported"
    assert result["dach"]["seed_version"] == 3
    assert result["dach"]["previous_version"] == 2
    assert result["dach"]["report"]["total"]["inserted"] == 1  # only the new row

    rows = database.list_config("keywords")
    assert {row["suchbegriff"] for row in rows} == {"Original DACH kw", "Added in v3"}
    assert runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS) == {"dach": 3}


def test_auto_reseed_preserves_user_edits(tmp_path):
    """User modifies a Seed-imported row; a subsequent version-bump must
    NOT overwrite the user's edit (that's the whole point of merge-mode)."""
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "Kw prio original")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH"])
    seed_import.auto_reseed_home_markets(seeds_dir=seeds)
    (row_id,) = [r["id"] for r in database.list_config("keywords")]

    # User edits the priority.
    database.update_config_row(
        "keywords",
        row_id,
        {
            "suchbegriff": "Kw prio original",
            "markt": ["DACH"],
            "themenbereich": ["E-buses"],
            "prioritaet": "Low",  # user-changed from High
            "sprache": "DE",
            "notizen": "user edit",
        },
    )

    # Ship a v3 seed with the ORIGINAL priority.
    _write_seed(seeds / "dach.json", "DACH", 3, "Kw prio original")
    seed_import.auto_reseed_home_markets(seeds_dir=seeds)

    rows = database.list_config("keywords")
    assert len(rows) == 1
    assert rows[0]["prioritaet"] == "Low"  # user edit survived
    assert rows[0]["notizen"] == "user edit"


def test_auto_reseed_missing_seed_file_reports_not_available(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    # No file at seeds/italy.json exists.
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["Italy"])

    result = seed_import.auto_reseed_home_markets(seeds_dir=seeds)
    assert result == {"italy": {"status": "not_available"}}
    # Marker NOT bumped when nothing was imported.
    assert runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS) is None


def test_auto_reseed_multiple_home_markets(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "DACH kw")
    _write_seed(seeds / "eu.json", "EU", 2, "EU kw")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH", "EU"])

    result = seed_import.auto_reseed_home_markets(seeds_dir=seeds)
    assert result["dach"]["status"] == "imported"
    assert result["eu"]["status"] == "imported"
    assert runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS) == {
        "dach": 2,
        "eu": 2,
    }
    rows = database.list_config("keywords")
    assert len(rows) == 2


def test_auto_reseed_marker_not_bumped_when_up_to_date(tmp_path):
    """If nothing needs importing, we don't churn the marker's updated_at."""
    seeds = _make_seeds_dir(tmp_path)
    _write_seed(seeds / "dach.json", "DACH", 2, "DACH kw")
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH"])
    runtime_settings.set(runtime_settings.IMPORTED_SEED_VERSIONS, {"dach": 2, "italy": 5})

    seed_import.auto_reseed_home_markets(seeds_dir=seeds)

    stored = runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS)
    # Existing italy=5 entry untouched (the "changed" flag stayed False).
    assert stored == {"dach": 2, "italy": 5}


def test_read_seed_version_missing_returns_zero(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    path = seeds / "no_version.json"
    path.write_text(json.dumps({"market": "X", "keywords": []}), encoding="utf-8")
    assert seed_import._read_seed_version(path) == 0


def test_read_seed_version_reads_int(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    path = seeds / "v.json"
    path.write_text(json.dumps({"seed_version": 7, "keywords": []}), encoding="utf-8")
    assert seed_import._read_seed_version(path) == 7


def test_read_seed_version_invalid_type_returns_zero(tmp_path):
    seeds = _make_seeds_dir(tmp_path)
    path = seeds / "bad.json"
    path.write_text(json.dumps({"seed_version": "two", "keywords": []}), encoding="utf-8")
    assert seed_import._read_seed_version(path) == 0


def test_mark_seed_imported_upserts(tmp_path):
    seed_import.mark_seed_imported("DACH", 2)
    assert runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS) == {"dach": 2}
    # Second call with lower version does NOT overwrite.
    seed_import.mark_seed_imported("DACH", 1)
    assert runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS) == {"dach": 2}
    # Higher version does.
    seed_import.mark_seed_imported("DACH", 5)
    assert runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS) == {"dach": 5}


def test_mark_seed_imported_ignores_zero(tmp_path):
    seed_import.mark_seed_imported("DACH", 0)
    assert runtime_settings.get(runtime_settings.IMPORTED_SEED_VERSIONS) is None


def test_shipped_dach_eu_seeds_at_expected_version():
    """Fixture check: after v0.4.0 the shipped dach.json/eu.json declare
    seed_version 2. Guards against accidental version regressions in the
    seed files themselves."""
    from app import config as _config

    for slug in ("dach", "eu"):
        path = _config.PROJECT_ROOT / "seeds" / f"{slug}.json"
        assert seed_import._read_seed_version(path) >= 2, (
            f"seeds/{slug}.json must be at seed_version >= 2 in v0.4.0"
        )

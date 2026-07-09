"""Tests for the idempotent seed importer (Task 7).

Contract: ``app.seed_import.import_seed_file(path, mode='merge'|'reset')``
returns per-table ``{"inserted": n, "skipped": n}`` plus a "total" aggregate.
Merge is idempotent — the second run inserts zero and never mutates existing
rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import database, seed_import


def _write_seed(tmp_path: Path, payload: dict, name: str = "seed.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _base_payload() -> dict:
    return {
        "market": "TestMarket",
        "seed_version": 1,
        "keywords": [
            {
                "suchbegriff": "E-Bus Förderung",
                "markt": ["DACH"],
                "themenbereich": ["E-buses"],
                "prioritaet": "High",
                "sprache": "DE",
                "notizen": "test",
            },
            {
                "suchbegriff": "Hydrogen bus grant",
                "markt": ["EU"],
                "themenbereich": ["Hydrogen buses"],
                "prioritaet": "Medium",
                "sprache": "EN",
                "notizen": None,
            },
        ],
        "feeds": [
            {
                "rss_feed": "https://example.com/rss",
                "typ": "RSS-Feed",
                "markt": ["DACH"],
                "themenbereich": [],
                "prioritaet": "High",
                "sprache": "DE",
                "notizen": None,
            },
        ],
        "outlets": [
            {
                "outlet": "Example Trade",
                "url": "https://example.com/",
                "markt": ["DACH"],
                "themenbereich": [],
                "prioritaet": "High",
                "sprache": "DE",
                "outlet_typ": "Trade press",
                "hat_rss": True,
                "notizen": None,
            },
        ],
        "negative_keywords": [
            {"begriff": "rail freight", "markt": ["EU"], "kategorie": "Off-topic"},
        ],
    }


# ---------------------------------------------------------------------------
# Merge mode
# ---------------------------------------------------------------------------

def test_merge_first_run_inserts_all_rows(tmp_path):
    seed = _write_seed(tmp_path, _base_payload())
    report = seed_import.import_seed_file(seed, mode="merge")

    assert report["keywords"]["inserted"] == 2
    assert report["feeds"]["inserted"] == 1
    assert report["outlets"]["inserted"] == 1
    assert report["negative_keywords"]["inserted"] == 1
    assert report["total"]["inserted"] == 5
    assert report["total"]["skipped"] == 0


def test_merge_second_run_is_idempotent(tmp_path):
    seed = _write_seed(tmp_path, _base_payload())
    seed_import.import_seed_file(seed, mode="merge")
    report = seed_import.import_seed_file(seed, mode="merge")

    assert report["total"]["inserted"] == 0
    assert report["total"]["skipped"] == 5
    for table in ("keywords", "feeds", "outlets", "negative_keywords"):
        assert report[table]["inserted"] == 0


def test_merge_natural_key_case_insensitive_for_keywords(tmp_path):
    payload = _base_payload()
    seed_import.import_seed_file(_write_seed(tmp_path, payload, "s1.json"), mode="merge")

    # Same keyword with different case → treated as duplicate.
    payload2 = {
        "keywords": [
            {
                "suchbegriff": "E-BUS FÖRDERUNG",
                "sprache": "DE",
                "markt": ["DACH"],
            }
        ]
    }
    report = seed_import.import_seed_file(_write_seed(tmp_path, payload2, "s2.json"), mode="merge")
    assert report["keywords"]["inserted"] == 0
    assert report["keywords"]["skipped"] == 1


def test_merge_natural_key_language_matters_for_keywords(tmp_path):
    payload = _base_payload()
    seed_import.import_seed_file(_write_seed(tmp_path, payload, "s1.json"), mode="merge")

    # Same term, different language → separate row.
    payload2 = {
        "keywords": [
            {"suchbegriff": "E-Bus Förderung", "sprache": "EN", "markt": ["DACH"]},
        ]
    }
    report = seed_import.import_seed_file(_write_seed(tmp_path, payload2, "s2.json"), mode="merge")
    assert report["keywords"]["inserted"] == 1
    assert report["keywords"]["skipped"] == 0


def test_merge_preserves_user_edits(tmp_path):
    seed = _write_seed(tmp_path, _base_payload())
    seed_import.import_seed_file(seed, mode="merge")

    # Simulate a user edit: bump priority to "Low" and change notes.
    rows = database.list_config("keywords")
    target = next(r for r in rows if r["suchbegriff"] == "E-Bus Förderung")
    database.update_config_row(
        "keywords",
        target["id"],
        {
            "suchbegriff": target["suchbegriff"],
            "markt": target["markt"],
            "themenbereich": target["themenbereich"],
            "prioritaet": "Low",
            "sprache": target["sprache"],
            "notizen": "hand-edited",
        },
    )

    # Re-import the same seed → the row must not be reverted.
    seed_import.import_seed_file(seed, mode="merge")
    after = next(r for r in database.list_config("keywords") if r["id"] == target["id"])
    assert after["prioritaet"] == "Low"
    assert after["notizen"] == "hand-edited"


def test_merge_outlets_fall_back_to_name_when_url_empty(tmp_path):
    payload = {
        "outlets": [
            {"outlet": "NoURLOutlet", "url": "", "markt": ["DACH"]},
            {"outlet": "NoURLOutlet", "url": None, "markt": ["EU"]},  # dup by name
        ]
    }
    report = seed_import.import_seed_file(_write_seed(tmp_path, payload), mode="merge")
    assert report["outlets"]["inserted"] == 1
    assert report["outlets"]["skipped"] == 1


def test_merge_outlets_url_case_and_trailing_slash_insensitive(tmp_path):
    """URL host case and a single trailing slash must not create a duplicate."""
    payload = {
        "outlets": [
            {"outlet": "Example", "url": "https://Example.com/", "markt": ["DACH"]},
            {"outlet": "Example", "url": "https://example.com", "markt": ["EU"]},  # dup
        ]
    }
    report = seed_import.import_seed_file(_write_seed(tmp_path, payload), mode="merge")
    assert report["outlets"]["inserted"] == 1
    assert report["outlets"]["skipped"] == 1


def test_merge_negative_keywords_case_insensitive(tmp_path):
    payload = {
        "negative_keywords": [
            {"begriff": "Rail Freight", "kategorie": "Off-topic"},
            {"begriff": "rail freight", "kategorie": "Off-topic"},  # dup
        ]
    }
    report = seed_import.import_seed_file(_write_seed(tmp_path, payload), mode="merge")
    assert report["negative_keywords"]["inserted"] == 1
    assert report["negative_keywords"]["skipped"] == 1


def test_merge_keyword_collision_unions_markt(tmp_path):
    """When an incoming keyword collides with a stored row (same term+language),
    the stored row's ``markt`` list must be extended to include the incoming
    row's markets — otherwise T8/T9 per-market seed research loses tags."""
    seed1 = _write_seed(
        tmp_path,
        {
            "keywords": [
                {
                    "suchbegriff": "autocar",
                    "sprache": "FR",
                    "markt": ["DACH"],
                    "themenbereich": [],
                    "prioritaet": "Medium",
                },
            ]
        },
        "s1.json",
    )
    seed_import.import_seed_file(seed1, mode="merge")

    seed2 = _write_seed(
        tmp_path,
        {
            "keywords": [
                {
                    "suchbegriff": "autocar",
                    "sprache": "FR",
                    "markt": ["Italy"],
                    "themenbereich": [],
                    "prioritaet": "Medium",
                },
            ]
        },
        "s2.json",
    )
    report = seed_import.import_seed_file(seed2, mode="merge")
    assert report["keywords"]["inserted"] == 0
    assert report["keywords"]["skipped"] == 1
    assert report["keywords"]["markt_unioned"] == 1

    rows = database.list_config("keywords")
    match = [r for r in rows if r["suchbegriff"] == "autocar"]
    assert len(match) == 1
    assert set(match[0]["markt"]) == {"DACH", "Italy"}

    # A third run with the same incoming markt must not re-issue the UPDATE.
    report2 = seed_import.import_seed_file(seed2, mode="merge")
    assert report2["keywords"]["markt_unioned"] == 0


def test_merge_ignores_unknown_top_level_keys(tmp_path):
    payload = _base_payload()
    payload["exported_at"] = "2026-01-01T00:00:00Z"
    payload["author"] = "someone"
    payload["market"] = "Custom"
    seed = _write_seed(tmp_path, payload)
    # Should not raise.
    report = seed_import.import_seed_file(seed, mode="merge")
    assert report["total"]["inserted"] == 5


# ---------------------------------------------------------------------------
# Reset mode
# ---------------------------------------------------------------------------

def test_reset_wipes_and_reinserts(tmp_path):
    # Pre-populate with two rows the seed does not mention.
    database.insert_config_row(
        "keywords",
        {"suchbegriff": "existing", "sprache": "DE", "markt": [], "themenbereich": [], "prioritaet": None, "notizen": None},
    )
    database.insert_config_row(
        "negative_keywords",
        {"begriff": "existing-negative", "markt": [], "kategorie": None, "notizen": None},
    )

    seed = _write_seed(tmp_path, _base_payload())
    report = seed_import.import_seed_file(seed, mode="reset")
    assert report["keywords"]["inserted"] == 2
    assert report["negative_keywords"]["inserted"] == 1

    kws = database.list_config("keywords")
    assert {r["suchbegriff"] for r in kws} == {"E-Bus Förderung", "Hydrogen bus grant"}
    assert "existing" not in {r["suchbegriff"] for r in kws}


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------

def test_invalid_mode_raises(tmp_path):
    seed = _write_seed(tmp_path, _base_payload())
    with pytest.raises(ValueError):
        seed_import.import_seed_file(seed, mode="upsert")


def test_missing_arrays_are_treated_as_empty(tmp_path):
    seed = _write_seed(tmp_path, {"market": "Empty", "seed_version": 1})
    report = seed_import.import_seed_file(seed, mode="merge")
    assert report["total"]["inserted"] == 0
    assert report["total"]["skipped"] == 0


def test_row_missing_primary_key_is_skipped(tmp_path):
    payload = {
        "keywords": [
            {"suchbegriff": "", "sprache": "DE"},
            {"sprache": "DE"},
            {"suchbegriff": "valid", "sprache": "DE"},
        ]
    }
    seed = _write_seed(tmp_path, payload)
    report = seed_import.import_seed_file(seed, mode="merge")
    assert report["keywords"]["inserted"] == 1
    assert report["keywords"]["skipped"] == 2


def test_actual_shipped_dach_seed_imports_cleanly(tmp_path):
    """Smoke test: the real seeds/dach.json imports without error and is
    idempotent."""
    from pathlib import Path as _P

    seed_path = _P(__file__).resolve().parents[1] / "seeds" / "dach.json"
    if not seed_path.exists():
        pytest.skip("seeds/dach.json not present")

    r1 = seed_import.import_seed_file(seed_path, mode="merge")
    r2 = seed_import.import_seed_file(seed_path, mode="merge")
    assert r1["total"]["inserted"] > 0
    assert r2["total"]["inserted"] == 0
    assert r2["total"]["skipped"] == r1["total"]["inserted"]


@pytest.mark.parametrize(
    "seed_name",
    ["dach", "eu", "italy", "france", "benelux", "portugal", "nordics"],
)
def test_all_shipped_seed_files_import_cleanly(tmp_path, seed_name):
    """Parametric smoke: every shipped seed pack imports cleanly and is
    idempotent on a second run.

    Guards the wizard's "Import selected" step — a broken seed pack (bad JSON,
    empty payload, missing natural key on a keyword row) would surface here
    before it hits Guglielmo on a fresh install.
    """
    from pathlib import Path as _P

    seed_path = _P(__file__).resolve().parents[1] / "seeds" / f"{seed_name}.json"
    if not seed_path.exists():
        pytest.skip(f"seeds/{seed_name}.json not present")

    r1 = seed_import.import_seed_file(seed_path, mode="merge")
    r2 = seed_import.import_seed_file(seed_path, mode="merge")
    assert r1["total"]["inserted"] > 0, f"{seed_name}: first-run inserted 0 rows"
    assert r2["total"]["inserted"] == 0, f"{seed_name}: second-run not idempotent"
    assert r2["total"]["skipped"] == r1["total"]["inserted"]

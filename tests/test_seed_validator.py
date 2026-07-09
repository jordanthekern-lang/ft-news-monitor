"""Tests for scripts/validate_seeds.py (offline validation core, Task 7).

The validator core is designed to be imported by pytest so we can test it
without running a subprocess. Network mode is manual-only.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import validate_seeds  # type: ignore  # noqa: E402


def _valid_payload() -> dict:
    return {
        "market": "DACH",
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
        ],
        "feeds": [
            {
                "rss_feed": "https://example.com/rss",
                "typ": "RSS-Feed",
                "markt": ["DACH"],
                "themenbereich": ["E-buses"],
                "prioritaet": "Medium",
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


def test_valid_payload_passes_clean():
    report = validate_seeds.validate_payload("test.json", _valid_payload())
    assert report.ok(), [f.format() for f in report.errors]
    assert report.warnings == []


def test_missing_array_is_warning_not_error():
    payload = _valid_payload()
    del payload["outlets"]
    report = validate_seeds.validate_payload("test.json", payload)
    assert report.ok()
    assert any(w.table == "outlets" and "missing" in w.message for w in report.warnings)


def test_unknown_priority_is_error():
    payload = _valid_payload()
    payload["keywords"][0]["prioritaet"] = "URGENT"
    report = validate_seeds.validate_payload("test.json", payload)
    assert not report.ok()
    assert any("prioritaet" in e.message and "URGENT" in e.message for e in report.errors)


def test_german_priority_alias_is_warning():
    payload = _valid_payload()
    payload["keywords"][0]["prioritaet"] = "Hoch"
    report = validate_seeds.validate_payload("test.json", payload)
    assert report.ok()
    assert any("prioritaet" in w.message and "Hoch" in w.message for w in report.warnings)


def test_german_theme_alias_is_warning():
    payload = _valid_payload()
    payload["keywords"][0]["themenbereich"] = ["E-Busse"]  # German alias
    report = validate_seeds.validate_payload("test.json", payload)
    assert report.ok()
    assert any("themenbereich" in w.message and "E-Busse" in w.message for w in report.warnings)


def test_bad_market_is_error():
    payload = _valid_payload()
    payload["keywords"][0]["markt"] = ["Narnia"]
    report = validate_seeds.validate_payload("test.json", payload)
    assert not report.ok()
    assert any("Narnia" in e.message for e in report.errors)


def test_sentinel_all_market_accepted():
    payload = _valid_payload()
    payload["keywords"][0]["markt"] = ["All"]
    report = validate_seeds.validate_payload("test.json", payload)
    assert report.ok()


def test_bad_language_is_error():
    payload = _valid_payload()
    payload["keywords"][0]["sprache"] = "XX"
    report = validate_seeds.validate_payload("test.json", payload)
    assert not report.ok()
    assert any("sprache" in e.message and "XX" in e.message for e in report.errors)


def test_missing_required_field_is_error():
    payload = _valid_payload()
    payload["keywords"][0].pop("suchbegriff")
    report = validate_seeds.validate_payload("test.json", payload)
    assert not report.ok()
    assert any("suchbegriff" in e.message for e in report.errors)


def test_combo_keyword_needs_significant_token_per_segment():
    payload = _valid_payload()
    payload["keywords"][0]["suchbegriff"] = "E-Bus + xy"  # second seg has no >=4-char token
    report = validate_seeds.validate_payload("test.json", payload)
    assert not report.ok()
    assert any("significant" in e.message for e in report.errors)


def test_combo_keyword_valid_passes():
    payload = _valid_payload()
    payload["keywords"][0]["suchbegriff"] = "E-Bus + Förderung"
    report = validate_seeds.validate_payload("test.json", payload)
    assert report.ok()


def test_bad_feed_type_is_error():
    payload = _valid_payload()
    payload["feeds"][0]["typ"] = "Atom"
    report = validate_seeds.validate_payload("test.json", payload)
    assert not report.ok()


def test_row_shape_error_when_not_dict():
    payload = _valid_payload()
    payload["keywords"].append("not-a-dict")  # type: ignore[arg-type]
    report = validate_seeds.validate_payload("test.json", payload)
    assert not report.ok()


def test_shipped_seeds_validate_clean():
    """The two seed files shipped in seeds/ must pass with zero warnings."""
    for name in ("dach.json", "eu.json"):
        path = ROOT / "seeds" / name
        if not path.exists():
            pytest.skip(f"seeds/{name} not present")
        report = validate_seeds.validate_file(path)
        assert report.ok(), [f.format() for f in report.errors]
        # Canonical seeds should carry NO alias warnings either.
        assert report.warnings == [], [w.format() for w in report.warnings]


def test_invalid_json_file_is_error(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not valid json {", encoding="utf-8")
    report = validate_seeds.validate_file(bad)
    assert not report.ok()
    assert any("invalid JSON" in e.message for e in report.errors)

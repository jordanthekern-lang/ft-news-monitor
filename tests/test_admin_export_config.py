"""Tests for GET /admin/export-config (Task 7).

Round-trip contract: export the current config → parse the JSON → merge-import
it into a fresh DB → all rows land unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import database, seed_import
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def _seed_one_row_per_table():
    database.insert_config_row(
        "keywords",
        {
            "suchbegriff": "roundtrip-kw",
            "markt": ["DACH"],
            "themenbereich": ["E-buses"],
            "prioritaet": "High",
            "sprache": "DE",
            "notizen": "hello",
        },
    )
    database.insert_config_row(
        "feeds",
        {
            "rss_feed": "https://roundtrip.example/rss",
            "typ": "RSS-Feed",
            "markt": ["DACH"],
            "themenbereich": [],
            "prioritaet": "High",
            "sprache": "DE",
            "notizen": None,
        },
    )
    database.insert_config_row(
        "outlets",
        {
            "outlet": "Roundtrip Outlet",
            "url": "https://roundtrip.example/",
            "markt": ["EU"],
            "themenbereich": [],
            "prioritaet": "Medium",
            "sprache": "EN",
            "outlet_typ": "Trade press",
            "hat_rss": True,
            "notizen": None,
        },
    )
    database.insert_config_row(
        "negative_keywords",
        {"begriff": "roundtrip-neg", "markt": ["EU"], "kategorie": "Off-topic", "notizen": None},
    )


def test_export_config_returns_seed_shape(client):
    _seed_one_row_per_table()
    resp = client.get("/admin/export-config")
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    payload = resp.json()

    assert payload["market"] == "custom-export"
    assert payload["seed_version"] == 1
    for k in ("keywords", "feeds", "outlets", "negative_keywords"):
        assert isinstance(payload[k], list)
    assert any(r["suchbegriff"] == "roundtrip-kw" for r in payload["keywords"])
    assert any(r["outlet"] == "Roundtrip Outlet" for r in payload["outlets"])
    # Auto-managed DB columns must be stripped from the export
    for section in ("keywords", "feeds", "outlets", "negative_keywords"):
        for row in payload[section]:
            assert "id" not in row
            assert "created_at" not in row
            assert "updated_at" not in row


def test_export_config_round_trips_via_seed_importer(client, tmp_path):
    _seed_one_row_per_table()
    resp = client.get("/admin/export-config")
    payload = resp.json()

    # Dump to disk and re-import into the SAME (test-isolated) DB.
    # First we wipe the DB tables so we can observe the re-import inserting
    # everything fresh.
    from app.config import DB_PATH
    import sqlite3

    with sqlite3.connect(DB_PATH) as conn:
        for table in ("keywords", "feeds", "outlets", "negative_keywords"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()

    dump = tmp_path / "export.json"
    dump.write_text(json.dumps(payload), encoding="utf-8")
    report = seed_import.import_seed_file(dump, mode="merge")

    assert report["total"]["inserted"] == 4
    assert report["total"]["skipped"] == 0

    kws = database.list_config("keywords")
    assert any(r["suchbegriff"] == "roundtrip-kw" and r["markt"] == ["DACH"] for r in kws)
    negs = database.list_config("negative_keywords")
    assert any(r["begriff"] == "roundtrip-neg" for r in negs)


def test_export_config_filename_has_date(client):
    resp = client.get("/admin/export-config")
    disp = resp.headers.get("content-disposition", "")
    assert "config-export_" in disp
    assert disp.endswith('.json"')


def test_admin_list_page_has_export_link(client):
    resp = client.get("/admin/keywords")
    assert resp.status_code == 200
    assert "/admin/export-config" in resp.text
    assert "Export configuration" in resp.text

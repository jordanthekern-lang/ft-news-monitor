"""Tests for the admin `markt` field and per-market list filter (Task 3, item 2).

THE known blocker: rows created via the admin UI previously got markt='[]'
and were invisible to market scoping. Every config table now exposes a
"Markets" multiselect; the list views accept ?market=<Market> and show rows
whose markt contains the market OR is empty (empty = global convention).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database
from app.admin import TABLE_META
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


ALL_TABLES = ["keywords", "feeds", "outlets", "negative_keywords"]


def test_all_tables_have_markt_field_and_list_column():
    for table in ALL_TABLES:
        meta = TABLE_META[table]
        markt_fields = [f for f in meta["fields"] if f["name"] == "markt"]
        assert markt_fields, f"{table}: TABLE_META has no markt field"
        field = markt_fields[0]
        assert field["type"] == "multiselect", f"{table}: markt must be multiselect"
        choices = {c["value"] for c in field["choices"]}
        assert choices == {
            "DACH", "Benelux", "France", "Italy", "Nordics", "Portugal", "EU",
        }, f"{table}: markt choices must be VALID_MARKETS"
        assert field["label"] == "Markets"
        assert "markt" in meta["list_columns"], f"{table}: markt missing in list_columns"


def test_create_keyword_with_markets_persists_list(client):
    resp = client.post(
        "/admin/keywords/new",
        data={
            "suchbegriff": "elektrobus",
            "sprache": "DE",
            "markt": ["Italy", "EU"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    rows = database.list_config("keywords")
    assert len(rows) == 1
    assert rows[0]["markt"] == ["Italy", "EU"]


def test_create_without_markets_is_global_empty_list(client):
    resp = client.post(
        "/admin/negative-keywords/new",
        data={"begriff": "casino"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    rows = database.list_config("negative_keywords")
    assert rows[0]["markt"] == []


def _seed_three_keywords():
    for term, markt in (
        ("italokw", ["Italy"]),
        ("dachkw", ["DACH"]),
        ("globalkw", []),
    ):
        database.insert_config_row(
            "keywords",
            {
                "suchbegriff": term,
                "markt": markt,
                "themenbereich": [],
                "prioritaet": None,
                "sprache": "DE",
                "notizen": None,
            },
        )


def test_list_view_market_filter_shows_match_and_global(client):
    _seed_three_keywords()
    resp = client.get("/admin/keywords?market=Italy")
    assert resp.status_code == 200
    assert "italokw" in resp.text
    assert "globalkw" in resp.text  # empty markt = global, always shown
    assert "dachkw" not in resp.text


def test_list_view_without_filter_shows_all(client):
    _seed_three_keywords()
    resp = client.get("/admin/keywords")
    assert resp.status_code == 200
    for term in ("italokw", "dachkw", "globalkw"):
        assert term in resp.text


def test_list_view_invalid_market_ignored(client):
    _seed_three_keywords()
    resp = client.get("/admin/keywords?market=Narnia")
    assert resp.status_code == 200
    for term in ("italokw", "dachkw", "globalkw"):
        assert term in resp.text


def test_edit_form_updates_markets(client):
    _seed_three_keywords()
    rows = database.list_config("keywords")
    row_id = next(r["id"] for r in rows if r["suchbegriff"] == "dachkw")
    resp = client.post(
        f"/admin/keywords/{row_id}/edit",
        data={"suchbegriff": "dachkw", "sprache": "DE", "markt": ["France"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    updated = next(
        r for r in database.list_config("keywords") if r["id"] == row_id
    )
    assert updated["markt"] == ["France"]

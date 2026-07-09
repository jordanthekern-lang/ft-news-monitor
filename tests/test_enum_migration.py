"""Tests for scripts/migrate_enums.py — idempotency and correct rewrites.

Uses the isolated_db fixture from conftest.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from app import database


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _insert_keyword(suchbegriff: str, prioritaet: str, themenbereich: list) -> int:
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO keywords (suchbegriff, prioritaet, themenbereich, markt, created_at, updated_at) "
            "VALUES (?, ?, ?, '[]', ?, ?)",
            (suchbegriff, prioritaet, json.dumps(themenbereich), _now(), _now()),
        )
        return cur.lastrowid


def _insert_article_with_category(category: str, themes: list) -> int:
    rid = database.insert_article(
        url=f"https://example.com/{category.replace(' ', '-')}",
        title="Test",
        source_name="src",
        source_type="rss",
        country="DACH",
        published_at=datetime.now(timezone.utc),
        snippet="test",
    )
    database.update_ai_score(
        rid,
        relevance_score=5,
        trust_bonus=0,
        category=category,
        themes=themes,
        reasoning="test",
    )
    return rid


def _insert_neg_kw(markt: list, kategorie: str) -> int:
    with database.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO negative_keywords (begriff, markt, kategorie, created_at, updated_at) "
            "VALUES ('spam', ?, ?, ?, ?)",
            (json.dumps(markt), kategorie, _now(), _now()),
        )
        return cur.lastrowid


# ---------------------------------------------------------------------------
# Import the migration module
# ---------------------------------------------------------------------------

def _run_migration(db_path=None):
    import importlib
    import sys
    # Remove cached module so we get a fresh import each time
    if "scripts.migrate_enums" in sys.modules:
        del sys.modules["scripts.migrate_enums"]
    # Import and run
    from scripts import migrate_enums
    if db_path is None:
        from app.config import DB_PATH
        db_path = DB_PATH
    return migrate_enums.migrate(db_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_migrates_german_category_in_articles():
    rid = _insert_article_with_category("Fahrzeugförderung", [])
    result = _run_migration()
    with database.get_conn() as conn:
        row = conn.execute("SELECT category FROM articles WHERE id = ?", (rid,)).fetchone()
    assert row["category"] == "Vehicle funding"
    assert result["articles"]["updated"] >= 1


def test_migrates_german_themes_in_articles():
    rid = _insert_article_with_category("Other", ["E-Busse", "Ladeinfrastruktur"])
    result = _run_migration()
    with database.get_conn() as conn:
        row = conn.execute("SELECT themes FROM articles WHERE id = ?", (rid,)).fetchone()
    themes = json.loads(row["themes"])
    assert "E-buses" in themes
    assert "Charging infrastructure" in themes


def test_migrates_german_priority_in_keywords():
    kid = _insert_keyword("e-bus", "Hoch", [])
    result = _run_migration()
    with database.get_conn() as conn:
        row = conn.execute("SELECT prioritaet FROM keywords WHERE id = ?", (kid,)).fetchone()
    assert row["prioritaet"] == "High"


def test_migrates_german_themenbereich_in_keywords():
    kid = _insert_keyword("e-bus", "High", ["E-Busse", "Wasserstoffbusse"])
    result = _run_migration()
    with database.get_conn() as conn:
        row = conn.execute("SELECT themenbereich FROM keywords WHERE id = ?", (kid,)).fetchone()
    themes = json.loads(row["themenbereich"])
    assert "E-buses" in themes
    assert "Hydrogen buses" in themes


def test_migration_is_idempotent():
    _insert_article_with_category("Fahrzeugförderung", ["E-Busse"])
    _insert_keyword("bus", "Hoch", ["Ladeinfrastruktur"])

    result1 = _run_migration()
    result2 = _run_migration()

    # Second run: everything already English → all unchanged
    for table in result2:
        assert result2[table]["updated"] == 0, f"Table {table} still had updates on 2nd run"
        # unchanged count must be >= whatever was in result1
        assert result2[table]["unchanged"] >= result1[table]["updated"] + result1[table]["unchanged"]


def test_migrates_alle_sentinel_to_all():
    nkid = _insert_neg_kw(["Alle"], "Off-topic")
    _run_migration()
    with database.get_conn() as conn:
        row = conn.execute("SELECT markt FROM negative_keywords WHERE id = ?", (nkid,)).fetchone()
    markt = json.loads(row["markt"])
    assert "All" in markt
    assert "Alle" not in markt


def test_english_values_unchanged():
    """Already-English values must be untouched (idempotent at value level)."""
    rid = _insert_article_with_category("Vehicle funding", ["E-buses"])
    result = _run_migration()
    with database.get_conn() as conn:
        row = conn.execute("SELECT category, themes FROM articles WHERE id = ?", (rid,)).fetchone()
    assert row["category"] == "Vehicle funding"
    assert "E-buses" in json.loads(row["themes"])
    assert result["articles"]["unchanged"] >= 1

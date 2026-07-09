"""Tests for Task 6: Markdown + CSV export.

The export module produces two artefacts from the same
``database.list_articles`` row shape:

* ``build_markdown`` — Confluence-friendly, grouped by ``country`` (empty
  → "Global"), sorted score DESC within each group, VALID_MARKETS order
  across groups.
* ``build_csv`` — UTF-8 with BOM, one row per article, dd.mm.yyyy dates.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app import database, export
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


def _article(
    *,
    title="An article",
    country="Italy",
    score=8,
    trust=0,
    starred=False,
    themes=None,
    category="Vehicle funding",
    published="2026-07-05T10:00:00+00:00",
    reasoning="",
    url=None,
    source="La Repubblica",
    title_en=None,
) -> dict:
    return {
        "title": title,
        "title_en": title_en,
        "url": url or f"https://example.com/{title.replace(' ', '-')}",
        "source_name": source,
        "country": country,
        "published_at": published,
        "relevance_score": score,
        "trust_bonus": trust,
        "feedback_bonus": 0,
        "starred": 1 if starred else 0,
        "read": 0,
        "ignored": 0,
        "category": category,
        "themes": themes if themes is not None else '["E-buses", "Charging infrastructure"]',
        "relevance_reasoning": reasoning,
    }


# ---------------------------------------------------------------------------
# build_markdown
# ---------------------------------------------------------------------------


def test_build_markdown_groups_by_market_and_sorts():
    rows = [
        _article(title="IT-mid", country="Italy", score=6),
        _article(title="IT-high", country="Italy", score=9),
        _article(title="EU-only", country="EU", score=7),
    ]
    md = export.build_markdown(rows)

    # Group headers present
    assert "## Italy" in md
    assert "## EU" in md

    # Italy before EU (VALID_MARKETS order: DACH, Benelux, France, Italy,
    # Nordics, Portugal, EU)
    assert md.index("## Italy") < md.index("## EU")

    # Score DESC inside Italy: IT-high above IT-mid
    it_block = md[md.index("## Italy"): md.index("## EU")]
    assert it_block.index("IT-high") < it_block.index("IT-mid")


def test_build_markdown_empty_country_bucketed_as_global():
    rows = [
        _article(title="Global-thing", country="", score=8),
        _article(title="IT-thing", country="Italy", score=8),
    ]
    md = export.build_markdown(rows)
    assert "## Global" in md
    # Global renders last (after known markets)
    assert md.index("## Italy") < md.index("## Global")


def test_build_markdown_includes_reasoning_when_flag_set():
    rows = [_article(reasoning="Because the article discusses e-bus tenders in Milan.")]
    md = export.build_markdown(rows, include_reasoning=True)
    assert "> Because the article discusses e-bus tenders in Milan." in md


def test_build_markdown_omits_reasoning_by_default():
    rows = [_article(reasoning="This should not appear")]
    md = export.build_markdown(rows)
    assert "This should not appear" not in md


def test_build_markdown_handles_missing_fields():
    # No themes, no category, no trust bonus, no reasoning
    rows = [
        _article(themes="[]", category=None, trust=0, reasoning=""),
    ]
    md = export.build_markdown(rows)
    # No " —  — " gap and no "(+0 trust)"
    assert " —  — " not in md
    assert "+0 trust" not in md
    # score X present
    assert "score 8" in md


def test_build_markdown_starred_glyph():
    rows = [_article(title="StarredTitleXYZ", starred=True)]
    md = export.build_markdown(rows)
    assert "⭐ StarredTitleXYZ" in md


# ---------------------------------------------------------------------------
# build_csv
# ---------------------------------------------------------------------------


def test_build_csv_utf8_bom():
    body = export.build_csv([_article()])
    assert body[:3] == b"\xef\xbb\xbf"


def test_build_csv_columns_and_types():
    rows = [
        _article(
            title="Italian article",
            country="Italy",
            score=7,
            trust=2,
            starred=True,
            themes='["E-buses"]',
            category="Vehicle funding",
            published="2026-07-05T10:00:00+00:00",
            reasoning="reason here",
            source="La Repubblica",
        )
    ]
    body = export.build_csv(rows)
    # Strip BOM before csv.reader
    text = body[3:].decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert header == list(export.CSV_COLUMNS)

    row = next(reader)
    columns = dict(zip(header, row))
    assert columns["published_date"] == "05.07.2026"
    assert columns["market"] == "Italy"
    assert columns["title"] == "Italian article"
    assert columns["source"] == "La Repubblica"
    assert columns["category"] == "Vehicle funding"
    assert columns["themes"] == "E-buses"
    assert columns["relevance_score"] == "9"  # 7 + trust 2
    assert columns["trust_bonus"] == "2"
    assert columns["starred"] == "true"
    assert columns["status"] == "starred"
    assert columns["reasoning"] == "reason here"


def test_build_csv_neutralises_formula_injection():
    """Article titles from untrusted RSS feeds must not become Excel formulas.

    Regression test for CWE-1236: cells beginning with ``=``/``+``/``-``/``@``/
    tab/CR are executed as formulas on double-click. ``build_csv`` must prefix
    every offending free-form string cell with an apostrophe so Excel treats
    the value as literal text.
    """
    row = _article(
        title="=SUM(1+1)",
        title_en="+cmd|/c calc'!A0",
        source="@evilsource",
        country="Italy",
        published="2026-07-07T10:00:00+00:00",
        category="-Vehicle funding",
        themes=["\tE-buses"],  # tab-prefixed theme (list form; JSON forbids raw tabs)
        reasoning="=1+1",
        url="https://example.com/",
    )
    body = export.build_csv([row])
    text = body[3:].decode("utf-8")  # strip UTF-8 BOM
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    cells = dict(zip(header, next(reader)))

    # Every free-form cell that started with a formula trigger must now
    # begin with an apostrophe and preserve the original payload verbatim.
    assert cells["title"] == "'=SUM(1+1)"
    assert cells["title_en"] == "'+cmd|/c calc'!A0"
    assert cells["source"] == "'@evilsource"
    assert cells["category"] == "'-Vehicle funding"
    assert cells["themes"] == "'\tE-buses"
    assert cells["reasoning"] == "'=1+1"

    # A benign cell is unaffected.
    assert cells["market"] == "Italy"

    # Belt & braces: no free-form cell begins with a raw formula trigger.
    free_form = ("title", "title_en", "url", "source", "category", "themes", "reasoning", "market")
    for name in free_form:
        value = cells[name]
        if value:
            assert value[0] not in ("=", "+", "-", "@", "\t", "\r"), (
                f"raw formula trigger in {name}: {value!r}"
            )


def test_build_csv_multi_market_row():
    """Article ``country`` currently scalar; when a legacy row carries a
    semicolon-joined string, the CSV preserves it verbatim."""
    row = _article(country="Italy;EU")
    body = export.build_csv([row])
    text = body[3:].decode("utf-8")
    reader = csv.reader(io.StringIO(text))
    next(reader)  # header
    market_cell = next(reader)[list(export.CSV_COLUMNS).index("market")]
    assert market_cell == "Italy;EU"


# ---------------------------------------------------------------------------
# Route integration
# ---------------------------------------------------------------------------


def _seed_article(url: str, title: str, country: str, score: int = 8) -> int:
    rid = database.insert_article(
        url=url,
        title=title,
        source_name="src",
        source_type="rss",
        country=country,
        published_at=datetime.now(timezone.utc),
        snippet="snippet",
    )
    assert rid is not None
    database.update_ai_score(
        rid,
        relevance_score=score,
        trust_bonus=0,
        category="Vehicle funding",
        themes=["E-buses"],
        reasoning="r",
    )
    return rid


def test_route_export_md_respects_filters(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy", score=8)
    _seed_article("https://de.example.com/a", "DachArticleXYZ", "DACH", score=8)
    resp = client.get("/export.md?market=Italy&min_score=7")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert "ItalyArticleXYZ" in resp.text
    assert "DachArticleXYZ" not in resp.text


def test_route_export_csv_respects_filters(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy", score=8)
    _seed_article("https://de.example.com/a", "DachArticleXYZ", "DACH", score=8)
    resp = client.get("/export.csv?market=Italy&min_score=7")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert "attachment" in resp.headers.get("content-disposition", "")
    body = resp.content
    assert body[:3] == b"\xef\xbb\xbf"
    text = body[3:].decode("utf-8")
    assert "ItalyArticleXYZ" in text
    assert "DachArticleXYZ" not in text


def test_route_copy_endpoint_returns_inline_plain(client):
    _seed_article("https://it.example.com/a", "ItalyArticleXYZ", "Italy", score=8)
    resp = client.get("/export.md?copy=1&min_score=0")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/plain")
    disposition = resp.headers.get("content-disposition", "")
    assert disposition == "inline"
    assert resp.text.startswith("# FT News Monitor")

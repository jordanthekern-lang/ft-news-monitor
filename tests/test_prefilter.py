"""Tests for the pre-filter stage."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app import database, prefilter
from app.models import NegativeKeyword, NotionConfig


def _seed_article(*, title: str, snippet: str, country: str, age_days: int) -> int:
    published = datetime.now(timezone.utc) - timedelta(days=age_days)
    rid = database.insert_article(
        url=f"https://example.com/{title.replace(' ', '-')}",
        title=title,
        source_name="test",
        source_type="rss",
        country=country,
        published_at=published,
        snippet=snippet,
    )
    assert rid is not None
    return rid


def test_filters_out_old_articles(monkeypatch):
    from app import config as app_config

    monkeypatch.setattr(
        app_config,
        "get_settings",
        lambda: app_config.Settings(
            ai_provider="bedrock",
            anthropic_api_key="",
            anthropic_model="x",
            ai_max_tokens=512,
            aws_profile="",
            aws_access_key_id="",
            aws_secret_access_key="",
            aws_session_token="",
            aws_region="eu-west-1",
            dashboard_port=8766,
            timezone="UTC",
            max_age_days=30,
            highlight_days=7,
            ai_filter_max_parallel=2,
            ai_skip_prescreen_days=0,
        ),
    )
    monkeypatch.setattr(prefilter, "get_settings", app_config.get_settings)

    young = _seed_article(title="Förderung E-Bus", snippet="frisch", country="DACH", age_days=5)
    old = _seed_article(title="Altmeldung Bus", snippet="alt", country="DACH", age_days=120)

    summary = prefilter.run(NotionConfig())

    assert summary["filtered_age"] == 1
    assert summary["passed"] == 1

    young_row = database.article_by_id(young)
    old_row = database.article_by_id(old)
    assert young_row["is_filtered_out"] == 0
    assert old_row["is_filtered_out"] == 1
    assert "older than" in (old_row["filter_reason"] or "")


def test_filters_out_negative_keyword_global():
    rid = _seed_article(title="Stellenanzeige Busfahrer", snippet="Wir suchen", country="DACH", age_days=2)
    cfg = NotionConfig(
        negative_keywords=[NegativeKeyword(begriff="Stellenanzeige", markt=["Alle"])]
    )
    summary = prefilter.run(cfg)

    assert summary["filtered_negative_keyword"] == 1
    row = database.article_by_id(rid)
    assert row["is_filtered_out"] == 1
    assert "stellenanzeige" in (row["filter_reason"] or "").lower()


def test_negative_keyword_market_scoped():
    rid_dach = _seed_article(title="Werbung Busse", snippet="", country="DACH", age_days=2)
    rid_fr = _seed_article(title="Werbung Bus FR", snippet="", country="France", age_days=2)
    cfg = NotionConfig(
        negative_keywords=[NegativeKeyword(begriff="Werbung", markt=["DACH"])]
    )
    prefilter.run(cfg)

    assert database.article_by_id(rid_dach)["is_filtered_out"] == 1
    assert database.article_by_id(rid_fr)["is_filtered_out"] == 0


def test_passes_when_no_match():
    rid = _seed_article(title="EU plant Förderung Wasserstoffbus", snippet="", country="EU", age_days=10)
    cfg = NotionConfig()
    summary = prefilter.run(cfg)

    assert summary["passed"] == 1
    assert database.article_by_id(rid)["is_filtered_out"] == 0

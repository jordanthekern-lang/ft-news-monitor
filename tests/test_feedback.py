"""Tests for the feedback module (few-shot + per-domain adjustment)."""

from __future__ import annotations

from datetime import datetime, timezone

from app import database, feedback


def _seed(url: str, *, title: str = "T", source: str = "src") -> int:
    rid = database.insert_article(
        url=url,
        title=title,
        source_name=source,
        source_type="rss",
        country="DACH",
        published_at=datetime.now(timezone.utc),
        snippet="snippet",
    )
    assert rid is not None
    database.update_ai_score(
        rid,
        relevance_score=5,
        trust_bonus=0,
        feedback_bonus=0,
        category="Sonstiges",
        themes=[],
        reasoning="r",
    )
    return rid


def _toggle(article_id: int, column: str) -> None:
    database.toggle_flag(article_id, column)


def test_recent_examples_returns_starred_and_ignored():
    star = _seed("https://a.example.com/1", title="Spannender Förder-Artikel")
    ignore = _seed("https://b.example.com/1", title="Off-Topic")
    _toggle(star, "starred")
    _toggle(ignore, "ignored")

    examples = feedback.recent_examples(limit_per_kind=5)
    labels = sorted(e.label for e in examples)
    assert labels == ["INTERESTING", "IRRELEVANT"]
    titles_by_label = {e.label: e.title for e in examples}
    assert "Förder-Artikel" in titles_by_label["INTERESTING"]
    assert "Off-Topic" in titles_by_label["IRRELEVANT"]


def test_domain_adjustment_under_threshold_is_zero():
    rid = _seed("https://small.example.com/post")
    _toggle(rid, "starred")
    # only one vote → below MIN_DOMAIN_VOTES → no adjustment
    assert feedback.domain_adjustment("https://small.example.com/post") == 0


def test_domain_adjustment_positive_when_mostly_starred():
    for i in range(4):
        rid = _seed(f"https://goodsite.example.com/{i}")
        _toggle(rid, "starred")
    rid = _seed("https://goodsite.example.com/x")
    _toggle(rid, "ignored")  # 4 stars / 1 ignore → ratio 0.8 → +1 or +2

    adj = feedback.domain_adjustment("https://goodsite.example.com/anything")
    assert adj > 0
    assert adj <= feedback.MAX_FEEDBACK_BONUS


def test_domain_adjustment_negative_when_mostly_ignored():
    for i in range(5):
        rid = _seed(f"https://spam.example.com/{i}")
        _toggle(rid, "ignored")
    adj = feedback.domain_adjustment("https://spam.example.com/anything")
    assert adj == feedback.MIN_FEEDBACK_BONUS  # 100 % ignored → -2


def test_domain_adjustment_zero_when_balanced():
    for i in range(3):
        rid = _seed(f"https://mixed.example.com/star{i}")
        _toggle(rid, "starred")
    for i in range(3):
        rid = _seed(f"https://mixed.example.com/ig{i}")
        _toggle(rid, "ignored")
    assert feedback.domain_adjustment("https://mixed.example.com/x") == 0


def test_domain_adjustment_unparseable_url_returns_zero():
    assert feedback.domain_adjustment("") == 0


def test_domain_summary_marks_under_threshold():
    rid = _seed("https://just-one-news.de/x")
    _toggle(rid, "starred")
    summary = feedback.domain_summary()
    assert len(summary) == 1
    row = summary[0]
    assert row["stars"] == 1
    assert row["ignores"] == 0
    assert row["adjustment"] == 0  # below threshold

"""Tests for the translator stage. Google's endpoint is fully mocked."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import database, translator


def _seed(*, title: str, snippet: str, country: str = "France") -> int:
    rid = database.insert_article(
        url=f"https://example.com/{title.replace(' ', '-')}",
        title=title,
        source_name="test",
        source_type="rss",
        country=country,
        published_at=datetime.now(timezone.utc),
        snippet=snippet,
    )
    assert rid is not None
    return rid


@pytest.fixture
def fake_google(monkeypatch):
    """Patch deep-translator.GoogleTranslator with a controllable fake."""
    captured = []

    class FakeTranslator:
        def __init__(self, source: str, target: str):
            self.source = source
            self.target = target

        def translate(self, text: str) -> str:
            captured.append((self.source, self.target, text))
            return f"[EN from {self.source}] {text}"

    fake_module = type("M", (), {"GoogleTranslator": FakeTranslator})

    import sys
    monkeypatch.setitem(sys.modules, "deep_translator", fake_module)
    return captured


def test_skip_when_disabled(monkeypatch, fake_google):
    monkeypatch.setenv("TRANSLATION_ENABLED", "false")
    rid = _seed(title="Subvention bus électrique", snippet="Le gouvernement annonce")
    summary = translator.run()
    assert summary == {
        "candidates": 0, "detected": 0, "translated": 0,
        "skipped_already_target": 0, "skipped_no_lang": 0, "errors": 0,
    }
    assert fake_google == []
    row = database.article_by_id(rid)
    assert row["translation_attempted"] == 0


def test_skips_german_articles(monkeypatch, fake_google):
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")
    rid = _seed(
        title="Förderung für Elektrobusse beschlossen",
        snippet="Bundesregierung gibt grünes Licht für neue Mittel.",
        country="DACH",
    )
    summary = translator.run()
    assert summary["skipped_already_target"] == 1
    assert summary["translated"] == 0
    row = database.article_by_id(rid)
    assert row["translation_attempted"] == 1
    assert row["language"] == "de"
    assert row["title_en"] is None
    assert fake_google == []  # no translation calls


def test_skips_english_articles(monkeypatch, fake_google):
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")
    rid = _seed(
        title="EU announces new funding for hydrogen buses",
        snippet="The European Commission unveiled a major package today.",
        country="EU",
    )
    summary = translator.run()
    assert summary["skipped_already_target"] == 1
    assert summary["translated"] == 0
    row = database.article_by_id(rid)
    assert row["language"] == "en"
    assert row["title_en"] is None


def test_translates_french_article(monkeypatch, fake_google):
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")
    rid = _seed(
        title="Subvention pour les bus électriques annoncée à Paris",
        snippet="Le gouvernement français a confirmé un nouveau programme de financement.",
        country="France",
    )
    summary = translator.run()
    assert summary["translated"] == 1
    row = database.article_by_id(rid)
    assert row["language"] == "fr"
    assert row["title_en"].startswith("[EN from fr]")
    assert row["snippet_en"].startswith("[EN from fr]")
    assert row["translation_attempted"] == 1


def test_uses_market_hint_for_short_titles(monkeypatch, fake_google):
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")
    # "EU autobus" is too short for reliable detection — should fall back
    # to the market hint ("Italy" → "it") and translate.
    rid = _seed(title="autobus", snippet="", country="Italy")
    summary = translator.run()
    assert summary["translated"] == 1
    row = database.article_by_id(rid)
    assert row["language"] == "it"
    assert row["title_en"].startswith("[EN from it]")


def test_failure_retries_then_gives_up(monkeypatch):
    """Google's endpoint flakes occasionally — one failed run should leave the
    article retriable; only after MAX_TRANSLATION_ATTEMPTS do we mark it final."""
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")

    class ExplosiveTranslator:
        def __init__(self, source, target):
            pass

        def translate(self, text):
            raise RuntimeError("Google blocked us")

    import sys
    fake_module = type("M", (), {"GoogleTranslator": ExplosiveTranslator})
    monkeypatch.setitem(sys.modules, "deep_translator", fake_module)

    rid = _seed(
        title="Subvention pour les bus électriques annoncée à Paris",
        snippet="Le gouvernement français a confirmé un nouveau programme.",
        country="France",
    )

    # First run: failure, but the article remains retriable.
    summary = translator.run()
    assert summary["errors"] == 1
    assert summary["translated"] == 0
    row = database.article_by_id(rid)
    assert row["translation_attempted"] == 0
    assert row["translation_attempts"] == 1
    assert row["title_en"] is None

    # Subsequent runs eventually trip the cap.
    for expected_attempts in (2, translator.MAX_TRANSLATION_ATTEMPTS):
        translator.run()
        row = database.article_by_id(rid)
        assert row["translation_attempts"] == expected_attempts

    # After MAX_TRANSLATION_ATTEMPTS the article is finally marked attempted
    # so it stops appearing in the candidate list.
    assert row["translation_attempted"] == 1


def test_skips_articles_already_filtered_out(monkeypatch, fake_google):
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")
    rid = _seed(
        title="Subvention pour les bus électriques",
        snippet="Le gouvernement annonce un programme",
        country="France",
    )
    database.mark_filtered(rid, reason="too old")

    summary = translator.run()
    assert summary["candidates"] == 0
    assert fake_google == []
    row = database.article_by_id(rid)
    assert row["title_en"] is None


# ---------------------------------------------------------------------------
# v0.3.1 — translation_target_langs (auto-derive from home markets, or
# manual override via runtime_settings)
# ---------------------------------------------------------------------------


def test_target_langs_defaults_when_no_home_markets_set():
    from app import runtime_settings

    # Clean slate: no home_markets, no manual override.
    runtime_settings.set(runtime_settings.HOME_MARKETS, [])
    runtime_settings.set(runtime_settings.TRANSLATION_TARGET_LANGS, None)

    langs = translator.translation_target_langs()
    # Falls back to the module-level historical default.
    assert langs == translator.TARGET_LANGUAGES


def test_target_langs_auto_derives_from_home_markets():
    from app import runtime_settings

    runtime_settings.set(runtime_settings.HOME_MARKETS, ["DACH", "EU"])
    runtime_settings.set(runtime_settings.TRANSLATION_TARGET_LANGS, None)

    langs = translator.translation_target_langs()
    # DACH → de, EU → en, plus the always-included "en".
    assert langs == {"de", "en"}


def test_target_langs_auto_for_portugal():
    from app import runtime_settings

    runtime_settings.set(runtime_settings.HOME_MARKETS, ["Portugal"])
    runtime_settings.set(runtime_settings.TRANSLATION_TARGET_LANGS, None)

    langs = translator.translation_target_langs()
    # PT is kept in original + EN never translated.
    assert langs == {"pt", "en"}


def test_target_langs_manual_override_wins():
    from app import runtime_settings

    runtime_settings.set(runtime_settings.HOME_MARKETS, ["Portugal"])
    runtime_settings.set(
        runtime_settings.TRANSLATION_TARGET_LANGS, ["de", "en", "fr"]
    )

    langs = translator.translation_target_langs()
    assert langs == {"de", "en", "fr"}


def test_portugal_article_stays_untranslated_for_portugal_market(
    monkeypatch, fake_google
):
    """Portugal-based colleague: PT articles must NOT be translated to EN."""
    from app import runtime_settings

    runtime_settings.set(runtime_settings.HOME_MARKETS, ["Portugal"])
    runtime_settings.set(runtime_settings.TRANSLATION_TARGET_LANGS, None)
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")

    rid = _seed(
        title="Autocarro elétrico em Lisboa",
        snippet="A empresa anuncia novos autocarros elétricos",
        country="Portugal",
    )

    summary = translator.run()
    # Skipped because pt is now in the target-set for Portugal.
    assert summary["skipped_already_target"] == 1
    assert summary["translated"] == 0
    # And Google was never called.
    assert fake_google == []
    row = database.article_by_id(rid)
    assert row["title_en"] is None


def test_rate_limit_block_aborts_run_without_burning_attempts(monkeypatch):
    """Google's CAPTCHA block (TooManyRequests) hits every request, so the run
    stops at the first one and no article loses one of its retry attempts."""
    monkeypatch.setenv("TRANSLATION_ENABLED", "true")
    calls = []

    class TooManyRequests(Exception):
        pass

    class BlockedTranslator:
        def __init__(self, source, target):
            pass

        def translate(self, text):
            calls.append(text)
            raise TooManyRequests("You made too many requests to the server.")

    import sys
    fake_module = type("M", (), {"GoogleTranslator": BlockedTranslator})
    monkeypatch.setitem(sys.modules, "deep_translator", fake_module)
    monkeypatch.setattr(translator, "TRANSLATE_DELAY_S", 0)

    ids = [
        _seed(title=f"Subvention autocar électrique numéro {i} à Lyon",
              snippet="La région annonce un nouveau programme de soutien.",
              country="France")
        for i in range(3)
    ]

    summary = translator.run()
    assert len(calls) == 1
    assert summary["translated"] == 0
    for rid in ids:
        row = database.article_by_id(rid)
        assert row["translation_attempts"] == 0
        assert row["translation_attempted"] == 0

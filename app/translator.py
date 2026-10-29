"""Lightweight translation step: convert non-DE/EN articles to English.

We use ``deep-translator``'s Google backend, which talks to Google Translate's
public widget endpoint and does NOT require an API key. That makes translation
free, but the trade-off is that Google could change or rate-limit the endpoint
at any time. To stay robust:

- Each article is processed independently in a try/except — one failure
  doesn't abort the whole run.
- ``translation_attempted = 1`` is set even on failure, so we don't retry
  forever; the user can re-trigger by clearing the flag manually.
- Set ``TRANSLATION_ENABLED=false`` in the .env to disable entirely.

Pipeline order:
    collector → prefilter → translator → ai_filter
so we only translate articles that survived the cheap filters.
"""

from __future__ import annotations

import logging
import os
import time
from typing import Optional

from . import database

logger = logging.getLogger(__name__)


# Default languages we don't need to translate. Used as a fallback when no
# home markets are configured yet (e.g. fresh install). At runtime this is
# overridden by :func:`translation_target_langs`, which derives the skip-list
# from ``runtime_settings.home_markets`` (v0.3.1). Kept as a module-level
# name for backward-compat with tests that patch it.
TARGET_LANGUAGES = {"de", "en"}


def translation_target_langs() -> set[str]:
    """Languages that should NOT be translated (kept in original language).

    v0.3.1: replaces the hard-coded ``TARGET_LANGUAGES`` skip-list.

    Resolution order:
      1. Manual override in ``runtime_settings.TRANSLATION_TARGET_LANGS``
         (a JSON list of ISO codes, e.g. ``["de","en","pt"]``) — used
         verbatim when non-empty. This lets a Portugal-based colleague
         say "keep PT and EN as-is" explicitly.
      2. Auto-derived from ``runtime_settings.effective_home_markets()``
         via :data:`config.MARKET_TO_LANGS`, plus a hard-coded ``"en"``
         (the UI + Claude prompt are English, so English articles are
         always readable without translation).
      3. Falls back to the module-level :data:`TARGET_LANGUAGES` set when
         no home markets are configured (fresh install pre-wizard).
    """
    from . import runtime_settings
    from .config import MARKET_TO_LANGS

    manual = runtime_settings.get(runtime_settings.TRANSLATION_TARGET_LANGS)
    if isinstance(manual, list) and manual:
        return {
            code.lower() for code in manual if isinstance(code, str) and code
        }

    markets = runtime_settings.effective_home_markets() or []
    if not markets:
        # Nothing configured yet → be conservative and use the historical
        # DE/EN default so behaviour on a bare install is unchanged.
        return set(TARGET_LANGUAGES)

    derived = {"en"}
    for m in markets:
        for code in MARKET_TO_LANGS.get(m, []):
            derived.add(code.lower())
    return derived

# Heuristics
MIN_TEXT_LEN_FOR_DETECTION = 20
MAX_TEXT_LEN_PER_CALL = 4500  # Google has a per-request limit
TRANSLATE_DELAY_S = 0.3  # be polite to the unofficial endpoint
# How often we retry a single article before giving up. Google's public
# widget endpoint is not officially supported and occasionally rate-limits,
# so a single failure should not condemn the article forever.
MAX_TRANSLATION_ATTEMPTS = 3

# Fallback hint when no source-language is configured AND langdetect is
# unreliable (very short titles etc.). Markets that span multiple languages
# are intentionally absent — guessing "sv" for a Norwegian source did more
# harm than good.
MARKET_LANG_HINT = {
    "DACH": "de",
    "EU": "en",
    "France": "fr",
    "Italy": "it",
    "Portugal": "pt",
}


def _translation_enabled() -> bool:
    return os.environ.get("TRANSLATION_ENABLED", "true").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _detect_language(
    text: str,
    market: Optional[str],
    *,
    source_language: Optional[str] = None,
) -> Optional[str]:
    """Best-effort language code (e.g. 'de', 'fr', 'pt').

    Priority:
      1. The source language declared in Notion (most reliable — the user
         curated the feed/outlet and knows what language it publishes in).
      2. langdetect on title + snippet, if there's enough text.
      3. The market hint, but only for single-language markets.
    """
    if source_language:
        return source_language.lower()
    if text and len(text) >= MIN_TEXT_LEN_FOR_DETECTION:
        try:
            from langdetect import DetectorFactory, detect
            DetectorFactory.seed = 0  # reproducible detection
            return detect(text).lower()
        except Exception as exc:  # langdetect raises a custom exception
            logger.debug("langdetect failed: %s", exc)
    if market:
        return MARKET_LANG_HINT.get(market)
    return None


class TranslatorBlocked(Exception):
    """Google answered with its rate-limit / CAPTCHA block.

    The block applies to the whole IP, not to one article, so every further
    request in this run would fail the same way. ``run()`` stops on it.
    """


def _is_rate_limit_block(exc: Exception) -> bool:
    # deep-translator raises ``exceptions.TooManyRequests`` when Google
    # redirects to its /sorry CAPTCHA page. Match by class name so the check
    # does not depend on importing deep-translator's exception module.
    return type(exc).__name__ == "TooManyRequests"


def _translate(text: str, source_lang: str) -> Optional[str]:
    """Translate one chunk of text to English. Returns None on failure."""
    if not text:
        return None
    try:
        from deep_translator import GoogleTranslator
    except ImportError as exc:
        logger.error("deep-translator not installed: %s", exc)
        return None

    try:
        out = GoogleTranslator(
            source=source_lang or "auto", target="en"
        ).translate(text[:MAX_TEXT_LEN_PER_CALL])
        if not out or out.strip() == text.strip():
            return None
        return out
    except Exception as exc:
        if _is_rate_limit_block(exc):
            raise TranslatorBlocked(str(exc)) from exc
        logger.warning(
            "Google translation failed (%s → en): %s", source_lang, exc
        )
        return None


def run() -> dict:
    """Process all articles flagged ``translation_attempted = 0``."""
    if not _translation_enabled():
        logger.info("TRANSLATION_ENABLED=false; skipping translator")
        return {
            "candidates": 0, "detected": 0, "translated": 0,
            "skipped_already_target": 0, "skipped_no_lang": 0, "errors": 0,
        }

    rows = database.fetch_articles_needing_translation()
    # v0.3.1 — resolve once per run so a mid-run settings change doesn't
    # partially skip half the batch.
    target_langs = translation_target_langs()
    logger.info("Translator target languages (skipped): %s", sorted(target_langs))
    detected = 0
    translated = 0
    skipped_target = 0
    skipped_nolang = 0
    errors = 0

    for idx, row in enumerate(rows):
        text_for_detection = " ".join(
            filter(None, [row["title"] or "", row["snippet"] or ""])
        )[:500]
        # The collector pre-fills `language` from feed.sprache / outlet.sprache;
        # treat that as authoritative.
        source_lang = row["language"] if "language" in row.keys() else None
        lang = _detect_language(
            text_for_detection,
            row["country"],
            source_language=source_lang,
        )

        if not lang:
            database.mark_translation_attempted(row["id"], language=None)
            skipped_nolang += 1
            continue

        detected += 1

        if lang in target_langs:
            database.mark_translation_attempted(row["id"], language=lang)
            skipped_target += 1
            continue

        try:
            title_en = _translate(row["title"] or "", lang) if row["title"] else None
            snippet_en = _translate(row["snippet"] or "", lang) if row["snippet"] else None
        except TranslatorBlocked as exc:
            # Leave this and all remaining articles untouched (no attempt
            # recorded) so they are retried once Google lifts the block.
            logger.warning(
                "Google translation blocked (rate limit / CAPTCHA); "
                "stopping translator for this run, %d article(s) deferred: %s",
                len(rows) - idx, exc,
            )
            break

        if title_en or snippet_en:
            database.set_translation(
                row["id"],
                language=lang,
                title_en=title_en,
                snippet_en=snippet_en,
            )
            translated += 1
        else:
            # Don't claim a translation, and only give up after a few attempts —
            # Google's widget endpoint sporadically 429s and one bad run should
            # not write off an article forever.
            gave_up = database.record_translation_failure(
                row["id"],
                language=lang,
                max_attempts=MAX_TRANSLATION_ATTEMPTS,
            )
            errors += 1
            if gave_up:
                logger.warning(
                    "Translation gave up after %d attempts for article %s",
                    MAX_TRANSLATION_ATTEMPTS, row["id"],
                )

        time.sleep(TRANSLATE_DELAY_S)

    summary = {
        "candidates": len(rows),
        "detected": detected,
        "translated": translated,
        "skipped_already_target": skipped_target,
        "skipped_no_lang": skipped_nolang,
        "errors": errors,
    }
    logger.info("Translator summary: %s", summary)
    return summary

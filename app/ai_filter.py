"""Claude-based relevance scorer.

We send a small JSON-only prompt per article. Cost-control:
- Pre-screen against the high-priority keyword list — if no overlap, skip.
- Bound parallelism with an asyncio.Semaphore (default 5).
- Track approximate token usage in the log line.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence

from anthropic import APIError, APIStatusError, RateLimitError

from . import database, domain_utils, enums, feedback
from .config import VALID_CATEGORIES, VALID_THEMES, get_settings
from .models import AIScore, Keyword, NotionConfig
from .providers import create_ai_client

logger = logging.getLogger(__name__)


_SUGGEST_KEYWORDS_ADDENDUM = (
    "\n\n"
    "Keyword-suggestion mode is ON. In addition to the score you may propose "
    "1-3 short phrases (each in the article's original language) that would have "
    "caught this article via keyword pre-screening if they had been in the list. "
    "Only propose a phrase if:\n"
    "  - the article is genuinely on-topic (relevance_score >= 5), AND\n"
    "  - the phrase contains at least one word of >=4 characters, AND\n"
    "  - the phrase is neither the article title verbatim nor a full sentence.\n"
    "Return an empty list when nothing is worth suggesting; a firehose of "
    "vague single-word phrases makes the follow-up review useless."
)


def _suggest_keywords_enabled() -> bool:
    """Toggle for the F2 prompt addendum (Claude proposing keywords).

    Priority (v0.3.1): runtime setting > env flag > default false. The
    runtime setting is UI-toggleable in ``/settings``; the env flag
    stays as a documented CLI escape hatch. Default OFF so installs
    that don't opt in don't pay for extra output tokens.
    """
    import os

    from . import runtime_settings

    rt = runtime_settings.get(runtime_settings.SUGGEST_KEYWORDS_ENABLED)
    if isinstance(rt, bool):
        return rt
    raw = os.environ.get("SUGGEST_KEYWORDS", "false").strip().lower()
    return raw in {"1", "true", "yes", "on"}


SYSTEM_PROMPT = """You are a filter assistant for a fleet-transformation news monitor run by a Public Affairs team.

Task: Score how relevant a news article is to monitoring the fleet transformation of
long-distance buses/coaches (diesel → zero- or low-emission drivetrains).

In scope — score each of the following topics:
1. Funding programmes / subsidies for zero-emission buses and coaches: purchase premiums,
   calls for projects, tax incentives (including HVO), toll exemptions.
2. Any bus funding programme — including city buses and regional buses — because a
   new bus-funding scheme is a Public-Affairs opportunity: we may lobby to have it
   opened up to coaches. Score these on the merits of the funding signal, not on
   whether the programme mentions coaches today.
3. Policy & regulation (national or EU): CO2 fleet standards, ZEV mandates, AFIR,
   clean-vehicle rules, decarbonisation strategies relevant to coaches.
4. Charging & hydrogen infrastructure relevant to coaches: depots, corridors, en-route
   charging, megawatt charging.
5. Truck / HGV charging infrastructure funding and regulation — including MCS
   corridors, en-route depots, and grid-connection programmes primarily framed
   around trucks. Coaches share the same charging power class as trucks, so any
   truck-charging expansion is a placement opportunity for us.
6. Tenders & public procurement of zero-emission coach services.
7. Market moves: operators or OEMs piloting or ordering zero-emission coaches, new
   e-coach models, battery or range developments relevant to long distance.
8. Energy costs / supply where directly relevant to bus operations.

Priority weighting (soft guidance, no hard cutoff):
  Policy/regulatory and funding items score +1–2 higher than otherwise-equal
  market/technology items of the same informational value. Bus- and truck-adjacent
  funding items (points 2 and 5) are in-scope on the same footing as coach-specific
  funding — do NOT down-rank them just because coaches aren't named explicitly.

Geographic scope — ALL markets are to be scored EQUALLY — no bonus for DACH or
German-language content; a French or Norwegian item is exactly as important as a
German one. Markets covered: Benelux, DACH, EU, France, Italy, Nordics, Portugal.

Coach vocabulary (articles may use any of these terms):
  coach, long-distance bus, Reisebus, Fernbus (DE), autocar (FR),
  pullman / autobus a lunga percorrenza (IT), autocarro (PT),
  touringcar (NL), turistbuss/expressbuss (SV), turbuss/ekspressbuss (NO),
  turistbus/fjernbus (DA), linja-auto/kaukoliikenne (FI).

Guardrails:
- City-bus / regional-bus / truck operational news that is NOT about funding,
  policy, or charging infrastructure (e.g. a local operator retiring a diesel
  fleet, a truck-model launch) → low score. The point is coach-adjacency of
  funding/policy signals, not general adjacent-mode reporting.
- Pure vehicle PR without fleet-transformation relevance → low score.
- Advertisements, job postings, off-topic content → 0–1.
- Articles may be in any language — read in the original language and score fairly
  regardless of language.

Respond ONLY with valid JSON in the exact schema specified. Reasoning must be in English.
"""


USER_TEMPLATE = """Article:
- Title: {title}
- Source: {source}
- Market (per source): {market}
- Language: {language}
- Published: {published}
- Snippet: {snippet}

Return JSON with the fields:
{{
  "relevance_score": integer 0-10,
  "category": one of {categories},
  "themes": list from {themes} (may be empty),
  "reasoning": 1-2 sentences in English{suggest_field}
}}
"""

# Appended to USER_TEMPLATE's schema block when SUGGEST_KEYWORDS is on. The
# comma prefix keeps the JSON schema valid whether the field is present or
# not; when the flag is off the format-arg is an empty string.
_SUGGEST_FIELD_SNIPPET = (
    ',\n  "suggested_keywords": list of 0-3 lower-case phrases (each has a token >=4 chars) '
    "in the article's original language, that would have caught this article via keyword pre-screen"
)


# ---------------------------------------------------------------------------
# Pre-screening
# ---------------------------------------------------------------------------

# Tokens shorter than this are too generic to be meaningful (de/the/le/il/…).
_MIN_TOKEN_LEN = 4
# Word-character classes covering Latin-1 plus common diacritics across all
# target languages (DACH, FR, IT, PT, NL, SV/NO/DA/FI). The hyphen lets
# compounds like "e-bus" survive tokenisation.
_TOKEN_RE = re.compile(
    r"[\wÀ-ſ-]+",
    re.UNICODE,
)


def _significant_tokens(phrase: str) -> List[str]:
    """Lower-cased word tokens of length >= _MIN_TOKEN_LEN, hyphenated
    compounds preserved."""
    return [
        tok.lower()
        for tok in _TOKEN_RE.findall(phrase or "")
        if len(tok) >= _MIN_TOKEN_LEN
    ]


def _high_priority_tokens(config: NotionConfig) -> List[str]:
    """Union of significant tokens across all High-priority PLAIN (non-combo) keywords.

    Switching from full-phrase substring to token-level matching is the key
    change that lets non-DE pre-screening work: a French keyword like
    "appel à projets autocar électrique" would never appear verbatim in an
    article title, but "autocar" or "électrique" usually do.

    Combo keywords (containing '+') are intentionally excluded here; their
    matching is handled separately by _combo_configs / _passes_pre_screen.
    """
    seen: set[str] = set()
    for kw in config.keywords:
        if not enums.is_high_priority(kw.prioritaet) or not kw.suchbegriff:
            continue
        if "+" in kw.suchbegriff:
            continue  # combo — handled separately
        for tok in _significant_tokens(kw.suchbegriff):
            seen.add(tok)
    return list(seen)


def _combo_configs(config: NotionConfig) -> List[List[List[str]]]:
    """Return validated combo keyword segment-token lists.

    A 'combo' keyword has '+' in its suchbegriff (e.g. 'autocar+subvention').
    Each '+'-delimited segment is tokenised; significant tokens (>= _MIN_TOKEN_LEN)
    are collected.

    A segment with NO significant tokens is un-matchable. Such combos are silently
    ignored and logged — they can never contribute to a pre-screen pass.

    Returns: list of combos, each combo is a list of segment-token-lists.
    Example: 'autocar+subvention' → [['autocar'], ['subvention']]
    """
    result: List[List[List[str]]] = []
    for kw in config.keywords:
        if not enums.is_high_priority(kw.prioritaet) or not kw.suchbegriff:
            continue
        if "+" not in kw.suchbegriff:
            continue
        segments = kw.suchbegriff.split("+")
        combo: List[List[str]] = []
        valid = True
        for seg in segments:
            tokens = _significant_tokens(seg)
            if not tokens:
                # All tokens in this segment are < _MIN_TOKEN_LEN chars —
                # the segment is un-matchable, so the entire combo is discarded.
                logger.debug(
                    "Pre-screen combo '%s': segment '%s' has no significant tokens "
                    "(all tokens < %d chars) — combo ignored.",
                    kw.suchbegriff, seg, _MIN_TOKEN_LEN,
                )
                valid = False
                break
            combo.append(tokens)
        if valid and combo:
            result.append(combo)
    return result


def _row_haystack(row) -> str:
    """Lowercased blob across original + English-translated title/snippet, so
    a keyword in any language can match articles that have been translated
    by the translator stage."""
    parts: List[str] = [row["title"] or "", row["snippet"] or ""]
    keys = row.keys()
    if "title_en" in keys and row["title_en"]:
        parts.append(row["title_en"])
    if "snippet_en" in keys and row["snippet_en"]:
        parts.append(row["snippet_en"])
    return " ".join(parts).lower()


def _passes_pre_screen(
    row,
    tokens: Sequence[str],
    *,
    config: Optional[NotionConfig] = None,
) -> bool:
    """Return True if the article passes the pre-screen.

    Pass conditions (OR):
    1. Any plain-keyword token appears in the haystack.
    2. Any validated combo keyword fully matches (every segment has >=1 token present).

    If both plain tokens AND combos are absent (user has no high-priority keywords),
    everything passes (send-all behaviour).
    """
    combos = _combo_configs(config) if config is not None else []
    if not tokens and not combos:
        # No high-priority keywords configured → send everything to AI
        return True
    haystack = _row_haystack(row)
    # Plain keyword check: any token in haystack
    if any(tok in haystack for tok in tokens):
        return True
    # Combo keyword check: every segment must have >=1 token present
    for combo in combos:
        if all(any(tok in haystack for tok in seg_tokens) for seg_tokens in combo):
            return True
    return False


def _is_recent(row, *, max_age_days: int) -> bool:
    """True when the article is fresh enough to bypass the keyword pre-screen.

    Falls back to collected_at when published_at is missing — Google News and
    Bing don't always populate the publication date in their RSS responses.
    """
    if max_age_days <= 0:
        return False
    raw = row["published_at"] or row["collected_at"]
    if not raw:
        return False
    try:
        ts = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)
    return ts >= cutoff


# ---------------------------------------------------------------------------
# Claude call
# ---------------------------------------------------------------------------

def _coerce_score(payload: Dict[str, Any]) -> AIScore:
    raw_score = payload.get("relevance_score", 0)
    try:
        score = max(0, min(10, int(raw_score)))
    except (TypeError, ValueError):
        score = 0

    category = payload.get("category") or "Other"
    if category not in VALID_CATEGORIES:
        category = "Other"

    themes_raw = payload.get("themes") or []
    if not isinstance(themes_raw, list):
        themes_raw = []
    themes = [t for t in themes_raw if t in VALID_THEMES]

    reasoning = str(payload.get("reasoning") or "")[:1000]

    # F2: parse suggested_keywords defensively. Strip + lowercase, require
    # at least one >=4-char token per phrase, dedupe, then cap at 3
    # (the cap comes AFTER filtering so junk entries don't push good ones
    # past the limit).
    suggestions: List[str] = []
    raw_suggestions = payload.get("suggested_keywords") or []
    if isinstance(raw_suggestions, list):
        seen: set[str] = set()
        for entry in raw_suggestions:
            if not isinstance(entry, str):
                continue
            phrase = entry.strip().lower()
            if not phrase or phrase in seen:
                continue
            if not _significant_tokens(phrase):
                continue
            # 80-char cap keeps single phrases from swallowing a whole title.
            suggestions.append(phrase[:80])
            seen.add(phrase)
            if len(suggestions) >= 3:
                break

    return AIScore(
        relevance_score=score,
        category=category,
        themes=themes,
        reasoning=reasoning,
        suggested_keywords=suggestions,
    )


def _extract_json(text: str) -> Dict[str, Any]:
    """Be lenient about JSON wrapping."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        # remove any leading 'json' fence label
        if text.lower().startswith("json"):
            text = text[4:]
        text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model output: {text[:200]}")
    return json.loads(text[start : end + 1])


def _format_examples(examples: List[feedback.FeedbackExample]) -> str:
    if not examples:
        return ""
    lines = [
        "",
        "",
        "Previous ratings from this user's workflow (taste examples):",
    ]
    for ex in examples:
        lines.append(
            f"\n[{ex.label}] (final score {ex.score}) {ex.title}"
            f"\n  Source: {ex.source} | Market: {ex.market or 'n/a'}"
            f"\n  Snippet: {ex.snippet}"
        )
    lines.append(
        "\n\nUse these examples as a reference — they show what the user considers "
        "highly relevant (INTERESTING) vs. uninteresting (IRRELEVANT). "
        "Score the new article in the same spirit."
    )
    return "\n".join(lines)


# Status codes worth retrying with backoff (besides 429, which has its own type).
_RETRYABLE_STATUS = {500, 502, 503, 504, 529}
_MAX_ATTEMPTS = 5
_BASE_BACKOFF = 2.0
_MAX_BACKOFF = 30.0


def _retry_after_seconds(exc: APIStatusError) -> Optional[float]:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) if response is not None else None
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


async def _create_with_retry(
    client,
    *,
    model: str,
    system: str,
    user_prompt: str,
    article_id: Any,
    max_tokens: int = 512,
):
    """Call messages.create with bounded retry on 429 and transient 5xx.

    The Anthropic SDK is configured with max_retries=0 so this function owns
    all retry behaviour. Honors the `retry-after` header on rate-limit errors
    and adds jitter so concurrent workers don't burst the limit again in lockstep.
    """
    backoff = _BASE_BACKOFF
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            # max_tokens is taken from settings.ai_max_tokens (default 512).
            # Our JSON score response is well under 200 tokens, so 512 is plenty
            # of headroom while staying within the SDK's non-streaming threshold.
            return await client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except RateLimitError as exc:
            wait = _retry_after_seconds(exc) or backoff
            wait += random.uniform(0, 1.0)
            if attempt == _MAX_ATTEMPTS:
                logger.warning(
                    "Claude rate limit, giving up after %d attempts (article %s)",
                    attempt, article_id,
                )
                return None
            logger.info(
                "Claude rate limit, retrying in %.1fs (attempt %d/%d, article %s)",
                wait, attempt, _MAX_ATTEMPTS, article_id,
            )
            await asyncio.sleep(wait)
            backoff = min(backoff * 2, _MAX_BACKOFF)
        except APIStatusError as exc:
            if exc.status_code in _RETRYABLE_STATUS and attempt < _MAX_ATTEMPTS:
                wait = backoff + random.uniform(0, 1.0)
                logger.info(
                    "Claude %s, retrying in %.1fs (attempt %d/%d, article %s)",
                    exc.status_code, wait, attempt, _MAX_ATTEMPTS, article_id,
                )
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, _MAX_BACKOFF)
                continue
            logger.warning("Claude API error for article %s: %s", article_id, exc)
            return None
        except APIError as exc:
            logger.warning("Claude API error for article %s: %s", article_id, exc)
            return None
        except Exception as exc:
            logger.exception("Unexpected Claude error for article %s: %s", article_id, exc)
            return None
    return None


async def _score_one(
    client,
    *,
    model: str,
    max_tokens: int,
    row,
    sem: asyncio.Semaphore,
    examples: List[feedback.FeedbackExample],
    suggest_on: bool = False,
) -> Optional[AIScore]:
    # If we have an English translation, append it after the original so
    # Claude sees both — useful for languages it might score less reliably.
    title = (row["title"] or "")[:300]
    snippet = (row["snippet"] or "")[:1500]
    title_en = row["title_en"] if "title_en" in row.keys() else None
    snippet_en = row["snippet_en"] if "snippet_en" in row.keys() else None
    language = row["language"] if "language" in row.keys() else None
    if title_en and title_en.strip().lower() != title.strip().lower():
        title = f"{title}\n  EN: {title_en[:300]}"
    if snippet_en and snippet_en.strip() != snippet.strip():
        snippet = f"{snippet}\n  EN: {snippet_en[:1500]}"

    user_prompt = USER_TEMPLATE.format(
        title=title,
        source=row["source_name"] or "",
        market=row["country"] or "",
        language=(language or "unknown"),
        published=row["published_at"] or "",
        snippet=snippet,
        categories=", ".join(VALID_CATEGORIES),
        themes=", ".join(VALID_THEMES),
        suggest_field=_SUGGEST_FIELD_SNIPPET if suggest_on else "",
    ) + _format_examples(examples)
    system_prompt = SYSTEM_PROMPT + (_SUGGEST_KEYWORDS_ADDENDUM if suggest_on else "")
    async with sem:
        resp = await _create_with_retry(
            client,
            model=model,
            system=system_prompt,
            user_prompt=user_prompt,
            article_id=row["id"],
            max_tokens=max_tokens,
        )
        if resp is None:
            return None

    text_blocks = [b.text for b in resp.content if getattr(b, "type", "") == "text"]
    raw = "\n".join(text_blocks).strip()
    try:
        payload = _extract_json(raw)
    except Exception as exc:
        logger.warning(
            "Could not parse Claude JSON for article %s: %s | raw=%s",
            row["id"], exc, raw[:300],
        )
        return None

    score = _coerce_score(payload)

    # Track approximate token usage for cost logging
    usage = getattr(resp, "usage", None)
    if usage:
        score_usage = getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)
        logger.debug(
            "Article %s scored=%s tokens(in/out)=%s",
            row["id"], score.relevance_score, score_usage,
        )
    return score


# ---------------------------------------------------------------------------
# Trust bonus
# ---------------------------------------------------------------------------

TRUST_BONUS_BY_PRIORITY = {
    enums.PRIORITY_HIGH: 2,
    enums.PRIORITY_MEDIUM: 1,
    enums.PRIORITY_LOW: 0,
}


def _trust_bonus_for(url: str) -> int:
    match = domain_utils.match_article_to_outlet(url)
    if not match:
        return 0
    canonical = enums.normalise_priority(match.get("priority"))
    return TRUST_BONUS_BY_PRIORITY.get(canonical or "", 0)


# ---------------------------------------------------------------------------
# Keyword suggestions (F2)
# ---------------------------------------------------------------------------

def _normalise_phrase(phrase: str) -> str:
    """Group-key for dedup: sorted significant tokens, whitespace-joined.

    ``PNRR autobus elettrici`` and ``autobus PNRR elettrici`` collapse to
    the same key. Empty when the phrase contributes no significant tokens
    (caller should skip the suggestion in that case)."""
    toks = _significant_tokens(phrase)
    if not toks:
        return ""
    return " ".join(sorted(set(toks)))


def _existing_keyword_token_sets(config: NotionConfig) -> List[frozenset]:
    """Frozen token-sets of every configured keyword (any priority, incl.
    combos with '+' expanded). Used to dedup new suggestions against the
    curated catalogue — both directions: incoming ⊆ existing OR existing ⊆
    incoming both count as duplicates."""
    sets: List[frozenset] = []
    for kw in config.keywords:
        if not kw.suchbegriff:
            continue
        # Combo keywords: use the token union of all segments as the set.
        expanded = kw.suchbegriff.replace("+", " ")
        toks = _significant_tokens(expanded)
        if toks:
            sets.append(frozenset(toks))
    return sets


def _is_dup_against_existing(
    phrase_tokens: frozenset, existing_sets: List[frozenset]
) -> bool:
    """Bidirectional subset check.

    Returns True when the new phrase adds no new query surface — either
    the existing catalogue already contains a superset of these tokens or
    a subset that would fire anyway on the same pre-screen path.
    """
    if not phrase_tokens:
        return True
    for existing in existing_sets:
        if phrase_tokens <= existing or existing <= phrase_tokens:
            return True
    return False


def _persist_suggestions(
    row,
    phrases: Sequence[str],
    *,
    config: NotionConfig,
    existing_sets: List[frozenset],
    pending_seen: set,
) -> int:
    """Persist Claude's ``suggested_keywords`` for a single article.

    ``pending_seen`` is an in-memory set of ``phrase_normalized`` values
    already inserted this run (or pre-existing in the DB); it is mutated
    in-place as new ones are added, so subsequent calls in the same run
    don't spam duplicates.

    Returns the number of rows actually inserted (0 when everything was
    filtered).
    """
    if not phrases:
        return 0

    market = row["country"] if "country" in row.keys() else None
    lang = row["language"] if "language" in row.keys() else None
    inserted = 0
    for raw_phrase in phrases:
        phrase = (raw_phrase or "").strip().lower()
        if not phrase:
            continue
        norm = _normalise_phrase(phrase)
        if not norm or norm in pending_seen:
            continue
        toks = frozenset(_significant_tokens(phrase))
        if _is_dup_against_existing(toks, existing_sets):
            continue
        try:
            database.insert_keyword_suggestion(
                phrase=phrase,
                phrase_normalized=norm,
                source_article_id=row["id"],
                market=market,
                sprache=(lang.upper() if lang else None),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "Failed to persist keyword suggestion %r: %s", phrase, exc,
            )
            continue
        pending_seen.add(norm)
        inserted += 1
    return inserted


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run(
    config: NotionConfig, *, skip_prescreen_days: Optional[int] = None
) -> Dict[str, int]:
    """Score unprocessed articles with Claude.

    ``skip_prescreen_days`` overrides the recent-article prescreen bypass
    window (runtime setting, plumbed in by ``app.pipeline``); ``None`` falls
    back to the ``AI_SKIP_PRESCREEN_DAYS`` env default.
    """
    settings = get_settings()

    # Cowork mode: the tool doesn't score. Articles stay with
    # ai_filtered=0 in the DB so a subsequent /api/articles/pending call
    # from the Cowork session can pick them up. We log the count and
    # return early — no Anthropic client is opened.
    if settings.ai_provider == "cowork":
        pending = database.fetch_articles_for_ai()
        logger.info(
            "AI provider is 'cowork'; skipping in-process scoring. "
            "%d article(s) left pending for external ranking.",
            len(pending),
        )
        return {
            "sent_to_ai": 0,
            "skipped_pre_screen": 0,
            "scored": 0,
            "external_scoring": True,
            "pending_for_cowork": len(pending),
        }

    try:
        client = create_ai_client(settings)
    except ValueError as exc:
        logger.error(
            "Cannot create AI client; skipping AI filter. %s", exc,
        )
        return {"sent_to_ai": 0, "skipped_pre_screen": 0, "scored": 0}

    rows = database.fetch_articles_for_ai()
    high_tokens = _high_priority_tokens(config)
    if skip_prescreen_days is None:
        skip_prescreen_days = settings.ai_skip_prescreen_days

    pre_pass: List = []
    pre_skip = 0
    bypassed_recent = 0
    for row in rows:
        # Articles published within the bypass window go straight to Claude —
        # the pre-screen only exists for cost control on large backlogs, and
        # recent articles are worth scoring unconditionally, so they skip it.
        if _is_recent(row, max_age_days=skip_prescreen_days):
            pre_pass.append(row)
            bypassed_recent += 1
            continue
        if _passes_pre_screen(row, high_tokens, config=config):
            pre_pass.append(row)
        else:
            pre_skip += 1
            # Mark as ai_filtered so we don't re-evaluate next run; the
            # reasoning column is stamped with a "Pre-screen: ..." tag so the
            # filtered-articles view can bucket these separately.
            database.update_ai_score(
                row["id"],
                relevance_score=0,
                trust_bonus=0,
                category="Other",
                themes=[],
                reasoning="Pre-screen: no high-priority keyword matched.",
            )

    if not pre_pass:
        logger.info("AI filter: nothing to score (pre-screen skipped %d)", pre_skip)
        return {
            "sent_to_ai": 0,
            "skipped_pre_screen": pre_skip,
            "bypassed_prescreen_recent": bypassed_recent,
            "scored": 0,
        }

    sem = asyncio.Semaphore(settings.ai_filter_max_parallel)

    # Few-shot: pull the user's recent star/ignore signals once per run.
    examples = feedback.recent_examples()
    if examples:
        logger.info("AI filter: using %d feedback examples in prompt", len(examples))

    # Resolve the SUGGEST_KEYWORDS toggle once per run (runtime setting hits
    # SQLite; per-article calls would be wasteful).
    suggest_on = _suggest_keywords_enabled()

    tasks = [
        _score_one(
            client,
            model=settings.anthropic_model,
            max_tokens=settings.ai_max_tokens,
            row=row,
            sem=sem,
            examples=examples,
            suggest_on=suggest_on,
        )
        for row in pre_pass
    ]
    results = await asyncio.gather(*tasks)

    # F2: pre-compute the dedup surface once per run (existing keyword
    # token-sets from the loaded config + pending suggestion norms already
    # in the DB from prior runs). Both are read-only during the run.
    if suggest_on:
        existing_keyword_sets = _existing_keyword_token_sets(config)
        pending_seen: set = database.pending_suggestion_norms()
    else:
        existing_keyword_sets = []
        pending_seen = set()

    scored = 0
    feedback_applied = 0
    suggestions_persisted = 0
    for row, score in zip(pre_pass, results):
        if score is None:
            # Leave ai_filtered=0 so we retry next run
            continue
        trust = _trust_bonus_for(row["url"])
        fb_bonus = feedback.domain_adjustment(row["url"])
        if fb_bonus:
            feedback_applied += 1
        database.update_ai_score(
            row["id"],
            relevance_score=score.relevance_score,
            trust_bonus=trust,
            feedback_bonus=fb_bonus,
            category=score.category,
            themes=score.themes,
            reasoning=score.reasoning,
        )
        scored += 1
        if suggest_on and score.suggested_keywords:
            suggestions_persisted += _persist_suggestions(
                row,
                score.suggested_keywords,
                config=config,
                existing_sets=existing_keyword_sets,
                pending_seen=pending_seen,
            )

    summary = {
        "sent_to_ai": len(pre_pass),
        "skipped_pre_screen": pre_skip,
        "bypassed_prescreen_recent": bypassed_recent,
        "scored": scored,
        "feedback_examples_used": len(examples),
        "feedback_domain_adjustments": feedback_applied,
        "suggestions_persisted": suggestions_persisted,
    }
    logger.info("AI filter summary: %s", summary)
    return summary

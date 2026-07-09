"""Tests for ai_filter pre-screen logic: plain keywords and combo keywords.

Combo-keyword spec (Task 2 / Item 3):
  A keyword whose suchbegriff contains '+' (e.g. 'autocar+subvention') is a combo.
  Each '+'-delimited segment is tokenised independently.
  The combo matches only if EVERY segment has >=1 significant token present in the
  haystack.  Plain (non-combo) keywords keep their existing ANY-token behaviour.
  Pre-screen passes if (any plain-keyword token matches) OR (any combo fully matches).
  A segment whose all tokens are <4 chars is non-matchable; the whole combo is
  silently ignored (cannot contribute to a pass).
"""

from __future__ import annotations

from app.ai_filter import _high_priority_tokens, _combo_configs, _passes_pre_screen
from app.models import Keyword, NotionConfig


# ---------------------------------------------------------------------------
# Helpers to build minimal test configs
# ---------------------------------------------------------------------------

def _kw(term: str, priority: str = "High") -> Keyword:
    return Keyword(suchbegriff=term, prioritaet=priority)


def _cfg(*terms: str, priority: str = "High") -> NotionConfig:
    return NotionConfig(keywords=[_kw(t, priority) for t in terms])


def _row(title: str = "", snippet: str = "") -> dict:
    """Minimal article row dict for pre-screen tests."""
    return {
        "title": title,
        "snippet": snippet,
        "title_en": None,
        "snippet_en": None,
        "keys": lambda: ["title", "snippet", "title_en", "snippet_en"],
    }


# We need a row that looks like a sqlite3.Row (has .keys() method)
class _MockRow(dict):
    def keys(self):
        return super().keys()


def _make_row(title: str = "", snippet: str = "", title_en=None, snippet_en=None) -> _MockRow:
    return _MockRow(title=title, snippet=snippet, title_en=title_en, snippet_en=snippet_en)


# ---------------------------------------------------------------------------
# Plain keyword tests (existing behaviour must not regress)
# ---------------------------------------------------------------------------

def test_plain_keyword_matches_token_in_title():
    cfg = _cfg("autocar électrique")
    tokens = _high_priority_tokens(cfg)
    row = _make_row(title="Subvention pour autocar électrique en France")
    assert _passes_pre_screen(row, tokens)


def test_plain_keyword_no_match():
    cfg = _cfg("wasserstoffbus")
    tokens = _high_priority_tokens(cfg)
    row = _make_row(title="City bike sharing programme launched")
    assert not _passes_pre_screen(row, tokens)


def test_low_priority_keyword_excluded_from_tokens():
    cfg = _cfg("wasserstoffbus", priority="Low")
    tokens = _high_priority_tokens(cfg)
    # Low-priority keywords must NOT contribute tokens to the pre-screen
    assert tokens == []
    row = _make_row(title="Wasserstoffbus Förderung gestartet")
    # Without tokens, _passes_pre_screen returns True (send-all behaviour)
    assert _passes_pre_screen(row, tokens)


# ---------------------------------------------------------------------------
# Combo keyword extraction
# ---------------------------------------------------------------------------

def test_combo_configs_extracts_plus_keywords():
    """_combo_configs must return only High-priority '+'-containing keywords."""
    cfg = NotionConfig(keywords=[
        _kw("autocar+subvention"),
        _kw("e-bus"),            # plain, should not appear
        _kw("hydrogen+bus+funding", priority="Low"),  # low-priority → excluded
    ])
    combos = _combo_configs(cfg)
    assert len(combos) == 1
    assert combos[0] == [["autocar"], ["subvention"]]


def test_combo_configs_multi_segment():
    cfg = _cfg("zero+emission+coach")
    combos = _combo_configs(cfg)
    # Each segment tokenised: "zero", "emission", "coach" all >= 4 chars
    # Shape: list of combos; each combo is list of segment-token-lists
    assert combos == [[["zero"], ["emission"], ["coach"]]]


def test_combo_configs_empty_when_no_combos():
    cfg = _cfg("e-bus", "Förderung", "autocar")
    assert _combo_configs(cfg) == []


# ---------------------------------------------------------------------------
# Combo matching
# ---------------------------------------------------------------------------

def test_combo_passes_when_all_segments_match():
    cfg = NotionConfig(keywords=[
        _kw("autocar+subvention"),
    ])
    tokens = _high_priority_tokens(cfg)  # plain tokens only (none here)
    row = _make_row(title="Nouvelle subvention pour les autocars en France")
    assert _passes_pre_screen(row, tokens, config=cfg)


def test_combo_fails_when_only_one_segment_matches():
    """Missing 'subvention' → combo must NOT match."""
    cfg = NotionConfig(keywords=[
        _kw("autocar+subvention"),
    ])
    tokens = _high_priority_tokens(cfg)
    row = _make_row(title="Les autocars sont populaires en France")
    assert not _passes_pre_screen(row, tokens, config=cfg)


def test_combo_fails_when_no_segment_matches():
    cfg = NotionConfig(keywords=[
        _kw("autocar+subvention"),
    ])
    tokens = _high_priority_tokens(cfg)
    row = _make_row(title="Bus électrique annoncé à Berlin")
    assert not _passes_pre_screen(row, tokens, config=cfg)


def test_combo_pass_via_snippet():
    """Combo segments can match across title and snippet."""
    cfg = NotionConfig(keywords=[_kw("autocar+funding")])
    tokens = _high_priority_tokens(cfg)
    row = _make_row(title="Autocar news", snippet="New funding programme announced")
    assert _passes_pre_screen(row, tokens, config=cfg)


def test_mixed_config_plain_passes_without_combo_match():
    """A plain keyword match should pass even if the combo does NOT match."""
    cfg = NotionConfig(keywords=[
        _kw("e-bus"),             # plain: 'e-bus' has token 'e-bus' (len 5 ✓)
        _kw("autocar+subvention"),  # combo: needs both
    ])
    tokens = _high_priority_tokens(cfg)
    # Title only matches plain keyword 'e-bus', not combo
    row = _make_row(title="New e-bus model released, no funding")
    assert _passes_pre_screen(row, tokens, config=cfg)


def test_mixed_config_combo_passes_when_plain_doesnt_match():
    """Combo match should pass even if plain keywords do NOT match."""
    cfg = NotionConfig(keywords=[
        _kw("wasserstoffbus"),    # plain: needs 'wasserstoffbus'
        _kw("autocar+subvention"),  # combo
    ])
    tokens = _high_priority_tokens(cfg)
    row = _make_row(title="Nouvelle subvention pour les autocars")
    assert _passes_pre_screen(row, tokens, config=cfg)


def test_all_short_segment_combo_ignored():
    """A segment whose tokens are ALL <4 chars is non-matchable → combo silently ignored."""
    # 'au' is 2 chars (non-matchable segment), so combo should be ignored entirely.
    cfg = NotionConfig(keywords=[
        _kw("au+subvention"),  # 'au' → all tokens < 4 chars → combo ignored
    ])
    tokens = _high_priority_tokens(cfg)
    combos = _combo_configs(cfg)
    # Combo should be excluded because 'au' segment has no significant tokens
    assert combos == []
    # Without any plain tokens or valid combos: send-all behaviour
    row = _make_row(title="subvention pour les bus")
    assert _passes_pre_screen(row, tokens, config=cfg)

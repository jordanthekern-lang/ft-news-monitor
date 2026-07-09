"""Export article rows to Markdown (Confluence-friendly) and CSV (Excel-friendly).

Both entrypoints take the same article-dict shape ``database.list_articles``
returns — a ``sqlite3.Row`` coerced through ``dict()`` — so the caller can pass
rows straight through without a projection step.

Design notes
------------
* Articles are single-market (they carry a scalar ``country``); an empty
  ``country`` is grouped as ``"Global"``. Since one row belongs to at most one
  market, the "multi-market row → appears once per market" question in the
  brief collapses to "appears once, under its ``country`` group".
* Markdown group order follows :data:`app.config.VALID_MARKETS` (dashboard
  order — DACH, Benelux, France, Italy, Nordics, Portugal, EU) so weekly
  recaps land in a stable order that matches the UI; unknown/legacy country
  values are appended alphabetically after the known set, with ``"Global"``
  last.
* Within a group, articles sort by ``final_score`` DESC then ``published_at``
  DESC (matches ``database.list_articles``' ORDER BY).
* Dates render as ``dd.mm.yyyy`` across both formats — matches the dashboard's
  ``date_only`` filter.
* CSV is UTF-8 with a BOM so Excel auto-detects the encoding when
  double-clicked (Italian accents, Nordic diacritics — otherwise Excel treats
  the file as Windows-1252 and mangles them).
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from typing import Any, Iterable, List, Optional

from .config import VALID_MARKETS

_GLOBAL_GROUP = "Global"

# UTF-8 byte-order mark; Excel uses this to auto-detect the encoding.
_UTF8_BOM = b"\xef\xbb\xbf"

# One-line reasoning quotes (blockquote). If the model dumps a paragraph with
# newlines, collapse them so the blockquote stays a single visual line.
_MAX_REASONING_CHARS = 500

# CSV cells whose first character is one of these are interpreted by Excel /
# LibreOffice as a formula on double-click (=HYPERLINK, =cmd|..., etc.). See
# CWE-1236 / OWASP "CSV injection". Titles arrive from untrusted RSS feeds,
# so any free-form string column must be neutralised before serialisation.
_CSV_FORMULA_TRIGGERS = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(value: Any) -> str:
    """Neutralize CSV formula injection (CWE-1236).

    Excel treats cells beginning with ``=`` / ``+`` / ``-`` / ``@`` / tab / CR
    as formulas on double-click. News article titles arrive from untrusted RSS
    feeds and can plant ``=HYPERLINK(...)`` / ``=cmd|...`` payloads. Prefixing
    with an apostrophe forces Excel to treat the cell as literal text; the
    apostrophe is stripped on display, so the visible value is unchanged.

    Applied to free-form string columns only — numeric / enum / pre-formatted
    date cells never begin with a trigger character in this codebase.
    """
    if value is None:
        return ""
    s = str(value)
    if s and s[0] in _CSV_FORMULA_TRIGGERS:
        return "'" + s
    return s


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _final_score(article: dict) -> int:
    return (
        (article.get("relevance_score") or 0)
        + (article.get("trust_bonus") or 0)
        + (article.get("feedback_bonus") or 0)
    )


def _parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_date(value: Optional[str]) -> str:
    dt = _parse_iso(value)
    if dt is None:
        return ""
    return dt.strftime("%d.%m.%Y")


def _themes(article: dict) -> List[str]:
    raw = article.get("themes")
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(t) for t in raw]
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if isinstance(parsed, list):
        return [str(t) for t in parsed]
    return []


def _market(article: dict) -> str:
    country = article.get("country") or ""
    country = country.strip()
    return country or _GLOBAL_GROUP


def _bool(value: Any) -> bool:
    """SQLite exposes booleans as ints; coerce truthiness explicitly."""
    return bool(value)


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


def _sort_key(article: dict) -> tuple:
    """Score DESC then published_at DESC — matches list_articles ORDER BY."""
    published = article.get("published_at") or ""
    # Descending sorts: negate score, invert string via reverse in the caller.
    return (-_final_score(article), published)


def _group_order(groups: Iterable[str]) -> List[str]:
    """Order groups by VALID_MARKETS first, then unknown countries alphabetically,
    then the "Global" bucket last."""
    seen = set(groups)
    ordered: List[str] = []
    for market in VALID_MARKETS:
        if market in seen:
            ordered.append(market)
            seen.discard(market)
    # Unknown / legacy markets, alphabetical, before Global.
    extras = sorted(m for m in seen if m != _GLOBAL_GROUP)
    ordered.extend(extras)
    if _GLOBAL_GROUP in seen:
        ordered.append(_GLOBAL_GROUP)
    return ordered


def _collapse_whitespace(text: str) -> str:
    """Blockquotes need single-line content; collapse internal whitespace."""
    return " ".join(text.split())


def _markdown_line(article: dict, *, include_reasoning: bool) -> List[str]:
    """Render one article as one or two markdown lines (bullet + optional quote)."""
    title = article.get("title_en") or article.get("title") or "(untitled)"
    if _bool(article.get("starred")):
        title = f"⭐ {title}"

    url = article.get("url") or ""
    source = article.get("source_name") or "unknown"
    published = _fmt_date(article.get("published_at"))
    score = _final_score(article)
    trust = article.get("trust_bonus") or 0
    category = (article.get("category") or "").strip()
    themes = [t for t in _themes(article) if t]

    score_part = f"score {score}"
    if trust:
        score_part += f" (+{trust} trust)"

    # Assemble segments, dropping any empty ones so we never emit " —  — ".
    parts: List[str] = []
    parts.append(f"**[{title}]({url})**" if url else f"**{title}**")
    parts.append(f"*{source}*, {published}" if published else f"*{source}*")
    parts.append(score_part)
    if category:
        parts.append(category)
    if themes:
        parts.append(", ".join(themes))

    line = "- " + " — ".join(parts)
    lines = [line]

    if include_reasoning:
        reasoning = (article.get("relevance_reasoning") or "").strip()
        if reasoning:
            snippet = _collapse_whitespace(reasoning)
            if len(snippet) > _MAX_REASONING_CHARS:
                snippet = snippet[: _MAX_REASONING_CHARS - 1].rstrip() + "…"
            lines.append(f"  > {snippet}")
    return lines


def build_markdown(
    articles: List[dict],
    *,
    include_reasoning: bool = False,
    today: Optional[datetime] = None,
) -> str:
    """Render articles as Confluence-friendly markdown, grouped by market.

    Group order follows :data:`VALID_MARKETS`; empty ``country`` → "Global"
    (rendered last). Within each group, articles are sorted by final score
    DESC then ``published_at`` DESC.
    """
    today = today or datetime.now(timezone.utc)
    date_label = today.strftime("%d.%m.%Y")

    grouped: dict[str, List[dict]] = {}
    for row in articles:
        grouped.setdefault(_market(row), []).append(row)

    out: List[str] = [f"# FT News Monitor — {date_label}", ""]
    if not grouped:
        out.append("_No articles matching the current filters._")
        return "\n".join(out) + "\n"

    for group in _group_order(grouped.keys()):
        items = sorted(grouped[group], key=_sort_key)
        out.append(f"## {group}")
        out.append("")
        for article in items:
            out.extend(_markdown_line(article, include_reasoning=include_reasoning))
        out.append("")
    # Ensure exactly one trailing newline.
    return "\n".join(out).rstrip() + "\n"


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------


CSV_COLUMNS = (
    "published_date",
    "market",
    "title",
    "title_en",
    "url",
    "source",
    "category",
    "themes",
    "relevance_score",
    "trust_bonus",
    "starred",
    "status",
    "reasoning",
)


def _status_label(article: dict) -> str:
    """Best-effort single-word status matching the dashboard status filter."""
    if _bool(article.get("ignored")):
        return "ignored"
    if _bool(article.get("starred")):
        return "starred"
    if _bool(article.get("read")):
        return "read"
    return "new"


def _bool_str(value: Any) -> str:
    return "true" if _bool(value) else "false"


def build_csv(articles: List[dict]) -> bytes:
    """Serialise articles to a UTF-8-with-BOM CSV. One row per article.

    Column order is fixed by :data:`CSV_COLUMNS`. Multi-value cells (themes)
    are comma-joined so the single cell remains machine-readable in Excel.
    The market cell joins with ``;`` — currently articles are single-market,
    but this leaves headroom without breaking the schema if that changes.
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for row in articles:
        market = _market(row)
        themes = _themes(row)
        # Every free-form string cell is passed through _csv_safe so untrusted
        # feed content can't smuggle Excel formulas (CWE-1236). Numeric and
        # enum columns are left as-is (they never begin with a trigger).
        writer.writerow(
            [
                _fmt_date(row.get("published_at")),
                _csv_safe(market),
                _csv_safe(row.get("title") or ""),
                _csv_safe(row.get("title_en") or ""),
                _csv_safe(row.get("url") or ""),
                _csv_safe(row.get("source_name") or ""),
                _csv_safe(row.get("category") or ""),
                _csv_safe(", ".join(themes)),
                _final_score(row),
                row.get("trust_bonus") or 0,
                _bool_str(row.get("starred")),
                _status_label(row),
                _csv_safe((row.get("relevance_reasoning") or "").strip()),
            ]
        )
    return _UTF8_BOM + buf.getvalue().encode("utf-8")

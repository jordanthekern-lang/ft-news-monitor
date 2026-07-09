"""Validate one or more seed packs against the canonical schema.

Usage:
    python scripts/validate_seeds.py                     # all seeds/*.json
    python scripts/validate_seeds.py seeds/dach.json     # explicit files
    python scripts/validate_seeds.py --network           # also fetch RSS feeds

Exit codes:
    0 — every file validated cleanly (warnings allowed)
    1 — at least one file has an error

The offline validation core is exercised by pytest (see tests/); the
``--network`` mode is manual-only and never runs in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import (  # noqa: E402
    VALID_CATEGORIES,
    VALID_MARKETS,
    VALID_THEMES,
    _CATEGORY_ALIASES,
    _THEME_ALIASES,
)
from app.enums import (  # noqa: E402
    FEED_TYPE_OPTIONS,
    LANGUAGE_OPTIONS,
    NEGATIVE_CATEGORY_OPTIONS,
    OUTLET_TYPE_OPTIONS,
    PRIORITY_OPTIONS,
    _NEGATIVE_CATEGORY_ALIASES,
    _OUTLET_TYPE_ALIASES,
    _PRIORITY_ALIASES,
)


TABLE_KEYS: Tuple[str, ...] = ("keywords", "feeds", "outlets", "negative_keywords")
GLOBAL_MARKET_SENTINELS = {"All", "Alle"}
VALID_MARKET_SET = set(VALID_MARKETS) | GLOBAL_MARKET_SENTINELS


@dataclass
class Finding:
    file: str
    table: str
    row_index: int
    severity: str  # "error" | "warn"
    message: str

    def format(self) -> str:
        return f"{self.file}:{self.table}[{self.row_index}]:{self.severity}: {self.message}"


@dataclass
class Report:
    findings: List[Finding] = field(default_factory=list)
    row_counts: Dict[str, Dict[str, int]] = field(default_factory=dict)

    @property
    def errors(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "error"]

    @property
    def warnings(self) -> List[Finding]:
        return [f for f in self.findings if f.severity == "warn"]

    def ok(self) -> bool:
        return not self.errors


# ---------------------------------------------------------------------------
# Validation core (offline)
# ---------------------------------------------------------------------------

_REQUIRED_FIELDS: Dict[str, Tuple[str, ...]] = {
    "keywords": ("suchbegriff", "sprache"),
    "feeds": ("rss_feed",),
    "outlets": ("outlet",),
    "negative_keywords": ("begriff",),
}


def _check_markets(file: str, table: str, i: int, markets: Any, report: Report) -> None:
    if markets is None or markets == []:
        return  # empty means "global"; accepted
    if not isinstance(markets, list):
        report.findings.append(Finding(file, table, i, "error", f"markt must be a list, got {type(markets).__name__}"))
        return
    for m in markets:
        if m not in VALID_MARKET_SET:
            report.findings.append(Finding(file, table, i, "error", f"markt {m!r} not in VALID_MARKETS ∪ {{All}}"))


def _check_themes(file: str, table: str, i: int, themes: Any, report: Report) -> None:
    if themes is None or themes == []:
        return
    if not isinstance(themes, list):
        report.findings.append(Finding(file, table, i, "error", f"themenbereich must be a list, got {type(themes).__name__}"))
        return
    known = set(VALID_THEMES) | set(_THEME_ALIASES.keys())
    for t in themes:
        if t not in VALID_THEMES:
            if t in _THEME_ALIASES:
                report.findings.append(Finding(file, table, i, "warn", f"themenbereich uses German alias {t!r} (canonical: {_THEME_ALIASES[t]!r})"))
            elif t not in known:
                report.findings.append(Finding(file, table, i, "error", f"themenbereich {t!r} not in VALID_THEMES"))


def _check_priority(file: str, table: str, i: int, value: Any, report: Report) -> None:
    if not value:
        return
    if value in PRIORITY_OPTIONS:
        return
    if value.strip().lower() in _PRIORITY_ALIASES:
        canonical = _PRIORITY_ALIASES[value.strip().lower()]
        report.findings.append(Finding(file, table, i, "warn", f"prioritaet uses alias {value!r} (canonical: {canonical!r})"))
    else:
        report.findings.append(Finding(file, table, i, "error", f"prioritaet {value!r} not in PRIORITY_OPTIONS"))


def _check_language(file: str, table: str, i: int, value: Any, required: bool, report: Report) -> None:
    if not value:
        if required:
            report.findings.append(Finding(file, table, i, "error", "sprache is required"))
        return
    if value not in LANGUAGE_OPTIONS:
        report.findings.append(Finding(file, table, i, "error", f"sprache {value!r} not in LANGUAGE_OPTIONS"))


def _check_combo_keyword(file: str, table: str, i: int, value: str, report: Report) -> None:
    """Every ``+``-separated segment of a keyword must have >=1 significant
    token (>=4 characters). Hyphens/quotes count as intra-token joiners, so
    ``E-Bus`` is a single 5-char token."""
    if "+" not in value:
        return
    segments = [s.strip() for s in value.split("+")]
    for seg in segments:
        if not seg:
            report.findings.append(Finding(file, table, i, "error", f"suchbegriff {value!r} has empty +-segment"))
            continue
        # Treat hyphen/underscore/apostrophe as intra-token; split on whitespace only.
        tokens = [t for t in seg.split() if len(t.strip('"')) >= 4]
        if not tokens:
            report.findings.append(Finding(file, table, i, "error", f"suchbegriff {value!r} segment {seg!r} has no significant (>=4 char) token"))


def _check_row_required(file: str, table: str, i: int, row: dict, report: Report) -> bool:
    """Verify presence of required scalar fields; returns False if missing."""
    ok = True
    for field_name in _REQUIRED_FIELDS[table]:
        val = row.get(field_name)
        if val is None or (isinstance(val, str) and not val.strip()):
            report.findings.append(Finding(file, table, i, "error", f"required field {field_name!r} is missing/empty"))
            ok = False
    return ok


def _validate_row(file: str, table: str, i: int, row: dict, report: Report) -> None:
    if not isinstance(row, dict):
        report.findings.append(Finding(file, table, i, "error", f"row must be an object, got {type(row).__name__}"))
        return

    _check_row_required(file, table, i, row, report)
    _check_markets(file, table, i, row.get("markt"), report)

    if table in ("keywords", "feeds", "outlets"):
        _check_themes(file, table, i, row.get("themenbereich"), report)
        _check_priority(file, table, i, row.get("prioritaet"), report)
        _check_language(file, table, i, row.get("sprache"), required=(table == "keywords"), report=report)

    if table == "keywords":
        _check_combo_keyword(file, table, i, row.get("suchbegriff") or "", report)

    if table == "feeds":
        typ = row.get("typ")
        if typ and typ not in FEED_TYPE_OPTIONS:
            report.findings.append(Finding(file, table, i, "error", f"typ {typ!r} not in FEED_TYPE_OPTIONS"))

    if table == "outlets":
        ot = row.get("outlet_typ")
        if ot:
            if ot not in OUTLET_TYPE_OPTIONS:
                if ot.strip().lower() in _OUTLET_TYPE_ALIASES:
                    canonical = _OUTLET_TYPE_ALIASES[ot.strip().lower()]
                    report.findings.append(Finding(file, table, i, "warn", f"outlet_typ uses alias {ot!r} (canonical: {canonical!r})"))
                else:
                    report.findings.append(Finding(file, table, i, "error", f"outlet_typ {ot!r} not in OUTLET_TYPE_OPTIONS"))

    if table == "negative_keywords":
        cat = row.get("kategorie")
        if cat:
            if cat not in NEGATIVE_CATEGORY_OPTIONS:
                if cat.strip().lower() in _NEGATIVE_CATEGORY_ALIASES:
                    canonical = _NEGATIVE_CATEGORY_ALIASES[cat.strip().lower()]
                    report.findings.append(Finding(file, table, i, "warn", f"kategorie uses alias {cat!r} (canonical: {canonical!r})"))
                else:
                    report.findings.append(Finding(file, table, i, "error", f"kategorie {cat!r} not in NEGATIVE_CATEGORY_OPTIONS"))


def validate_payload(file: str, payload: Any, report: Optional[Report] = None) -> Report:
    """Validate a parsed seed payload (dict). Missing arrays warn but don't fail."""
    if report is None:
        report = Report()

    if not isinstance(payload, dict):
        report.findings.append(Finding(file, "<root>", 0, "error", f"top-level JSON must be an object, got {type(payload).__name__}"))
        return report

    counts: Dict[str, int] = {}
    for table in TABLE_KEYS:
        rows = payload.get(table)
        if rows is None:
            report.findings.append(Finding(file, table, 0, "warn", "array missing (treated as empty)"))
            counts[table] = 0
            continue
        if not isinstance(rows, list):
            report.findings.append(Finding(file, table, 0, "error", f"{table} must be an array, got {type(rows).__name__}"))
            counts[table] = 0
            continue
        counts[table] = len(rows)
        for i, row in enumerate(rows):
            _validate_row(file, table, i, row, report)

    report.row_counts[file] = counts
    return report


def validate_file(path: Path, report: Optional[Report] = None) -> Report:
    if report is None:
        report = Report()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        report.findings.append(Finding(str(path), "<file>", 0, "error", f"invalid JSON: {exc}"))
        return report
    return validate_payload(str(path), payload, report)


# ---------------------------------------------------------------------------
# --network extension
# ---------------------------------------------------------------------------

def check_feeds_network(path: Path, timeout: float = 10.0) -> List[Tuple[str, bool, str]]:
    """Fetch every RSS-Feed-typed row and confirm HTTP 200 + >0 entries.

    Returns a list of ``(url, ok, message)`` tuples. Manual-only — not called
    from the offline validator.
    """
    try:
        import feedparser  # type: ignore
        import requests  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(f"--network requires 'feedparser' and 'requests': {exc}")

    results: List[Tuple[str, bool, str]] = []
    payload = json.loads(path.read_text(encoding="utf-8"))
    for row in payload.get("feeds") or []:
        if not isinstance(row, dict):
            continue
        if row.get("typ") != "RSS-Feed":
            continue
        url = (row.get("rss_feed") or "").strip()
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=timeout, headers={"User-Agent": "FTNewsMonitor-validator/1"})
            if resp.status_code != 200:
                results.append((url, False, f"HTTP {resp.status_code}"))
                continue
            parsed = feedparser.parse(resp.content)
            n = len(parsed.entries or [])
            if n <= 0:
                results.append((url, False, "0 entries"))
            else:
                results.append((url, True, f"{n} entries"))
        except Exception as exc:
            results.append((url, False, f"error: {exc}"))
    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_summary(report: Report) -> None:
    print("\n=== Seed validator ===")
    if report.row_counts:
        print("\nRow counts:")
        header = f"{'file':<40} {'keywords':>10} {'feeds':>8} {'outlets':>9} {'neg-kw':>8}"
        print(header)
        print("-" * len(header))
        for f, c in report.row_counts.items():
            print(f"{Path(f).name:<40} {c.get('keywords', 0):>10} {c.get('feeds', 0):>8} {c.get('outlets', 0):>9} {c.get('negative_keywords', 0):>8}")

    if report.warnings:
        print(f"\n{len(report.warnings)} warning(s):")
        for w in report.warnings:
            print(f"  {w.format()}")

    if report.errors:
        print(f"\n{len(report.errors)} error(s):")
        for e in report.errors:
            print(f"  {e.format()}")
        print("\nFAIL")
    else:
        print("\nOK")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate seed pack JSON files.")
    parser.add_argument("paths", nargs="*", type=Path, help="Seed files (default: seeds/*.json)")
    parser.add_argument("--network", action="store_true", help="Additionally fetch each RSS-Feed URL")
    args = parser.parse_args()

    if args.paths:
        paths = args.paths
    else:
        seeds_dir = ROOT / "seeds"
        # Skip files starting with "_" (holding-pen files like
        # _pending_research.json — not shipped, not imported at runtime).
        paths = sorted(p for p in seeds_dir.glob("*.json") if not p.name.startswith("_"))
        if not paths:
            print(f"No seed files found in {seeds_dir}/", file=sys.stderr)
            return 1

    report = Report()
    for p in paths:
        if not p.exists():
            report.findings.append(Finding(str(p), "<file>", 0, "error", "file not found"))
            continue
        validate_file(p, report)

    _print_summary(report)

    if args.network:
        print("\n--network: fetching RSS-Feed URLs …")
        for p in paths:
            if not p.exists():
                continue
            print(f"\n{p}:")
            for url, ok, msg in check_feeds_network(p):
                marker = "OK  " if ok else "FAIL"
                print(f"  {marker}  {url}  ({msg})")

    return 0 if report.ok() else 1


if __name__ == "__main__":
    raise SystemExit(main())

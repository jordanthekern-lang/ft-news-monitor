"""Central configuration loader, sourced from .env."""

from __future__ import annotations

try:  # corporate proxies re-sign TLS; trust the Windows cert store
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_data_dir() -> Path:
    """Where the DB, .env, and log files live.

    Resolution order (first match wins):

    1. ``FM_DATA_DIR`` environment variable — explicit override. Used by the
       test suite (conftest points it at a tmp dir) and by advanced users who
       want to relocate the state.
    2. Packaged deployment — detected by the sibling ``python/python.exe``
       (the embedded interpreter layout ``build_release.py`` produces). In
       this mode the ZIP extract is treated as read-only and state lives in
       ``%LOCALAPPDATA%\\FTNewsMonitor``. This keeps the DB alive across a
       re-extract (colleague update flow).
    3. Dev default — ``PROJECT_ROOT/data``. Same as before packaging.
    """
    override = os.getenv("FM_DATA_DIR")
    if override:
        return Path(override)
    if (PROJECT_ROOT / "python" / "python.exe").exists():
        appdata = os.getenv("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local")
        return Path(appdata) / "FTNewsMonitor"
    return PROJECT_ROOT / "data"


DATA_DIR = _resolve_data_dir()
DB_PATH = DATA_DIR / "monitor.db"
LOG_PATH = DATA_DIR / "collection.log"
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def env_path() -> Path:
    """Single source of truth for the .env file location.

    - Dev default (no ``FM_DATA_DIR``, no embedded interpreter): repo-root
      ``.env`` — preserves the pre-packaging developer workflow where the
      wizard-written secrets live next to ``requirements.txt``.
    - Packaged / ``FM_DATA_DIR`` override: ``DATA_DIR / .env`` — the ZIP
      extract is treated as read-only, secrets sit alongside the DB in
      ``%LOCALAPPDATA%\\FTNewsMonitor`` and survive a re-extract.

    The setup wizard writes through :func:`write_env_values`, which calls
    this helper — a single edit here controls both read and write paths.

    Resolves ``DATA_DIR`` live via :func:`_resolve_data_dir` so callers who
    change ``FM_DATA_DIR`` after import (e.g. test monkeypatches, re-entrant
    callers) always see the current mapping. The cost is a couple of
    ``Path.exists`` checks — sub-microsecond and only hit a few times per
    wizard interaction.
    """
    if os.getenv("FM_DATA_DIR"):
        return _resolve_data_dir() / ".env"
    if (PROJECT_ROOT / "python" / "python.exe").exists():
        return _resolve_data_dir() / ".env"
    return PROJECT_ROOT / ".env"


def _load_env() -> None:
    # `utf-8-sig` strips a leading BOM if present — Windows PowerShell's
    # `Set-Content -Encoding UTF8` writes one by default, which would otherwise
    # corrupt the first variable name (e.g. "﻿ANTHROPIC_API_KEY").
    env_file = env_path()
    if env_file.exists():
        load_dotenv(env_file, encoding="utf-8-sig")
    else:
        load_dotenv(encoding="utf-8-sig")


_load_env()


def reload_env() -> None:
    """Re-read the .env file into ``os.environ`` (override wins).

    ``get_settings()`` reads ``os.getenv`` on every call — no cache to
    invalidate — so this is all that's needed after the setup wizard patches
    the file via :func:`write_env_values`.
    """
    env_file = env_path()
    if env_file.exists():
        load_dotenv(env_file, encoding="utf-8-sig", override=True)


def _quote_env_value(value: str) -> str:
    """Quote only when needed. ``#`` starts a comment; spaces need quoting."""
    if value == "":
        return ""
    if any(ch in value for ch in (" ", "\t", "#", '"', "'", "\n", "=")):
        # Prefer double quotes; escape any embedded double quotes and backslashes.
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def write_env_values(values: dict) -> None:
    """Patch-write ``.env``: replace existing ``KEY=`` lines in place,
    append new keys at the end, and never touch unrelated lines or comments.

    Empty-string values are still written (the wizard filters those upstream);
    callers that mean "leave unset" must drop the key before calling.

    The function must NEVER log the values it writes — secrets pass through
    here.
    """
    if not isinstance(values, dict):
        raise TypeError("write_env_values expects a dict of str -> str")

    path = env_path()
    if path.exists():
        # utf-8-sig strips a BOM on read; we always write plain utf-8.
        text = path.read_text(encoding="utf-8-sig")
    else:
        text = ""

    # Split preserving trailing newline state so we can round-trip cleanly.
    had_trailing_newline = text.endswith("\n") if text else True
    lines = text.splitlines()

    remaining: dict = dict(values)
    out_lines: list = []
    for line in lines:
        stripped = line.lstrip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            out_lines.append(line)
            continue
        key = stripped.split("=", 1)[0].strip()
        if key in remaining:
            new_value = remaining.pop(key)
            out_lines.append(f"{key}={_quote_env_value(str(new_value))}")
        else:
            out_lines.append(line)

    # Append any keys that weren't already in the file.
    for key, value in remaining.items():
        out_lines.append(f"{key}={_quote_env_value(str(value))}")

    new_text = "\n".join(out_lines)
    if out_lines and (had_trailing_newline or remaining):
        new_text += "\n"

    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp-file in the same directory + os.replace so a crash
    # mid-write can never leave a partially-written .env on disk. Secrets
    # transit this function, so a truncated file would be worse than a stale
    # one. Same directory is required for os.replace to be atomic on Windows.
    fd, tmp = tempfile.mkstemp(prefix=".env.", dir=str(path.parent))
    try:
        # No explicit `newline` — matches the previous ``path.write_text``
        # behaviour (text mode, platform-default line separator on write).
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(new_text)
        # POSIX best-effort: tighten permissions before the atomic swap.
        # Windows ignores this — file permissions there come from ACLs.
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # AI provider: "bedrock" (default), "anthropic" (direct API key), or
    # "cowork" (scoring is delegated to a Claude Cowork session via the
    # /api/articles/{pending,scores} endpoints — the tool itself never
    # opens an Anthropic client in this mode).
    ai_provider: str

    # Auth: either set static keys (legacy) OR rely on a named profile from
    # ~/.aws/credentials (Azure-AD-SSO style). Profile takes precedence.
    aws_profile: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str
    aws_region: str
    anthropic_model: str

    # Direct Anthropic API key (used when ai_provider="anthropic")
    anthropic_api_key: str

    # Shared token that gates the cowork ranking endpoints. Empty string
    # disables the gate — only sensible in dev/tests. install.bat generates
    # a random hex string on first cowork-mode install.
    cowork_api_token: str

    # AI generation settings
    ai_max_tokens: int

    dashboard_port: int
    timezone: str
    max_age_days: int
    highlight_days: int
    ai_filter_max_parallel: int
    ai_skip_prescreen_days: int


def get_settings() -> Settings:
    return Settings(
        ai_provider=os.getenv("AI_PROVIDER", "bedrock"),
        aws_profile=os.getenv("AWS_PROFILE", ""),
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", ""),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", ""),
        aws_session_token=os.getenv("AWS_SESSION_TOKEN", ""),
        aws_region=os.getenv("AWS_REGION", "eu-west-1"),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "eu.anthropic.claude-opus-4-7"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        cowork_api_token=os.getenv("COWORK_API_TOKEN", ""),
        ai_max_tokens=_int("AI_MAX_TOKENS", 512),
        dashboard_port=_int("DASHBOARD_PORT", 8766),
        timezone=os.getenv("TIMEZONE", "Europe/Berlin"),
        max_age_days=_int("MAX_AGE_DAYS", 180),
        highlight_days=_int("HIGHLIGHT_DAYS", 30),
        ai_filter_max_parallel=_int("AI_FILTER_MAX_PARALLEL", 5),
        ai_skip_prescreen_days=_int("AI_SKIP_PRESCREEN_DAYS", 7),
    )


# Sprache → hl/gl/ceid mapping (Google News)
LANG_TO_GOOGLE_NEWS = {
    "DE": ("de", "DE", "DE:de"),
    "EN": ("en", "GB", "GB:en"),
    "NL": ("nl", "NL", "NL:nl"),
    "FR": ("fr", "FR", "FR:fr"),
    "IT": ("it", "IT", "IT:it"),
    "PT": ("pt", "PT", "PT:pt"),
    "SV": ("sv", "SE", "SE:sv"),
    "NO": ("no", "NO", "NO:no"),
    "DA": ("da", "DK", "DK:da"),
    "FI": ("fi", "FI", "FI:fi"),
}

# Sprache → setlang/cc mapping (Bing News). Bing's index is geo-segmented
# more strictly than Google's, which makes it the better second source for
# non-DACH coverage when the crawler runs from a German IP.
LANG_TO_BING_NEWS = {
    "DE": ("de", "DE"),
    "EN": ("en", "GB"),
    "NL": ("nl", "NL"),
    "FR": ("fr", "FR"),
    "IT": ("it", "IT"),
    "PT": ("pt", "PT"),
    "SV": ("sv", "SE"),
    "NO": ("no", "NO"),
    "DA": ("da", "DK"),
    "FI": ("fi", "FI"),
}

# Markt → primäre Sprache(n) (Fallback wenn Sprache fehlt)
MARKET_TO_LANGS = {
    "DACH": ["DE"],
    "Benelux": ["NL", "FR"],
    "France": ["FR"],
    "Italy": ["IT"],
    "Portugal": ["PT"],
    "Nordics": ["SV", "NO", "DA", "FI"],
    "EU": ["EN"],
}

VALID_MARKETS = ["DACH", "Benelux", "France", "Italy", "Nordics", "Portugal", "EU"]
VALID_THEMES = [
    "E-buses",
    "Hydrogen buses",
    "Charging infrastructure",
    "Hydrogen infrastructure",
    "Bus fleets",
    "Drivetrain technology",
    "Energy supply",
    "Sustainability",
    "Tenders",
    "Fleet strategy",
    "Alternative fuels (HVO/biofuels)",
]
VALID_CATEGORIES = [
    "Vehicle funding",
    "Infrastructure funding",
    "Policy & regulation",
    "Tenders & procurement",
    "Market & technology",
    "Other",
]

# Map any legacy German theme/category to the canonical English one. Keeps
# historical article rows (which were tagged in German by Claude before the
# prompt switch) consistent with the new UI options.
_THEME_ALIASES = {
    "E-Busse": "E-buses",
    "Wasserstoffbusse": "Hydrogen buses",
    "Ladeinfrastruktur": "Charging infrastructure",
    "Wasserstoffinfrastruktur": "Hydrogen infrastructure",
    "Busflotten": "Bus fleets",
    "Antriebstechnologie": "Drivetrain technology",
    "Energieversorgung": "Energy supply",
    "Nachhaltigkeit": "Sustainability",
}

_CATEGORY_ALIASES = {
    "Fahrzeugförderung": "Vehicle funding",
    "Infrastrukturförderung": "Infrastructure funding",
    # Pre-Task-2 category "Both" → closest match is now "Vehicle funding"
    # (covers combined vehicle+infra funding articles); logged for awareness.
    "Beides": "Vehicle funding",
    "Both": "Vehicle funding",
    # Pre-Task-2 "Policy development" → new canonical form
    "Policy development": "Policy & regulation",
    "Politische Entwicklung": "Policy & regulation",
    "Sonstiges": "Other",
}


def normalise_theme(value):
    if not value:
        return value
    return _THEME_ALIASES.get(value, value)


def normalise_category(value):
    if not value:
        return value
    return _CATEGORY_ALIASES.get(value, value)

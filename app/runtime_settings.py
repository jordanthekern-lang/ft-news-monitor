"""Runtime user settings, persisted as JSON in the ``app_settings`` table.

Storage split (contract): secrets and machine configuration (``AWS_*``, model,
port, …) stay in ``.env`` — ONLY user behaviour lives here. Never store
credentials in the database.

Known keys (the setup wizard of a later task builds on these exact names):

======================================  =========================================
Key                                     Value
======================================  =========================================
``home_markets``                        list[str] — installation's markets;
                                        unset or ``[]`` = all markets
``min_score_default``                   int — dashboard default minimum score
``ai_skip_prescreen_days_override``     int | None — None = use env default
``scheduler_enabled``                   bool
``scheduler_time``                      str "HH:MM"
``last_run_at``                         str, ISO-8601 (set by app.pipeline)
``last_non_dry_run_at``                 str, ISO-8601 — only stamped when
                                        ``dry_run=False`` (used by the
                                        scheduler catch-up logic)
``last_run_summary``                    dict (set by app.pipeline)
``setup_completed``                     bool
======================================  =========================================
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import database

# --- Known keys (module constants, part of the Task-3 contract) -------------
HOME_MARKETS = "home_markets"
MIN_SCORE_DEFAULT = "min_score_default"
AI_SKIP_PRESCREEN_DAYS_OVERRIDE = "ai_skip_prescreen_days_override"
SCHEDULER_ENABLED = "scheduler_enabled"
SCHEDULER_TIME = "scheduler_time"
LAST_RUN_AT = "last_run_at"
LAST_NON_DRY_RUN_AT = "last_non_dry_run_at"
LAST_RUN_SUMMARY = "last_run_summary"
SETUP_COMPLETED = "setup_completed"
# v0.3.1 — user-editable feature flags that live in the DB instead of .env
# so they can be toggled from the Settings UI without editing files.
SUGGEST_KEYWORDS_ENABLED = "suggest_keywords_enabled"
TRANSLATION_TARGET_LANGS = "translation_target_langs"
# v0.4.0 — tracks the highest ``seed_version`` we've already merged per
# market slug (e.g. {"dach": 2, "eu": 2}). The auto-reseeder on startup
# reads this to decide whether a shipped seed file needs a re-merge.
IMPORTED_SEED_VERSIONS = "imported_seed_versions"
# v0.5.0 legacy — deprecated in v0.5.1. The pipeline no longer reads this flag
# because the adaptive time-window logic keys off ``last_non_dry_run_at``
# instead. The constant is kept for backward compatibility with tests and
# existing DB rows (T11 additive-only invariant); it will always be ignored.
INITIAL_BACKFILL_COMPLETED = "initial_backfill_completed"


def get(key: str, default: Any = None, *, db_path: Optional[Path] = None) -> Any:
    """Return the JSON-decoded value for ``key``, or ``default`` when unset.

    An explicitly stored ``None`` (JSON ``null``) is returned as ``None`` —
    only a *missing* row yields ``default``.
    """
    with database.get_conn(db_path or database.DB_PATH) as conn:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = ?", (key,)
        ).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def set(key: str, value: Any, *, db_path: Optional[Path] = None) -> None:
    """Persist ``value`` (any JSON-serialisable object) under ``key``."""
    encoded = json.dumps(value, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()
    with database.get_conn(db_path or database.DB_PATH) as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, encoded, now),
        )


def effective_home_markets(*, db_path: Optional[Path] = None) -> Optional[list]:
    """``home_markets`` with its documented semantics applied.

    Returns a non-empty list of market names, or ``None`` when the setting is
    unset or empty — meaning "all markets" (backward compatible before the
    setup wizard exists).
    """
    value = get(HOME_MARKETS, db_path=db_path)
    if not isinstance(value, list):
        return None
    markets = [m for m in value if isinstance(m, str) and m]
    return markets or None

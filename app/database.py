"""SQLite persistence layer.

All timestamps are stored as ISO-8601 UTC strings. Display formatting happens
in the dashboard layer.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional

from .config import DB_PATH, DATA_DIR, _THEME_ALIASES

logger = logging.getLogger(__name__)


TABLES = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    source_name TEXT,
    source_type TEXT,
    country TEXT,
    article_domain TEXT,
    published_at TEXT,
    collected_at TEXT NOT NULL,
    snippet TEXT,
    raw_content TEXT,
    category TEXT,
    relevance_score INTEGER,
    trust_bonus INTEGER DEFAULT 0,
    feedback_bonus INTEGER DEFAULT 0,
    relevance_reasoning TEXT,
    themes TEXT,
    is_filtered_out INTEGER DEFAULT 0,
    filter_reason TEXT,
    ai_filtered INTEGER DEFAULT 0,
    starred INTEGER DEFAULT 0,
    read INTEGER DEFAULT 0,
    ignored INTEGER DEFAULT 0,
    language TEXT,
    title_en TEXT,
    snippet_en TEXT,
    translation_attempted INTEGER DEFAULT 0,
    outlook_entry_id TEXT,
    outlook_store_id TEXT
);

CREATE TABLE IF NOT EXISTS config_cache (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Runtime user settings (JSON-encoded values), managed via app/runtime_settings.py.
-- Secrets / machine config stay in .env — ONLY user behaviour lives here.
CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS outlet_domains (
    domain TEXT PRIMARY KEY,
    outlet_name TEXT NOT NULL,
    priority TEXT,
    market TEXT,
    outlet_type TEXT
);

CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT PRIMARY KEY,
    source_name TEXT,
    source_type TEXT,
    last_run TEXT,
    last_success TEXT,
    last_error TEXT,
    consecutive_failures INTEGER DEFAULT 0,
    items_collected INTEGER DEFAULT 0
);

-- Market-Edition: configuration tables, replacing the Notion databases.
-- Multi-value fields (markt, themenbereich) are stored as JSON arrays.
CREATE TABLE IF NOT EXISTS keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    suchbegriff TEXT NOT NULL,
    markt TEXT NOT NULL DEFAULT '[]',
    themenbereich TEXT NOT NULL DEFAULT '[]',
    prioritaet TEXT,
    sprache TEXT,
    notizen TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feeds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rss_feed TEXT NOT NULL,
    typ TEXT,
    markt TEXT NOT NULL DEFAULT '[]',
    themenbereich TEXT NOT NULL DEFAULT '[]',
    prioritaet TEXT,
    sprache TEXT,
    notizen TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outlets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    outlet TEXT NOT NULL,
    url TEXT,
    markt TEXT NOT NULL DEFAULT '[]',
    themenbereich TEXT NOT NULL DEFAULT '[]',
    prioritaet TEXT,
    sprache TEXT,
    outlet_typ TEXT,
    hat_rss INTEGER NOT NULL DEFAULT 0,
    notizen TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS negative_keywords (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    begriff TEXT NOT NULL,
    markt TEXT NOT NULL DEFAULT '[]',
    kategorie TEXT,
    notizen TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- v0.2.0 Feature 1: Golden-Set Backtesting.
-- Personal per-install labels for eval. Separate from starred/ignored so
-- the eval-set isn't contaminated by prompt few-shot leakage.
CREATE TABLE IF NOT EXISTS golden_labels (
    article_id INTEGER PRIMARY KEY,
    label      TEXT NOT NULL CHECK (label IN ('relevant', 'noise')),
    added_at   TEXT NOT NULL,
    notes      TEXT,
    FOREIGN KEY (article_id) REFERENCES articles(id) ON DELETE CASCADE
);

-- v0.2.0 Feature 2: Claude keyword suggestions.
-- Rows land in status='pending' from the pipeline; the admin UI moves them
-- to approved (writes a keywords row) or dismissed.
CREATE TABLE IF NOT EXISTS keyword_suggestions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    phrase              TEXT NOT NULL,
    phrase_normalized   TEXT NOT NULL,
    source_article_id   INTEGER,
    market              TEXT,
    sprache             TEXT,
    suggested_at        TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
                            CHECK (status IN ('pending','approved','dismissed')),
    approved_keyword_id INTEGER,
    FOREIGN KEY (source_article_id) REFERENCES articles(id) ON DELETE SET NULL,
    FOREIGN KEY (approved_keyword_id) REFERENCES keywords(id) ON DELETE SET NULL
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_articles_score ON articles(relevance_score DESC);
CREATE INDEX IF NOT EXISTS idx_articles_published ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_collected ON articles(collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_filtered ON articles(is_filtered_out, ai_filtered);
CREATE INDEX IF NOT EXISTS idx_articles_domain ON articles(article_domain);
CREATE INDEX IF NOT EXISTS idx_keywords_term ON keywords(suchbegriff);
CREATE INDEX IF NOT EXISTS idx_feeds_url ON feeds(rss_feed);
CREATE INDEX IF NOT EXISTS idx_outlets_name ON outlets(outlet);
CREATE INDEX IF NOT EXISTS idx_negative_term ON negative_keywords(begriff);
CREATE INDEX IF NOT EXISTS idx_golden_label ON golden_labels(label);
CREATE INDEX IF NOT EXISTS idx_suggestion_status ON keyword_suggestions(status);
CREATE INDEX IF NOT EXISTS idx_suggestion_norm ON keyword_suggestions(phrase_normalized);
"""


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the database to WAL journal mode (better concurrent read/write
    behaviour for dashboard + pipeline). Guarded: some filesystems (e.g.
    network shares) don't support WAL — fall back silently to the default."""
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
    except sqlite3.OperationalError:  # pragma: no cover - env dependent
        pass


def init_db(db_path: Path = DB_PATH) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        _enable_wal(conn)
        # 1) Tables first
        conn.executescript(TABLES)
        # 2) Idempotent column migrations (must run before any index that
        #    references the column, otherwise the index creation aborts on
        #    pre-existing DBs).
        for column, definition in (
            ("article_domain", "TEXT"),
            ("feedback_bonus", "INTEGER DEFAULT 0"),
            ("language", "TEXT"),
            ("title_en", "TEXT"),
            ("snippet_en", "TEXT"),
            ("translation_attempted", "INTEGER DEFAULT 0"),
            ("translation_attempts", "INTEGER DEFAULT 0"),
            # v0.3.2 — Outlook COM back-reference so the dashboard can
            # re-open the original mail. Both nullable; only set for
            # source_type='newsletter' rows ingested from v0.3.2 onwards.
            ("outlook_entry_id", "TEXT"),
            ("outlook_store_id", "TEXT"),
        ):
            try:
                conn.execute(f"ALTER TABLE articles ADD COLUMN {column} {definition}")
            except sqlite3.OperationalError:
                pass  # column already exists
        # 3) Indexes
        conn.executescript(INDEXES)
        conn.commit()


@contextmanager
def get_conn(db_path: Path = DB_PATH) -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    _enable_wal(conn)
    conn.execute("PRAGMA foreign_keys = ON;")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Article persistence
# ---------------------------------------------------------------------------

def insert_article(
    *,
    url: str,
    title: str,
    source_name: str,
    source_type: str,
    country: Optional[str],
    published_at: Optional[datetime],
    snippet: Optional[str],
    raw_content: Optional[str] = None,
    language: Optional[str] = None,
    outlook_entry_id: Optional[str] = None,
    outlook_store_id: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> Optional[int]:
    """Insert article. Returns row id, or None if URL already existed.

    ``language`` is the *source* language as known to the collector (from the
    feed/outlet config in Notion); the translator stage uses it as the most
    reliable signal before falling back to langdetect.

    ``outlook_entry_id`` / ``outlook_store_id`` (v0.3.2) are the COM
    back-reference for ``source_type='newsletter'`` rows so the dashboard
    can re-open the mail in Outlook. Both nullable; older newsletter rows
    predating v0.3.2 leave them NULL.
    """
    # Local import to avoid a hard cycle (domain_utils imports `database`).
    from . import domain_utils

    domain = domain_utils.extract_domain(url)
    with get_conn(db_path) as conn:
        try:
            cur = conn.execute(
                """
                INSERT INTO articles (
                    url, title, source_name, source_type, country, article_domain,
                    published_at, collected_at, snippet, raw_content, language,
                    outlook_entry_id, outlook_store_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    url,
                    title,
                    source_name,
                    source_type,
                    country,
                    domain,
                    published_at.isoformat() if published_at else None,
                    _utc_now_iso(),
                    snippet,
                    raw_content,
                    language.lower() if language else None,
                    outlook_entry_id,
                    outlook_store_id,
                ),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


def mark_filtered(article_id: int, reason: str, db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE articles SET is_filtered_out = 1, filter_reason = ? WHERE id = ?",
            (reason, article_id),
        )


def update_ai_score(
    article_id: int,
    *,
    relevance_score: int,
    trust_bonus: int,
    category: str,
    themes: List[str],
    reasoning: str,
    feedback_bonus: int = 0,
    db_path: Path = DB_PATH,
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE articles SET
                relevance_score = ?,
                trust_bonus = ?,
                feedback_bonus = ?,
                category = ?,
                themes = ?,
                relevance_reasoning = ?,
                ai_filtered = 1
            WHERE id = ?
            """,
            (
                relevance_score,
                trust_bonus,
                feedback_bonus,
                category,
                json.dumps(themes, ensure_ascii=False),
                reasoning,
                article_id,
            ),
        )


def fetch_articles_for_prefilter(db_path: Path = DB_PATH) -> List[sqlite3.Row]:
    with get_conn(db_path) as conn:
        return list(
            conn.execute(
                "SELECT * FROM articles WHERE is_filtered_out = 0 AND ai_filtered = 0"
            )
        )


def fetch_articles_for_ai(db_path: Path = DB_PATH) -> List[sqlite3.Row]:
    with get_conn(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM articles
                WHERE is_filtered_out = 0 AND ai_filtered = 0
                ORDER BY collected_at DESC
                """
            )
        )


def article_by_id(article_id: int, db_path: Path = DB_PATH) -> Optional[sqlite3.Row]:
    with get_conn(db_path) as conn:
        cur = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,))
        return cur.fetchone()


def url_exists(url: str, db_path: Path = DB_PATH) -> bool:
    with get_conn(db_path) as conn:
        cur = conn.execute("SELECT 1 FROM articles WHERE url = ?", (url,))
        return cur.fetchone() is not None


def list_excluded_articles(
    *,
    kind: str = "alle",
    limit: int = 500,
    db_path: Path = DB_PATH,
) -> List[sqlite3.Row]:
    """Articles that don't appear on the main dashboard.

    kind:
      'prefilter'  → is_filtered_out = 1 (too old / negative keyword)
      'prescreen'  → AI ran a no-op because no high-priority keyword was found
      'lowscore'   → AI scored, but final score < 4
      'pending'    → not yet pre-filtered or AI-scored (visible only briefly)
      'alle'       → union of all of the above
    """
    base = """
        SELECT *,
            CASE
                WHEN is_filtered_out = 1 THEN 'Pre-Filter'
                WHEN ai_filtered = 1 AND COALESCE(relevance_reasoning, '') LIKE 'Pre-screen%' THEN 'Pre-Screen'
                WHEN ai_filtered = 1 THEN 'Low score'
                ELSE 'Pending'
            END AS exclusion_reason
        FROM articles
    """
    if kind == "prefilter":
        where = "WHERE is_filtered_out = 1"
    elif kind == "prescreen":
        where = (
            "WHERE is_filtered_out = 0 AND ai_filtered = 1 "
            "AND COALESCE(relevance_reasoning, '') LIKE 'Pre-screen%'"
        )
    elif kind == "lowscore":
        where = (
            "WHERE is_filtered_out = 0 AND ai_filtered = 1 "
            "AND COALESCE(relevance_reasoning, '') NOT LIKE 'Pre-screen%' "
            "AND (COALESCE(relevance_score, 0) + COALESCE(trust_bonus, 0) + COALESCE(feedback_bonus, 0)) < 4"
        )
    elif kind == "pending":
        where = "WHERE is_filtered_out = 0 AND ai_filtered = 0"
    else:  # alle
        where = (
            "WHERE is_filtered_out = 1 "
            "OR (ai_filtered = 1 AND COALESCE(relevance_reasoning, '') LIKE 'Pre-screen%') "
            "OR (ai_filtered = 1 AND (COALESCE(relevance_score, 0) + COALESCE(trust_bonus, 0) + COALESCE(feedback_bonus, 0)) < 4)"
        )
    sql = f"{base} {where} ORDER BY collected_at DESC LIMIT ?"
    with get_conn(db_path) as conn:
        return list(conn.execute(sql, (limit,)))


def excluded_counts(db_path: Path = DB_PATH) -> Dict[str, int]:
    with get_conn(db_path) as conn:
        out: Dict[str, int] = {}
        out["prefilter"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE is_filtered_out = 1"
        ).fetchone()[0]
        out["prescreen"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE is_filtered_out = 0 "
            "AND ai_filtered = 1 AND COALESCE(relevance_reasoning, '') LIKE 'Pre-screen%'"
        ).fetchone()[0]
        out["lowscore"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE is_filtered_out = 0 "
            "AND ai_filtered = 1 "
            "AND COALESCE(relevance_reasoning, '') NOT LIKE 'Pre-screen%' "
            "AND (COALESCE(relevance_score, 0) + COALESCE(trust_bonus, 0) + COALESCE(feedback_bonus, 0)) < 4"
        ).fetchone()[0]
        out["pending"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE is_filtered_out = 0 AND ai_filtered = 0"
        ).fetchone()[0]
        return out


def list_articles(
    *,
    min_score: Optional[int] = None,
    markets: Optional[List[str]] = None,
    themes: Optional[List[str]] = None,
    days: Optional[int] = None,
    status: Optional[str] = None,  # "neu" | "markiert" | "alle"
    include_ignored: bool = False,
    limit: int = 200,
    db_path: Path = DB_PATH,
) -> List[sqlite3.Row]:
    sql = ["SELECT * FROM articles WHERE is_filtered_out = 0 AND ai_filtered = 1"]
    params: List[Any] = []

    if not include_ignored:
        sql.append("AND ignored = 0")

    if min_score is not None:
        sql.append("AND COALESCE(relevance_score, 0) + COALESCE(trust_bonus, 0) + COALESCE(feedback_bonus, 0) >= ?")
        params.append(min_score)

    if markets:
        placeholders = ",".join("?" * len(markets))
        sql.append(f"AND country IN ({placeholders})")
        params.extend(markets)

    if themes:
        # Match if any theme overlaps (themes column contains JSON list).
        # Expand each requested theme to also match its German alias so that
        # pre-migration rows (stored with German values) are found correctly.
        # Build reverse map: canonical English → list of German aliases
        _reverse_theme: dict[str, list[str]] = {}
        for german, english in _THEME_ALIASES.items():
            _reverse_theme.setdefault(english, []).append(german)

        like_terms: list[str] = []
        like_params: list[str] = []
        for t in themes:
            like_terms.append("themes LIKE ?")
            like_params.append(f'%"{t}"%')
            for alias in _reverse_theme.get(t, []):
                like_terms.append("themes LIKE ?")
                like_params.append(f'%"{alias}"%')
        sql.append(f"AND ({' OR '.join(like_terms)})")
        params.extend(like_params)

    if days is not None:
        cutoff = _days_ago_iso(days)
        sql.append("AND COALESCE(published_at, collected_at) >= ?")
        params.append(cutoff)

    if status in ("neu", "new"):
        sql.append("AND read = 0 AND ignored = 0")
    elif status in ("markiert", "starred"):
        sql.append("AND starred = 1")
    # "alle" / "all" → no additional filter

    sql.append("ORDER BY (COALESCE(relevance_score, 0) + COALESCE(trust_bonus, 0) + COALESCE(feedback_bonus, 0)) DESC, published_at DESC")
    sql.append("LIMIT ?")
    params.append(limit)

    with get_conn(db_path) as conn:
        return list(conn.execute(" ".join(sql), params))


def _days_ago_iso(days: int) -> str:
    from datetime import timedelta

    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


# ---------------------------------------------------------------------------
# Article actions
# ---------------------------------------------------------------------------

def toggle_flag(article_id: int, column: str, db_path: Path = DB_PATH) -> int:
    if column not in {"starred", "read", "ignored"}:
        raise ValueError(f"Invalid column: {column}")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            f"UPDATE articles SET {column} = 1 - {column} WHERE id = ? RETURNING {column}",
            (article_id,),
        )
        row = cur.fetchone()
        return int(row[0]) if row else 0


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------

def stats(db_path: Path = DB_PATH) -> Dict[str, int]:
    with get_conn(db_path) as conn:
        rows: Dict[str, int] = {}
        rows["total"] = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        rows["filtered_out"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE is_filtered_out = 1"
        ).fetchone()[0]
        rows["scored"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE ai_filtered = 1 AND is_filtered_out = 0"
        ).fetchone()[0]
        rows["high_score"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE COALESCE(relevance_score, 0) + COALESCE(trust_bonus, 0) + COALESCE(feedback_bonus, 0) >= 8"
        ).fetchone()[0]
        rows["new_today"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE collected_at >= ?",
            (_days_ago_iso(1),),
        ).fetchone()[0]
        rows["this_week"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE collected_at >= ?",
            (_days_ago_iso(7),),
        ).fetchone()[0]
        rows["starred"] = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE starred = 1"
        ).fetchone()[0]
        return rows


# ---------------------------------------------------------------------------
# Config cache
# ---------------------------------------------------------------------------

def cache_set(key: str, value: Any, db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO config_cache (key, value, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, default=str, ensure_ascii=False), _utc_now_iso()),
        )


def cache_get(key: str, db_path: Path = DB_PATH) -> Optional[Dict[str, Any]]:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "SELECT value, updated_at FROM config_cache WHERE key = ?", (key,)
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "value": json.loads(row["value"]),
            "updated_at": row["updated_at"],
        }


# ---------------------------------------------------------------------------
# Outlet domains
# ---------------------------------------------------------------------------

def replace_outlet_domains(rows: Iterable[Dict[str, Any]], db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM outlet_domains")
        conn.executemany(
            """
            INSERT OR REPLACE INTO outlet_domains (domain, outlet_name, priority, market, outlet_type)
            VALUES (:domain, :outlet_name, :priority, :market, :outlet_type)
            """,
            list(rows),
        )


def lookup_outlet_domain(domain: str, db_path: Path = DB_PATH) -> Optional[sqlite3.Row]:
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "SELECT * FROM outlet_domains WHERE domain = ?", (domain,)
        )
        return cur.fetchone()


# ---------------------------------------------------------------------------
# Source health
# ---------------------------------------------------------------------------

def record_source_run(
    *,
    source_id: str,
    source_name: str,
    source_type: str,
    success: bool,
    error: Optional[str],
    items_collected: int,
    db_path: Path = DB_PATH,
) -> None:
    now = _utc_now_iso()
    with get_conn(db_path) as conn:
        existing = conn.execute(
            "SELECT consecutive_failures FROM source_health WHERE source_id = ?",
            (source_id,),
        ).fetchone()
        prev_failures = existing["consecutive_failures"] if existing else 0
        new_failures = 0 if success else prev_failures + 1

        conn.execute(
            """
            INSERT INTO source_health (source_id, source_name, source_type, last_run,
                                       last_success, last_error, consecutive_failures,
                                       items_collected)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                source_name=excluded.source_name,
                source_type=excluded.source_type,
                last_run=excluded.last_run,
                last_success=CASE WHEN excluded.last_success IS NOT NULL THEN excluded.last_success ELSE source_health.last_success END,
                last_error=excluded.last_error,
                consecutive_failures=excluded.consecutive_failures,
                items_collected=source_health.items_collected + excluded.items_collected
            """,
            (
                source_id,
                source_name,
                source_type,
                now,
                now if success else None,
                error,
                new_failures,
                items_collected,
            ),
        )


def source_health_summary(db_path: Path = DB_PATH) -> Dict[str, int]:
    with get_conn(db_path) as conn:
        active = conn.execute(
            "SELECT COUNT(*) FROM source_health WHERE consecutive_failures = 0"
        ).fetchone()[0]
        failing = conn.execute(
            "SELECT COUNT(*) FROM source_health WHERE consecutive_failures > 0"
        ).fetchone()[0]
        return {"active": active, "failing": failing}


# ---------------------------------------------------------------------------
# Feedback (used by feedback.py for few-shot examples and domain bonus)
# ---------------------------------------------------------------------------

def fetch_feedback_examples(
    *, kind: str, limit: int, db_path: Path = DB_PATH
) -> List[sqlite3.Row]:
    """Return recently starred (kind='starred') or ignored (kind='ignored') rows."""
    column = "starred" if kind == "starred" else "ignored"
    with get_conn(db_path) as conn:
        return list(
            conn.execute(
                f"""
                SELECT * FROM articles
                WHERE {column} = 1 AND ai_filtered = 1
                ORDER BY collected_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def domain_feedback_counts(domain: str, db_path: Path = DB_PATH) -> tuple[int, int]:
    """Return (stars, ignores) for an article domain."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT
                COALESCE(SUM(CASE WHEN starred = 1 THEN 1 ELSE 0 END), 0) AS stars,
                COALESCE(SUM(CASE WHEN ignored = 1 THEN 1 ELSE 0 END), 0) AS ignores
            FROM articles
            WHERE article_domain = ?
            """,
            (domain,),
        ).fetchone()
        if not row:
            return 0, 0
        return int(row[0] or 0), int(row[1] or 0)


def list_domain_feedback(
    *, limit: int = 20, db_path: Path = DB_PATH
) -> List[Dict[str, Any]]:
    """Domains where the user has cast at least one star or ignore."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT
                article_domain AS domain,
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN starred = 1 THEN 1 ELSE 0 END), 0) AS stars,
                COALESCE(SUM(CASE WHEN ignored = 1 THEN 1 ELSE 0 END), 0) AS ignores
            FROM articles
            WHERE article_domain IS NOT NULL
              AND (starred = 1 OR ignored = 1)
            GROUP BY article_domain
            ORDER BY (stars + ignores) DESC, domain ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def fetch_articles_needing_translation(
    *, limit: int = 1000, db_path: Path = DB_PATH
) -> List[sqlite3.Row]:
    """Articles that have passed pre-filter but haven't been considered for
    translation yet."""
    with get_conn(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT * FROM articles
                WHERE is_filtered_out = 0
                  AND translation_attempted = 0
                ORDER BY collected_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        )


def mark_translation_attempted(
    article_id: int,
    *,
    language: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> None:
    """Mark the article as processed by the translator (no translation
    needed or translation failed). Idempotent."""
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE articles
            SET translation_attempted = 1,
                language = COALESCE(?, language)
            WHERE id = ?
            """,
            (language, article_id),
        )


def record_translation_failure(
    article_id: int,
    *,
    language: Optional[str],
    max_attempts: int,
    db_path: Path = DB_PATH,
) -> bool:
    """Bump the per-article retry counter on translation failure.

    Returns True once the cap is reached and the article has been marked as
    finally attempted (so the translator won't keep retrying forever).
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE articles
            SET translation_attempts = COALESCE(translation_attempts, 0) + 1,
                language = COALESCE(?, language)
            WHERE id = ?
            RETURNING translation_attempts
            """,
            (language, article_id),
        )
        row = cur.fetchone()
        attempts = int(row[0]) if row else max_attempts
        if attempts >= max_attempts:
            conn.execute(
                "UPDATE articles SET translation_attempted = 1 WHERE id = ?",
                (article_id,),
            )
            return True
        return False


def set_translation(
    article_id: int,
    *,
    language: str,
    title_en: Optional[str],
    snippet_en: Optional[str],
    db_path: Path = DB_PATH,
) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE articles
            SET translation_attempted = 1,
                language = ?,
                title_en = ?,
                snippet_en = ?
            WHERE id = ?
            """,
            (language, title_en, snippet_en, article_id),
        )


def translation_stats(db_path: Path = DB_PATH) -> Dict[str, int]:
    with get_conn(db_path) as conn:
        translated = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE title_en IS NOT NULL OR snippet_en IS NOT NULL"
        ).fetchone()[0]
        pending = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE is_filtered_out = 0 AND translation_attempted = 0"
        ).fetchone()[0]
        return {"translated": int(translated), "pending": int(pending)}


# ---------------------------------------------------------------------------
# Local config tables (Market-Edition replacement for Notion)
# ---------------------------------------------------------------------------

_CONFIG_LIST_COLUMNS = {
    "keywords": ("markt", "themenbereich"),
    "feeds": ("markt", "themenbereich"),
    "outlets": ("markt", "themenbereich"),
    "negative_keywords": ("markt",),
}


def _serialise_lists(table: str, row: Dict[str, Any]) -> Dict[str, Any]:
    """Convert any list-valued field into a JSON string for storage."""
    out = dict(row)
    for col in _CONFIG_LIST_COLUMNS.get(table, ()):
        value = out.get(col)
        if value is None:
            out[col] = "[]"
        elif isinstance(value, list):
            out[col] = json.dumps(value, ensure_ascii=False)
        # else: assume caller passed a JSON string already
    if table == "outlets":
        # Coerce hat_rss to 0/1
        out["hat_rss"] = 1 if out.get("hat_rss") else 0
    return out


def _deserialise_lists(table: str, row: sqlite3.Row) -> Dict[str, Any]:
    """Inverse of _serialise_lists for read paths."""
    out = dict(row)
    for col in _CONFIG_LIST_COLUMNS.get(table, ()):
        raw = out.get(col)
        if raw is None or raw == "":
            out[col] = []
        else:
            try:
                parsed = json.loads(raw)
                out[col] = parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, TypeError):
                out[col] = []
    if table == "outlets":
        out["hat_rss"] = bool(out.get("hat_rss"))
    return out


def list_config(table: str, db_path: Path = DB_PATH) -> List[Dict[str, Any]]:
    """Return all rows from a config table as decoded dicts."""
    if table not in _CONFIG_LIST_COLUMNS:
        raise ValueError(f"Unknown config table: {table}")
    with get_conn(db_path) as conn:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY id ASC").fetchall()
        return [_deserialise_lists(table, r) for r in rows]


def insert_config_row(table: str, data: Dict[str, Any], db_path: Path = DB_PATH) -> int:
    if table not in _CONFIG_LIST_COLUMNS:
        raise ValueError(f"Unknown config table: {table}")
    payload = _serialise_lists(table, data)
    payload["created_at"] = _utc_now_iso()
    payload["updated_at"] = payload["created_at"]
    cols = ", ".join(payload.keys())
    placeholders = ", ".join(f":{k}" for k in payload)
    with get_conn(db_path) as conn:
        cur = conn.execute(
            f"INSERT INTO {table} ({cols}) VALUES ({placeholders})",
            payload,
        )
        return int(cur.lastrowid)


def update_config_row(
    table: str, row_id: int, data: Dict[str, Any], db_path: Path = DB_PATH
) -> None:
    if table not in _CONFIG_LIST_COLUMNS:
        raise ValueError(f"Unknown config table: {table}")
    payload = _serialise_lists(table, data)
    payload["updated_at"] = _utc_now_iso()
    sets = ", ".join(f"{k} = :{k}" for k in payload)
    payload["__id"] = row_id
    with get_conn(db_path) as conn:
        conn.execute(f"UPDATE {table} SET {sets} WHERE id = :__id", payload)


def delete_config_row(table: str, row_id: int, db_path: Path = DB_PATH) -> None:
    if table not in _CONFIG_LIST_COLUMNS:
        raise ValueError(f"Unknown config table: {table}")
    with get_conn(db_path) as conn:
        conn.execute(f"DELETE FROM {table} WHERE id = ?", (row_id,))


def count_config_rows(db_path: Path = DB_PATH) -> Dict[str, int]:
    out: Dict[str, int] = {}
    with get_conn(db_path) as conn:
        for table in _CONFIG_LIST_COLUMNS:
            out[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    return out


def feedback_totals(db_path: Path = DB_PATH) -> Dict[str, int]:
    with get_conn(db_path) as conn:
        starred = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE starred = 1"
        ).fetchone()[0]
        ignored = conn.execute(
            "SELECT COUNT(*) FROM articles WHERE ignored = 1"
        ).fetchone()[0]
        domains_with_signal = conn.execute(
            """
            SELECT COUNT(DISTINCT article_domain) FROM articles
            WHERE article_domain IS NOT NULL AND (starred = 1 OR ignored = 1)
            """
        ).fetchone()[0]
        return {
            "starred": int(starred),
            "ignored": int(ignored),
            "domains_with_signal": int(domains_with_signal),
        }


# ---------------------------------------------------------------------------
# Golden-Set labels (v0.2.0 Feature 1)
# ---------------------------------------------------------------------------

_GOLDEN_LABELS = frozenset({"relevant", "noise"})


def add_golden_label(
    article_id: int,
    label: str,
    *,
    notes: Optional[str] = None,
    db_path: Path = DB_PATH,
) -> None:
    """Upsert a golden-set label. ``label`` must be 'relevant' or 'noise'.

    Raises ValueError on invalid label so callers surface bad input as 400,
    not 500. Uses ON CONFLICT so relabeling from relevant→noise is a single
    round-trip.
    """
    if label not in _GOLDEN_LABELS:
        raise ValueError(f"Invalid golden label: {label!r}")
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO golden_labels (article_id, label, added_at, notes)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(article_id) DO UPDATE SET
                label = excluded.label,
                added_at = excluded.added_at,
                notes = excluded.notes
            """,
            (article_id, label, _utc_now_iso(), notes),
        )


def remove_golden_label(article_id: int, db_path: Path = DB_PATH) -> None:
    """Delete a golden-set label. No-op if none exists."""
    with get_conn(db_path) as conn:
        conn.execute("DELETE FROM golden_labels WHERE article_id = ?", (article_id,))


def get_golden_label(article_id: int, db_path: Path = DB_PATH) -> Optional[str]:
    """Return the label ('relevant'|'noise') or None."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT label FROM golden_labels WHERE article_id = ?",
            (article_id,),
        ).fetchone()
        return row["label"] if row else None


def list_golden_labels(
    *, label: Optional[str] = None, db_path: Path = DB_PATH
) -> List[sqlite3.Row]:
    """Return labeled rows joined with article metadata for the admin view.

    ``label``: optional filter to 'relevant' or 'noise' (None = both).
    """
    sql = [
        """
        SELECT
            a.*,
            g.label AS golden_label,
            g.added_at AS golden_added_at,
            g.notes AS golden_notes
        FROM golden_labels g
        JOIN articles a ON a.id = g.article_id
        """
    ]
    params: List[Any] = []
    if label is not None:
        if label not in _GOLDEN_LABELS:
            raise ValueError(f"Invalid golden label: {label!r}")
        sql.append("WHERE g.label = ?")
        params.append(label)
    sql.append("ORDER BY g.added_at DESC")
    with get_conn(db_path) as conn:
        return list(conn.execute(" ".join(sql), params))


def count_golden_labels(
    *, label: Optional[str] = None, db_path: Path = DB_PATH
) -> int:
    with get_conn(db_path) as conn:
        if label is None:
            return int(
                conn.execute("SELECT COUNT(*) FROM golden_labels").fetchone()[0]
            )
        if label not in _GOLDEN_LABELS:
            raise ValueError(f"Invalid golden label: {label!r}")
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM golden_labels WHERE label = ?", (label,)
            ).fetchone()[0]
        )


def golden_label_map(db_path: Path = DB_PATH) -> Dict[int, str]:
    """Return ``{article_id: label}`` for every labeled article.

    Slim variant of :func:`list_golden_labels` — avoids the JOIN and
    full-article projection when the dashboard only needs to highlight
    R/N buttons on the row cards.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT article_id, label FROM golden_labels"
        ).fetchall()
        return {row["article_id"]: row["label"] for row in rows}


def fetch_labeled_articles_for_eval(db_path: Path = DB_PATH) -> List[sqlite3.Row]:
    """Return every labeled article joined with its label, ordered oldest-first.

    Ordering is deterministic (added_at ASC, article_id ASC) so eval runs are
    reproducible across invocations even without shuffling.
    """
    with get_conn(db_path) as conn:
        return list(
            conn.execute(
                """
                SELECT
                    a.*,
                    g.label AS golden_label,
                    g.added_at AS golden_added_at
                FROM golden_labels g
                JOIN articles a ON a.id = g.article_id
                ORDER BY g.added_at ASC, g.article_id ASC
                """
            )
        )


# ---------------------------------------------------------------------------
# Keyword suggestions (v0.2.0 Feature 2)
# ---------------------------------------------------------------------------

_SUGGESTION_STATUSES = frozenset({"pending", "approved", "dismissed"})


def insert_keyword_suggestion(
    *,
    phrase: str,
    phrase_normalized: str,
    source_article_id: Optional[int],
    market: Optional[str],
    sprache: Optional[str],
    db_path: Path = DB_PATH,
) -> int:
    """Persist a new pending suggestion. Callers must dedup upstream."""
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO keyword_suggestions
                (phrase, phrase_normalized, source_article_id, market,
                 sprache, suggested_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (
                phrase,
                phrase_normalized,
                source_article_id,
                market,
                sprache,
                _utc_now_iso(),
            ),
        )
        return int(cur.lastrowid)


def list_keyword_suggestions(
    *, status: Optional[str] = None, db_path: Path = DB_PATH
) -> List[Dict[str, Any]]:
    """Grouped by ``phrase_normalized`` (frequency), most-frequent first.

    Returns a list of dicts with fields:
      phrase_normalized, count, ids (list of individual suggestion ids),
      phrases (list of raw phrases seen), markets, sprachen,
      first_suggested_at, last_suggested_at, status.
    ``status`` filters the underlying rows before grouping (default: all).
    """
    if status is not None and status not in _SUGGESTION_STATUSES:
        raise ValueError(f"Invalid status: {status!r}")

    sql = ["SELECT * FROM keyword_suggestions"]
    params: List[Any] = []
    if status is not None:
        sql.append("WHERE status = ?")
        params.append(status)
    sql.append("ORDER BY suggested_at DESC")

    with get_conn(db_path) as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()

    groups: Dict[str, Dict[str, Any]] = {}
    for row in rows:
        key = row["phrase_normalized"]
        g = groups.setdefault(
            key,
            {
                "phrase_normalized": key,
                "count": 0,
                "ids": [],
                "phrases": [],
                "markets": [],
                "sprachen": [],
                "first_suggested_at": row["suggested_at"],
                "last_suggested_at": row["suggested_at"],
                "status": row["status"],
            },
        )
        g["count"] += 1
        g["ids"].append(row["id"])
        if row["phrase"] not in g["phrases"]:
            g["phrases"].append(row["phrase"])
        if row["market"] and row["market"] not in g["markets"]:
            g["markets"].append(row["market"])
        if row["sprache"] and row["sprache"] not in g["sprachen"]:
            g["sprachen"].append(row["sprache"])
        if row["suggested_at"] < g["first_suggested_at"]:
            g["first_suggested_at"] = row["suggested_at"]
        if row["suggested_at"] > g["last_suggested_at"]:
            g["last_suggested_at"] = row["suggested_at"]

    # Sort by frequency (desc), then latest last_suggested_at first.
    # Two-pass with stable sort: first by ts desc, then by count desc.
    # That gives equal-count groups the correct newest-first order.
    tmp = sorted(groups.values(), key=lambda g: g["last_suggested_at"], reverse=True)
    return sorted(tmp, key=lambda g: -g["count"])


def count_keyword_suggestions(
    *, status: Optional[str] = None, db_path: Path = DB_PATH
) -> int:
    with get_conn(db_path) as conn:
        if status is None:
            return int(
                conn.execute(
                    "SELECT COUNT(*) FROM keyword_suggestions"
                ).fetchone()[0]
            )
        if status not in _SUGGESTION_STATUSES:
            raise ValueError(f"Invalid status: {status!r}")
        return int(
            conn.execute(
                "SELECT COUNT(*) FROM keyword_suggestions WHERE status = ?",
                (status,),
            ).fetchone()[0]
        )


def suggestion_by_id(
    suggestion_id: int, db_path: Path = DB_PATH
) -> Optional[sqlite3.Row]:
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT * FROM keyword_suggestions WHERE id = ?",
            (suggestion_id,),
        ).fetchone()


def set_suggestion_status(
    suggestion_id: int,
    *,
    status: str,
    approved_keyword_id: Optional[int] = None,
    db_path: Path = DB_PATH,
) -> None:
    if status not in _SUGGESTION_STATUSES:
        raise ValueError(f"Invalid status: {status!r}")
    with get_conn(db_path) as conn:
        conn.execute(
            """
            UPDATE keyword_suggestions
            SET status = ?, approved_keyword_id = ?
            WHERE id = ?
            """,
            (status, approved_keyword_id, suggestion_id),
        )


def set_suggestion_group_status(
    phrase_normalized: str,
    *,
    status: str,
    approved_keyword_id: Optional[int] = None,
    db_path: Path = DB_PATH,
) -> int:
    """Bulk-update every pending suggestion matching a normalized phrase.

    Only touches rows currently in ``status='pending'``. Historic rows in
    ``approved`` or ``dismissed`` state stay untouched — the intent is that
    each admin action reflects the *pending queue at the time of the click*,
    not a rewrite of history. Returns the rowcount of the UPDATE (0 when
    the group was already emptied by a concurrent action — the caller
    should treat that as "someone beat us to it").
    """
    if status not in _SUGGESTION_STATUSES:
        raise ValueError(f"Invalid status: {status!r}")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE keyword_suggestions
            SET status = ?, approved_keyword_id = ?
            WHERE phrase_normalized = ? AND status = 'pending'
            """,
            (status, approved_keyword_id, phrase_normalized),
        )
        return cur.rowcount or 0


def attach_keyword_to_approved_group(
    phrase_normalized: str,
    *,
    keyword_id: int,
    db_path: Path = DB_PATH,
) -> int:
    """Post-approve step: link approved-group rows to the newly-created
    keywords row. Filtered to ``approved_keyword_id IS NULL`` so a
    subsequent re-approval (from a stale form) can't overwrite the link.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            """
            UPDATE keyword_suggestions
            SET approved_keyword_id = ?
            WHERE phrase_normalized = ?
              AND status = 'approved'
              AND approved_keyword_id IS NULL
            """,
            (keyword_id, phrase_normalized),
        )
        return cur.rowcount or 0


def dismiss_all_pending_suggestions(db_path: Path = DB_PATH) -> int:
    """Bulk-dismiss every pending keyword-suggestion in one UPDATE.

    Used by the "Dismiss all" button on the Suggestions admin page — when
    the pending queue has hundreds of low-signal proposals from a wide
    AI run, clicking each one out is tedious. Only rows in status='pending'
    are flipped; already-approved/dismissed rows are untouched. Returns
    the rowcount.
    """
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE keyword_suggestions SET status = 'dismissed' "
            "WHERE status = 'pending'"
        )
        return cur.rowcount or 0


def pending_suggestion_norms(db_path: Path = DB_PATH) -> set[str]:
    """Return the set of phrase_normalized values already pending. Used by
    :func:`ai_filter._persist_suggestions` for in-run dedup so we don't
    write the same phrase 40 times during a single pipeline run."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT phrase_normalized FROM keyword_suggestions "
            "WHERE status = 'pending'"
        ).fetchall()
        return {r["phrase_normalized"] for r in rows}

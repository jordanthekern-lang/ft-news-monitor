"""FastAPI dashboard for the Förder-Monitor."""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import (
    admin,
    ai_filter,
    cowork_bridge,
    database,
    domain_utils,
    enums,
    export,
    feedback,
    local_config,
    outlook_poller,
    pipeline,
    progress as pipeline_progress,
    runtime_settings,
    scheduler,
    seed_import,
    setup_wizard,
    translator,
)
from .config import (
    TEMPLATES_DIR,
    VALID_CATEGORIES,
    VALID_MARKETS,
    VALID_THEMES,
    get_settings,
    normalise_category,
    normalise_theme,
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI lifespan: DB init + in-process scheduler.

    Replaces the deprecated ``@app.on_event("startup")`` hook. The scheduler
    only starts when :data:`runtime_settings.SCHEDULER_ENABLED` is truthy —
    a fresh install without the wizard completed leaves it off. Shutdown
    always stops the scheduler so uvicorn reload / test teardown don't leak
    a live APScheduler event loop.
    """
    database.init_db()
    # v0.4.0 — merge shipped seed packs whose seed_version exceeds what
    # this install has already imported. No-op on Fresh installs (the wizard
    # runs its own explicit import) and on runs where every home market's
    # seed file is already at the stored version. See seed_import
    # .auto_reseed_home_markets for the exact preconditions.
    try:
        report = seed_import.auto_reseed_home_markets()
        for slug, entry in report.items():
            if entry.get("status") == "imported":
                logger.info(
                    "Auto-reseed: %s v%s applied (previous v%s), report=%s",
                    slug,
                    entry.get("seed_version"),
                    entry.get("previous_version"),
                    entry.get("report", {}).get("total"),
                )
    except Exception:  # pragma: no cover - defensive: never block startup
        logger.exception("Auto-reseed failed; continuing without it.")
    if runtime_settings.get(runtime_settings.SCHEDULER_ENABLED, False):
        scheduler.start()

    # v0.7.6 — Cowork file-bridge watcher. Only started when the tool is
    # configured to delegate scoring to a Cowork session; otherwise the
    # inbox/outbox directories are never touched.
    if get_settings().ai_provider == "cowork":
        cowork_bridge.start()

    try:
        yield
    finally:
        await cowork_bridge.stop()
        scheduler.stop()


# VERSION file at the repo root is the single source of truth for the
# release string (also embedded in the release ZIP, surfaced by
# build/build_release.py). Fall back to a dev sentinel when running from
# a checkout without the file (shouldn't happen in production).
_VERSION_FILE = Path(__file__).resolve().parent.parent / "VERSION"
APP_VERSION = (
    _VERSION_FILE.read_text(encoding="utf-8").strip()
    if _VERSION_FILE.exists()
    else "0.0.0-dev"
)

app = FastAPI(title="FT News Monitor", version=APP_VERSION, lifespan=lifespan)

# Localhost-only posture (docs/DELIVERY_v0.1.0.md#threat-model): the tool
# binds to 127.0.0.1 by design. Two middlewares harden that posture against
# the two well-known "localhost-is-safe" myths:
#
# 1. TrustedHostMiddleware blocks DNS-rebinding — a malicious site whose
#    A record briefly points at 127.0.0.1 could otherwise read /settings,
#    /admin/export-config, etc. after TTL flip. Only literal loopback
#    Host headers are accepted.
# 2. _origin_guard rejects state-changing (POST/PUT/DELETE/PATCH) requests
#    whose Origin or Referer isn't the loopback UI itself. That kills
#    CSRF drive-by attacks like a rogue page issuing
#    POST http://127.0.0.1:8766/setup/aws to overwrite the user's creds.
#
# Both are additive-only for the intended user (localhost browser); a
# curl/tests hit without headers still passes on state-changing routes
# because we require the negative — an Origin/Referer that IS set AND
# doesn't match — rather than requiring it to be present.
app.add_middleware(
    TrustedHostMiddleware,
    # Starlette strips the port before matching, so bare hostnames suffice.
    allowed_hosts=["127.0.0.1", "localhost", "testserver"],
)
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
app.include_router(admin.router)
app.include_router(setup_wizard.router)


# ---------------------------------------------------------------------------
# CSRF/Origin guard
# ---------------------------------------------------------------------------
_STATE_CHANGING_METHODS: frozenset = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_ALLOWED_ORIGIN_HOSTS: frozenset = frozenset({"127.0.0.1", "localhost", "testserver"})


def _origin_host_ok(header_value: str) -> bool:
    """True when a Origin/Referer header points at a loopback UI."""
    if not header_value:
        return False
    try:
        from urllib.parse import urlsplit
        parsed = urlsplit(header_value)
    except ValueError:
        return False
    return (parsed.hostname or "").lower() in _ALLOWED_ORIGIN_HOSTS


@app.middleware("http")
async def _origin_guard(request: Request, call_next):
    """Reject state-changing requests whose Origin/Referer isn't loopback.

    The check is deliberately lenient: only when the header IS present AND
    doesn't match do we 403. That keeps curl / CLI / test-harness flows
    working (they typically send no Origin), while blocking browser-driven
    CSRF (browsers always send Origin on cross-origin POSTs)."""
    if request.method not in _STATE_CHANGING_METHODS:
        return await call_next(request)
    origin = request.headers.get("origin") or ""
    referer = request.headers.get("referer") or ""
    # If neither header is present, this is a non-browser client (curl,
    # tests, scripts) — allow. If either header IS present, at least one
    # must resolve to loopback.
    if origin or referer:
        if not (_origin_host_ok(origin) or _origin_host_ok(referer)):
            return JSONResponse(
                {"error": "cross-origin request refused"}, status_code=403
            )
    return await call_next(request)

# Initialise the SQLite schema as soon as the app module is imported. This
# guarantees the tables exist for both `uvicorn` startup and `TestClient`,
# which doesn't always fire the lifespan (TestClient does, but only inside
# a ``with`` block — module-level DB access in tests still needs this).
database.init_db()


# ---------------------------------------------------------------------------
# First-run gate
# ---------------------------------------------------------------------------
#
# Until ``runtime_settings["setup_completed"]`` is truthy, any HTML page GET
# is redirected to ``/setup``. Kept as one middleware so the exemption list
# lives in a single place. POST routes (including the wizard's own posts)
# and JSON API endpoints under ``/api`` are always allowed through — the
# wizard needs to accept its own form submissions before the flag is set,
# and status/health JSON should stay callable for headless tooling.
# ``/favicon.ico`` and ``/static/*`` are exempted so browser chrome doesn't
# get 307-bounced into the wizard on every request.
_GATE_EXEMPT_EXACT: frozenset = frozenset({"/setup", "/favicon.ico"})
_GATE_EXEMPT_PREFIXES: tuple = ("/setup/", "/api/", "/static/")


def _is_gate_exempt(path: str) -> bool:
    if path in _GATE_EXEMPT_EXACT:
        return True
    return any(path.startswith(p) for p in _GATE_EXEMPT_PREFIXES)


@app.middleware("http")
async def _setup_gate(request: Request, call_next):
    path = request.url.path
    if request.method != "GET":
        return await call_next(request)
    if _is_gate_exempt(path):
        return await call_next(request)
    if runtime_settings.get(runtime_settings.SETUP_COMPLETED):
        return await call_next(request)
    return RedirectResponse(url="/setup", status_code=307)


# ---------------------------------------------------------------------------
# Jinja filters
# ---------------------------------------------------------------------------

def _local_dt(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    settings = get_settings()
    try:
        local = dt.astimezone(ZoneInfo(settings.timezone))
    except Exception:
        local = dt
    return local.strftime("%d.%m.%Y %H:%M")


def _date_only(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    settings = get_settings()
    try:
        local = dt.astimezone(ZoneInfo(settings.timezone))
    except Exception:
        local = dt
    return local.strftime("%d.%m.%Y")


def _days_old(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    return max(0, delta.days)


def _themes_list(value: Optional[str]) -> List[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
        if isinstance(loaded, list):
            return [str(t) for t in loaded]
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    return []


templates.env.filters["local_dt"] = _local_dt
templates.env.filters["date_only"] = _date_only
templates.env.filters["days_old"] = _days_old
templates.env.filters["themes_list"] = _themes_list

# Admin templates (F1 Golden set page) use the same filters — register on
# the admin router's Jinja env too so admin-hosted pages don't hit
# TemplateAssertionError on ``| local_dt`` etc.
for _flt_env in (admin.templates.env, setup_wizard.templates.env):
    _flt_env.filters["local_dt"] = _local_dt
    _flt_env.filters["date_only"] = _date_only
    _flt_env.filters["days_old"] = _days_old
    _flt_env.filters["themes_list"] = _themes_list


# ---------------------------------------------------------------------------
# Status param backward-compat: old German values → new English values
# ---------------------------------------------------------------------------

_STATUS_ALIASES: dict[str, str] = {
    "alle": "all",
    "neu": "new",
    "markiert": "starred",
}


def _normalise_status(status: str) -> str:
    """Map legacy German status params to canonical English; unknown → 'all'."""
    canonical = _STATUS_ALIASES.get(status, status)
    if canonical not in {"all", "new", "starred"}:
        return "all"
    return canonical


# ---------------------------------------------------------------------------
# Read-time normalisation (belt & braces for pre-migration rows)
# ---------------------------------------------------------------------------

def _normalise_article(item: dict) -> dict:
    """Run category and themes through alias maps so old German rows display correctly."""
    raw_category = item.get("category")
    if raw_category:
        item["category"] = normalise_category(raw_category)

    raw_themes = item.get("themes")
    if raw_themes:
        try:
            theme_list = json.loads(raw_themes) if isinstance(raw_themes, str) else raw_themes
            if isinstance(theme_list, list):
                item["themes"] = json.dumps(
                    [normalise_theme(t) for t in theme_list],
                    ensure_ascii=False,
                )
        except (json.JSONDecodeError, TypeError):
            pass
    return item


# ---------------------------------------------------------------------------
# Header chip context (last-run / next-run pill in base.html)
# ---------------------------------------------------------------------------


def _relative_last_run(iso: Optional[str]) -> str:
    """Compact relative label: '5m ago' / '2h ago' / '3d ago' / 'never'.

    Timezone convention matches the rest of the app: parse as UTC when the
    stored value is naive, otherwise trust the offset.
    """
    if not iso:
        return "never"
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return "unknown"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _next_run_label() -> str:
    """HH:MM label for the scheduler's next fire (local tz), or '—' when off."""
    dt = scheduler.next_run_at()
    if dt is None:
        return "—"
    settings = get_settings()
    try:
        local = dt.astimezone(ZoneInfo(settings.timezone))
    except Exception:
        local = dt
    return local.strftime("%H:%M")


def _header_chip_last_run() -> str:
    """Jinja global — relative label like '5m ago' or 'never'."""
    return _relative_last_run(runtime_settings.get(runtime_settings.LAST_RUN_AT))


def _header_chip_next_run() -> str:
    """Jinja global — HH:MM next-fire label, or '—' when scheduler is off."""
    return _next_run_label()


def _nav_golden_count() -> int:
    """Jinja global — number of labeled articles for the header nav badge."""
    try:
        return database.count_golden_labels()
    except Exception:  # pragma: no cover - defensive
        return 0


def _nav_suggestions_count() -> int:
    """Jinja global — count of pending keyword suggestions (F2)."""
    try:
        return database.count_keyword_suggestions(status="pending")
    except Exception:  # pragma: no cover - defensive
        return 0


# Exposed as globals so base.html can render the chip on every page that
# extends it, without every route having to pass the context through.
# Each router (admin, setup_wizard) builds its own Jinja2Templates so we
# register the globals on all of them.
for _env in (templates.env, admin.templates.env, setup_wizard.templates.env):
    _env.globals["header_chip_last_run"] = _header_chip_last_run
    _env.globals["header_chip_next_run"] = _header_chip_next_run
    _env.globals["nav_golden_count"] = _nav_golden_count
    _env.globals["nav_suggestions_count"] = _nav_suggestions_count


# ---------------------------------------------------------------------------
# Dashboard routes
# ---------------------------------------------------------------------------

def _default_min_score() -> int:
    """Dashboard default min score: runtime setting, falling back to 6."""
    value = runtime_settings.get(runtime_settings.MIN_SCORE_DEFAULT)
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10:
        return value
    return 6


def _parse_dashboard_filters(
    *,
    min_score: Optional[int],
    theme: List[str],
    market: List[str],
    days: int,
    status: str,
) -> dict:
    """Normalise raw dashboard query params into a filter dict.

    Shared between the dashboard HTML route and the export routes so both
    honour exactly the same query-param surface. Enforces:

    * unknown themes / markets are silently dropped
    * empty explicit market selection falls back to home markets (runtime
      setting); still-empty means "all markets"
    * missing min_score falls back to the runtime default
    * status is canonicalised via ``_normalise_status``
    """
    canonical_status = _normalise_status(status)
    themes = [t for t in theme if t in VALID_THEMES]

    markets = [m for m in market if m in VALID_MARKETS]
    if not markets:
        markets = runtime_settings.effective_home_markets() or []

    if min_score is None:
        min_score = _default_min_score()

    return {
        "min_score": min_score,
        "themes": themes,
        "markets": markets,
        "days": days,
        "status": canonical_status,
    }


def _articles_for_filters(filters: dict, *, limit: int = 200) -> list:
    """Fetch articles matching the parsed dashboard filters."""
    return database.list_articles(
        min_score=filters["min_score"],
        markets=filters["markets"] or None,
        themes=filters["themes"] or None,
        days=filters["days"],
        status=filters["status"],
        limit=limit,
    )


def _filters_query_string(filters: dict) -> str:
    """Re-serialise a parsed filter dict as a URL query string.

    Used by the dashboard template to build ``<a>`` links to the export
    endpoints that carry the *current* filter state. Only non-default keys
    are emitted so short-and-readable URLs stay short.
    """
    parts: List[str] = []
    for market in filters["markets"]:
        parts.append(f"market={market}")
    for theme in filters["themes"]:
        parts.append(f"theme={theme}")
    parts.append(f"min_score={filters['min_score']}")
    parts.append(f"days={filters['days']}")
    parts.append(f"status={filters['status']}")
    return "&".join(parts)


@app.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    min_score: Optional[int] = Query(None, ge=0, le=10),
    theme: List[str] = Query(default=[]),
    market: List[str] = Query(default=[]),
    days: int = Query(30, ge=1, le=365),
    status: str = Query("all"),
):
    settings = get_settings()
    filters = _parse_dashboard_filters(
        min_score=min_score, theme=theme, market=market, days=days, status=status
    )
    articles = _articles_for_filters(filters)

    # Fetch every existing golden label in one slim query (article_id + label
    # only — avoids the JOIN + full-article projection used by the admin view).
    golden_map = database.golden_label_map()

    # enrich each article with outlet match for trust indicator
    enriched = []
    for row in articles:
        item = _normalise_article(dict(row))
        item["final_score"] = (
            (item.get("relevance_score") or 0)
            + (item.get("trust_bonus") or 0)
            + (item.get("feedback_bonus") or 0)
        )
        item["themes_parsed"] = _themes_list(item.get("themes"))
        item["outlet_match"] = domain_utils.match_article_to_outlet(item["url"])
        item["display_title"] = item.get("title_en") or item.get("title")
        item["display_snippet"] = item.get("snippet_en") or item.get("snippet")
        item["is_translated"] = bool(item.get("title_en") or item.get("snippet_en"))
        item["golden_label"] = golden_map.get(item["id"])
        enriched.append(item)

    pending_for_cowork = 0
    if settings.ai_provider == "cowork":
        pending_for_cowork = len(database.fetch_articles_for_ai())

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "articles": enriched,
            "stats": database.stats(),
            "source_health": database.source_health_summary(),
            "filters": filters,
            "filters_qs": _filters_query_string(filters),
            "all_themes": VALID_THEMES,
            "all_markets": VALID_MARKETS,
            "highlight_days": settings.highlight_days,
            "ai_provider": settings.ai_provider,
            "pending_for_cowork": pending_for_cowork,
        },
    )


# ---------------------------------------------------------------------------
# Export routes (Markdown + CSV, honouring the current dashboard filters)
# ---------------------------------------------------------------------------


def _export_filename(ext: str) -> str:
    """Attachment filename: ``ft-news-monitor_<yyyy-mm-dd>.<ext>``."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"ft-news-monitor_{today}.{ext}"


@app.get("/export.md")
def export_markdown(
    request: Request,
    min_score: Optional[int] = Query(None, ge=0, le=10),
    theme: List[str] = Query(default=[]),
    market: List[str] = Query(default=[]),
    days: int = Query(30, ge=1, le=365),
    status: str = Query("all"),
    include_reasoning: bool = Query(False),
    copy: int = Query(0),
):
    """Confluence-friendly markdown export of the current dashboard filter set.

    ``?copy=1`` swaps the download response for an inline ``text/plain`` body
    — the "Copy as Markdown" button in the dashboard fetches this variant
    and pushes the response into the clipboard.
    """
    filters = _parse_dashboard_filters(
        min_score=min_score, theme=theme, market=market, days=days, status=status
    )
    rows = _articles_for_filters(filters, limit=500)
    articles = [_normalise_article(dict(r)) for r in rows]
    body = export.build_markdown(articles, include_reasoning=include_reasoning)

    if copy:
        return Response(
            content=body,
            media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": "inline"},
        )
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_export_filename("md")}"'
        },
    )


@app.get("/export.csv")
def export_csv(
    request: Request,
    min_score: Optional[int] = Query(None, ge=0, le=10),
    theme: List[str] = Query(default=[]),
    market: List[str] = Query(default=[]),
    days: int = Query(30, ge=1, le=365),
    status: str = Query("all"),
):
    """UTF-8-BOM CSV export (Excel double-click friendly)."""
    filters = _parse_dashboard_filters(
        min_score=min_score, theme=theme, market=market, days=days, status=status
    )
    rows = _articles_for_filters(filters, limit=500)
    articles = [_normalise_article(dict(r)) for r in rows]
    body = export.build_csv(articles)
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_export_filename("csv")}"'
        },
    )


_EXCLUSION_REASON_EN = {
    "Pre-Filter": "Pre-filter",
    "Pre-Screen": "Pre-screen",
    "Niedrig bewertet": "Low score",
    "Low score": "Low score",
    "Noch offen": "Pending",
    "Pending": "Pending",
}


@app.get("/filtered", response_class=HTMLResponse)
def filtered_view(request: Request, kind: str = Query("alle")):
    if kind not in {"alle", "prefilter", "prescreen", "lowscore", "pending"}:
        kind = "alle"
    rows = database.list_excluded_articles(kind=kind)
    enriched = []
    for row in rows:
        item = _normalise_article(dict(row))
        item["final_score"] = (item.get("relevance_score") or 0) + (item.get("trust_bonus") or 0)
        item["themes_parsed"] = _themes_list(item.get("themes"))
        item["exclusion_reason_en"] = _EXCLUSION_REASON_EN.get(
            item.get("exclusion_reason"), item.get("exclusion_reason") or "Unknown"
        )
        enriched.append(item)
    return templates.TemplateResponse(
        request,
        "filtered.html",
        {
            "articles": enriched,
            "kind": kind,
            "counts": database.excluded_counts(),
        },
    )


@app.get("/article/{article_id}", response_class=HTMLResponse)
def article_detail(request: Request, article_id: int):
    row = database.article_by_id(article_id)
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    item = _normalise_article(dict(row))
    item["final_score"] = (
        (item.get("relevance_score") or 0)
        + (item.get("trust_bonus") or 0)
        + (item.get("feedback_bonus") or 0)
    )
    item["themes_parsed"] = _themes_list(item.get("themes"))
    item["outlet_match"] = domain_utils.match_article_to_outlet(item["url"])
    item["is_translated"] = bool(item.get("title_en") or item.get("snippet_en"))
    item["golden_label"] = database.get_golden_label(article_id)
    return templates.TemplateResponse(
        request,
        "detail.html",
        {"article": item},
    )


@app.post("/article/{article_id}/star")
def article_star(article_id: int):
    new_state = database.toggle_flag(article_id, "starred")
    return {"starred": new_state}


@app.post("/article/{article_id}/read")
def article_read(article_id: int):
    new_state = database.toggle_flag(article_id, "read")
    return {"read": new_state}


@app.post("/article/{article_id}/ignore")
def article_ignore(article_id: int):
    new_state = database.toggle_flag(article_id, "ignored")
    return {"ignored": new_state}


# ---------------------------------------------------------------------------
# Open a newsletter article back in Outlook (v0.3.2)
# ---------------------------------------------------------------------------


@app.post("/outlook/open/{article_id}")
async def outlook_open(article_id: int):
    """Re-open the source mail in the running Outlook desktop app.

    Returns 200 in both success and controlled-failure cases so the JS
    handler can surface a helpful message (e.g. "row predates v0.3.2 —
    no EntryID stored") without treating it as a network error. A hard
    404 is only used when the article does not exist.
    """
    row = database.article_by_id(article_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Article not found.")
    if row["source_type"] != "newsletter":
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "This is not a newsletter article — there's no mail "
                    "in Outlook to open."
                ),
            },
        )
    keys = row.keys()
    entry_id = row["outlook_entry_id"] if "outlook_entry_id" in keys else None
    store_id = row["outlook_store_id"] if "outlook_store_id" in keys else None
    if not entry_id:
        subject = (row["title"] or "").strip() or "(no subject)"
        return JSONResponse(
            {
                "ok": False,
                "error": (
                    "This newsletter was ingested before v0.3.2 and has no "
                    "Outlook link stored. Search your Outlook folder for "
                    f"the subject: {subject!r}"
                ),
            },
        )
    result = await outlook_poller.open_item(entry_id=entry_id, store_id=store_id)
    return JSONResponse(result)


# ---------------------------------------------------------------------------
# Golden-set labels (v0.2.0 Feature 1)
# ---------------------------------------------------------------------------
#
# Unlike ``star``/``read``/``ignore`` (which are cheap daily toggles), golden
# labels are the ground-truth for eval — always an explicit ``label`` on the
# URL, never a toggle, so mislabeling has to be intentional (clear, then set).
_GOLDEN_LABEL_ACTIONS = {"relevant", "noise", "clear"}


@app.post("/article/{article_id}/golden/{action}")
def article_golden(article_id: int, action: str):
    if action not in _GOLDEN_LABEL_ACTIONS:
        raise HTTPException(status_code=400, detail=f"Unknown action: {action}")
    if database.article_by_id(article_id) is None:
        raise HTTPException(status_code=404, detail="Article not found")
    if action == "clear":
        database.remove_golden_label(article_id)
        return {"golden_label": None}
    try:
        database.add_golden_label(article_id, action)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"golden_label": action}


# NB: /admin/golden is registered on admin.router in app/admin.py so it wins
# against the router's catch-all /admin/{slug} route.


@app.post("/reload-config")
def reload_config():
    cfg = local_config.load_config()
    return {
        "ok": True,
        "counts": {
            "keywords": len(cfg.keywords),
            "feeds": len(cfg.feeds),
            "outlets": len(cfg.outlets),
            "negative_keywords": len(cfg.negative_keywords),
        },
        "loaded_at": cfg.loaded_at.isoformat(),
    }


@app.post("/run-collection")
async def run_collection_endpoint():
    """Run the full pipeline via app.pipeline (home markets, run lock)."""
    result = await pipeline.run_pipeline()
    if result.get("status") == "already_running":
        return JSONResponse(result, status_code=409)
    return result


@app.get("/pipeline-status")
def pipeline_status_endpoint():
    """Poll target for the dashboard progress bar (v0.7.4).

    Returns the live phase + sub-step of the currently running pipeline.
    When nothing is running the response still returns 200 with
    ``phase='idle'`` (or 'done' after the most recent run finished) so
    the UI can decide whether to hide the bar without special-casing
    HTTP status codes.
    """
    return JSONResponse(pipeline_progress.snapshot())


@app.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, saved: int = Query(0)):
    diag = local_config.diagnose()
    settings = get_settings()

    override = runtime_settings.get(runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE)
    suggest_kw = ai_filter._suggest_keywords_enabled()
    translation_auto = sorted(translator.translation_target_langs())
    translation_manual = runtime_settings.get(runtime_settings.TRANSLATION_TARGET_LANGS)
    if not isinstance(translation_manual, list):
        translation_manual = []
    runtime = {
        "home_markets": runtime_settings.get(runtime_settings.HOME_MARKETS) or [],
        "min_score_default": _default_min_score(),
        "ai_skip_prescreen_days_override": override if isinstance(override, int) else "",
        "scheduler_enabled": bool(
            runtime_settings.get(runtime_settings.SCHEDULER_ENABLED)
        ),
        "scheduler_time": runtime_settings.get(runtime_settings.SCHEDULER_TIME)
        or "06:00",
        "suggest_keywords_enabled": suggest_kw,
        "translation_target_langs_effective": translation_auto,
        "translation_target_langs_manual": translation_manual,
        "translation_target_langs_mode": "manual" if translation_manual else "auto",
    }

    import os as _os

    outlook_state = {
        "enabled": (
            _os.environ.get("OUTLOOK_ENABLED", "").strip().lower()
            in {"1", "true", "yes", "on"}
        ),
        "folder_path": _os.environ.get("OUTLOOK_FOLDER_PATH", ""),
        "store_name": _os.environ.get("OUTLOOK_STORE_NAME", ""),
        "max_age_days": _os.environ.get("OUTLOOK_MAX_AGE_DAYS", "30"),
        "max_links_per_email": _os.environ.get("OUTLOOK_MAX_LINKS_PER_EMAIL", "20"),
    }

    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "diag": diag,
            "saved": bool(saved),
            "runtime": runtime,
            "all_markets": VALID_MARKETS,
            "all_languages": enums.LANGUAGE_OPTIONS,
            "outlook_state": outlook_state,
            "feedback_totals": database.feedback_totals(),
            "feedback_domains": feedback.domain_summary(limit=30),
            "translation_stats": database.translation_stats(),
            "translation_enabled": translator._translation_enabled(),
            "settings": {
                "anthropic_model": settings.anthropic_model,
                "dashboard_port": settings.dashboard_port,
                "timezone": settings.timezone,
                "max_age_days": settings.max_age_days,
                "highlight_days": settings.highlight_days,
                "ai_filter_max_parallel": settings.ai_filter_max_parallel,
                "ai_skip_prescreen_days": settings.ai_skip_prescreen_days,
            },
        },
    )


_SCHEDULER_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@app.post("/settings/update")
async def settings_update(request: Request):
    """Persist user preferences (runtime settings). Server-side validation:
    markets ⊆ VALID_MARKETS, min score 0–10, override empty or >= 0, HH:MM."""
    form = await request.form()

    home_markets = [str(v) for v in form.getlist("home_markets")]
    invalid_markets = [m for m in home_markets if m not in VALID_MARKETS]
    if invalid_markets:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid market(s): {', '.join(invalid_markets)}",
        )

    raw_score = str(form.get("min_score_default") or "").strip()
    try:
        min_score_default = int(raw_score)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail="Default min score must be a whole number between 0 and 10.",
        )
    if not 0 <= min_score_default <= 10:
        raise HTTPException(
            status_code=400,
            detail="Default min score must be between 0 and 10.",
        )

    raw_override = str(form.get("ai_skip_prescreen_days_override") or "").strip()
    if raw_override == "":
        prescreen_override = None  # use env default
    else:
        try:
            prescreen_override = int(raw_override)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail="Pre-screen bypass days must be a whole number (or empty for the default).",
            )
        if prescreen_override < 0:
            raise HTTPException(
                status_code=400,
                detail="Pre-screen bypass days must be 0 or greater.",
            )

    scheduler_enabled = bool(form.get("scheduler_enabled"))
    scheduler_time = str(form.get("scheduler_time") or "").strip()
    if not _SCHEDULER_TIME_RE.match(scheduler_time):
        raise HTTPException(
            status_code=400,
            detail="Scheduler time must be in HH:MM format (e.g. 06:30).",
        )

    suggest_keywords_enabled = bool(form.get("suggest_keywords_enabled"))

    # Translation targets: "auto" mode clears the manual override, "manual"
    # persists the checkbox-selected ISO codes. Codes are validated against
    # ``enums.LANGUAGE_OPTIONS`` (case-insensitive) and stored lower-case
    # so ``translator.translation_target_langs`` doesn't have to normalise.
    translation_mode = str(form.get("translation_target_langs_mode") or "auto").strip().lower()
    if translation_mode == "manual":
        raw_langs = [str(v).strip().lower() for v in form.getlist("translation_target_langs")]
        allowed = {code.lower() for code in enums.LANGUAGE_OPTIONS}
        translation_langs = [c for c in raw_langs if c and c in allowed]
        # Enforce a sensible floor: english is always safe to skip because
        # the UI + Claude prompt are English. Empty selection collapses to
        # auto mode instead of an unusable "translate everything" state.
        if not translation_langs:
            translation_mode = "auto"
    else:
        translation_langs = []

    # v0.6.1 M5: when the user ADDS a new home market via Settings,
    # trigger auto-reseed immediately so the newly-selected market's
    # keywords/feeds/outlets show up in the next collection run. Without
    # this the user would have to restart the app to pick them up.
    previous_markets = runtime_settings.get(runtime_settings.HOME_MARKETS) or []
    if not isinstance(previous_markets, list):
        previous_markets = []
    added_markets = sorted(set(home_markets) - set(previous_markets))

    runtime_settings.set(runtime_settings.HOME_MARKETS, home_markets)
    runtime_settings.set(runtime_settings.MIN_SCORE_DEFAULT, min_score_default)
    runtime_settings.set(
        runtime_settings.AI_SKIP_PRESCREEN_DAYS_OVERRIDE, prescreen_override
    )
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, scheduler_enabled)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, scheduler_time)
    runtime_settings.set(
        runtime_settings.SUGGEST_KEYWORDS_ENABLED, suggest_keywords_enabled
    )
    if translation_mode == "manual":
        runtime_settings.set(
            runtime_settings.TRANSLATION_TARGET_LANGS, translation_langs
        )
    else:
        runtime_settings.set(runtime_settings.TRANSLATION_TARGET_LANGS, None)
    # Live-reload the daily job so the settings change is felt without a
    # restart. No-op when the scheduler isn't running.
    scheduler.reload()

    # v0.6.1 M5: trigger auto-reseed for newly-added markets so their
    # keywords/feeds/outlets are available on the next Run collection
    # click. No-op if no markets were added or setup isn't complete.
    if added_markets:
        try:
            seed_import.auto_reseed_home_markets()
            logger.info(
                "Auto-reseed after settings save: new markets=%s", added_markets
            )
        except Exception:  # pragma: no cover - defensive: never block save
            logger.exception("Auto-reseed after settings save failed.")

    return RedirectResponse(url="/settings?saved=1", status_code=303)


@app.get("/api/stats")
def api_stats():
    return JSONResponse(
        {
            "stats": database.stats(),
            "source_health": database.source_health_summary(),
        }
    )


# ---------------------------------------------------------------------------
# Cowork ranking API (v0.7.6)
# ---------------------------------------------------------------------------
#
# Three endpoints let an external Cowork session score articles instead of
# the built-in Bedrock/Anthropic scorer. Only relevant when AI_PROVIDER=cowork,
# but the endpoints exist unconditionally so the Cowork skill can probe
# /api/health before deciding whether to run.
#
# Auth: COWORK_API_TOKEN in .env. Any non-empty token gates all three
# endpoints. Empty token allows anonymous access (dev/tests only).


def _require_cowork_token(request: Request) -> None:
    """Reject the request when the shared token is set but doesn't match.

    The header name is X-Cowork-Token. When COWORK_API_TOKEN is empty
    (dev default), the check is a no-op — otherwise a missing / wrong
    header returns 401.
    """
    expected = get_settings().cowork_api_token
    if not expected:
        return
    provided = request.headers.get("x-cowork-token", "")
    if provided != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing X-Cowork-Token")


@app.get("/api/health")
def api_health():
    """Liveness probe for the Cowork skill.

    Returns the app version and pending-articles count so the skill can
    detect "nothing to rank" without a second call.
    """
    settings = get_settings()
    pending_count = len(database.fetch_articles_for_ai())
    return JSONResponse(
        {
            "status": "ok",
            "version": APP_VERSION,
            "ai_provider": settings.ai_provider,
            "pending_count": pending_count,
        }
    )


_PENDING_FIELDS = (
    "id",
    "title",
    "source_name",
    "url",
    "published_at",
    "snippet",
    "country",
    "language",
)


@app.get("/api/articles/pending")
def api_articles_pending(request: Request, limit: int = Query(200, ge=1, le=1000)):
    """Return unscored articles for external ranking.

    Fields match what ``ai_filter.USER_TEMPLATE`` (app/ai_filter.py:117-132)
    renders for the in-process Bedrock scorer, so Cowork has the same
    inputs.
    """
    _require_cowork_token(request)
    rows = database.fetch_articles_for_ai()[:limit]
    articles = []
    for row in rows:
        item = {}
        keys = row.keys()
        for field in _PENDING_FIELDS:
            item[field] = row[field] if field in keys else None
        articles.append(item)
    return JSONResponse(
        {
            "articles": articles,
            "total": len(articles),
            "categories": VALID_CATEGORIES,
            "themes": VALID_THEMES,
        }
    )


_SCORE_REQUIRED = ("id", "relevance_score", "category", "themes", "reasoning")


@app.post("/api/articles/scores")
async def api_articles_scores(request: Request):
    """Accept a batch of scores from the external ranker.

    Body: ``{"scores": [{"id": int, "relevance_score": int, "category": str,
    "themes": list, "reasoning": str, "trust_bonus"?: int}, ...]}``.

    Idempotent: re-posting the same score is a no-op UPDATE. Unknown IDs
    are silently skipped (Cowork may have a stale pending list).
    """
    _require_cowork_token(request)
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Request body must be JSON")

    scores = body.get("scores")
    if not isinstance(scores, list):
        raise HTTPException(
            status_code=422, detail="Body must contain a 'scores' list"
        )

    updated = 0
    skipped = 0
    errors = []
    for idx, entry in enumerate(scores):
        if not isinstance(entry, dict):
            errors.append({"index": idx, "error": "Entry must be an object"})
            continue
        missing = [k for k in _SCORE_REQUIRED if k not in entry]
        if missing:
            errors.append(
                {"index": idx, "error": f"Missing fields: {', '.join(missing)}"}
            )
            continue
        try:
            article_id = int(entry["id"])
            relevance_score = int(entry["relevance_score"])
            if not 0 <= relevance_score <= 10:
                raise ValueError("relevance_score out of range 0..10")
        except (TypeError, ValueError) as exc:
            errors.append({"index": idx, "error": str(exc)})
            continue

        themes = entry.get("themes") or []
        if not isinstance(themes, list):
            errors.append({"index": idx, "error": "themes must be a list"})
            continue

        if database.article_by_id(article_id) is None:
            skipped += 1
            continue

        database.update_ai_score(
            article_id,
            relevance_score=relevance_score,
            trust_bonus=int(entry.get("trust_bonus") or 0),
            feedback_bonus=int(entry.get("feedback_bonus") or 0),
            category=normalise_category(str(entry["category"])),
            themes=[normalise_theme(str(t)) for t in themes],
            reasoning=str(entry["reasoning"]),
        )
        updated += 1

    if errors:
        return JSONResponse(
            {"updated": updated, "skipped": skipped, "errors": errors},
            status_code=422,
        )
    return JSONResponse({"updated": updated, "skipped": skipped, "errors": []})

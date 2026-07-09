"""Admin routes for editing the local config tables in-browser.

Four tables, one generic CRUD pattern per table:
    GET  /admin/<table>           list view
    GET  /admin/<table>/new       blank form
    POST /admin/<table>/new       create
    GET  /admin/<table>/<id>/edit edit form
    POST /admin/<table>/<id>/edit update
    POST /admin/<table>/<id>/delete  delete

Multi-value fields (markt, themenbereich) come in as comma-separated form
strings and get split into JSON lists before persistence.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import database, enums, local_config
from .config import (
    TEMPLATES_DIR,
    VALID_CATEGORIES,
    VALID_MARKETS,
    VALID_THEMES,
)


router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Per-table metadata: column definitions used by the generic templates.
# Each "field" dict is consumed by both list and edit templates.
# ---------------------------------------------------------------------------

def _list_choices(values):
    return [{"value": v, "label": v} for v in values]


_PRIORITY_CHOICES = _list_choices(enums.PRIORITY_OPTIONS)
_LANGUAGE_CHOICES = _list_choices(enums.LANGUAGE_OPTIONS)
_MARKET_CHOICES = _list_choices(VALID_MARKETS)
_THEME_CHOICES = _list_choices(VALID_THEMES)


TABLE_META: Dict[str, Dict[str, Any]] = {
    "keywords": {
        "label": "Keywords",
        "singular": "keyword",
        "fields": [
            {"name": "suchbegriff", "label": "Search term", "type": "text", "required": True, "list_col": True},
            {"name": "markt", "label": "Markets", "type": "multiselect", "choices": _MARKET_CHOICES, "list_col": True},
            {"name": "themenbereich", "label": "Themes", "type": "multiselect", "choices": _THEME_CHOICES, "list_col": False},
            {"name": "prioritaet", "label": "Priority", "type": "select", "choices": _PRIORITY_CHOICES, "list_col": True},
            {"name": "sprache", "label": "Language", "type": "select", "choices": _LANGUAGE_CHOICES, "list_col": True, "required": True},
            {"name": "notizen", "label": "Notes", "type": "textarea", "list_col": False},
        ],
        "list_columns": ["suchbegriff", "markt", "prioritaet", "sprache"],
    },
    "feeds": {
        "label": "Feeds",
        "singular": "feed",
        "fields": [
            {"name": "rss_feed", "label": "Feed URL or query", "type": "text", "required": True, "list_col": True, "placeholder": "https://example.com/rss"},
            {"name": "typ", "label": "Type", "type": "select", "choices": _list_choices(enums.FEED_TYPE_OPTIONS), "list_col": True},
            {"name": "markt", "label": "Markets", "type": "multiselect", "choices": _MARKET_CHOICES, "list_col": True},
            {"name": "themenbereich", "label": "Themes", "type": "multiselect", "choices": _THEME_CHOICES, "list_col": False},
            {"name": "prioritaet", "label": "Priority", "type": "select", "choices": _PRIORITY_CHOICES, "list_col": True},
            {"name": "sprache", "label": "Language", "type": "select", "choices": _LANGUAGE_CHOICES, "list_col": True},
            {"name": "notizen", "label": "Notes", "type": "textarea", "list_col": False},
        ],
        "list_columns": ["rss_feed", "typ", "markt", "prioritaet", "sprache"],
    },
    "outlets": {
        "label": "Outlets",
        "singular": "outlet",
        "fields": [
            {"name": "outlet", "label": "Outlet name", "type": "text", "required": True, "list_col": True},
            {"name": "url", "label": "Homepage URL", "type": "text", "list_col": True, "placeholder": "https://www.outlet.com/"},
            {"name": "markt", "label": "Markets", "type": "multiselect", "choices": _MARKET_CHOICES, "list_col": True},
            {"name": "themenbereich", "label": "Themes", "type": "multiselect", "choices": _THEME_CHOICES, "list_col": False},
            {"name": "outlet_typ", "label": "Outlet type", "type": "select", "choices": _list_choices(enums.OUTLET_TYPE_OPTIONS), "list_col": True},
            {"name": "prioritaet", "label": "Priority", "type": "select", "choices": _PRIORITY_CHOICES, "list_col": True},
            {"name": "sprache", "label": "Language", "type": "select", "choices": _LANGUAGE_CHOICES, "list_col": False},
            {"name": "hat_rss", "label": "Has RSS feed", "type": "checkbox", "list_col": True},
            {"name": "notizen", "label": "Notes", "type": "textarea", "list_col": False},
        ],
        "list_columns": ["outlet", "url", "markt", "outlet_typ", "prioritaet", "hat_rss"],
    },
    "negative_keywords": {
        "label": "Excludes",
        "singular": "exclude",
        "fields": [
            {"name": "begriff", "label": "Excluded term", "type": "text", "required": True, "list_col": True},
            {"name": "markt", "label": "Markets", "type": "multiselect", "choices": _MARKET_CHOICES, "list_col": True},
            {"name": "kategorie", "label": "Category", "type": "select", "choices": _list_choices(enums.NEGATIVE_CATEGORY_OPTIONS), "list_col": True},
            {"name": "notizen", "label": "Notes", "type": "textarea", "list_col": False},
        ],
        "list_columns": ["begriff", "markt", "kategorie"],
    },
}


# Routing aliases — the URL slug "negative-keywords" is friendlier than the
# table name "negative_keywords".
URL_TO_TABLE = {
    "keywords": "keywords",
    "feeds": "feeds",
    "outlets": "outlets",
    "negative-keywords": "negative_keywords",
}


def _resolve(slug: str) -> str:
    table = URL_TO_TABLE.get(slug)
    if not table:
        raise HTTPException(status_code=404, detail="Unknown admin table")
    return table


def _form_to_payload(table: str, form: Dict[str, Any]) -> Dict[str, Any]:
    """Translate a form-data dict into the shape ``database.insert/update`` expects."""
    meta = TABLE_META[table]
    payload: Dict[str, Any] = {}
    for field in meta["fields"]:
        name = field["name"]
        ftype = field["type"]
        raw = form.get(name)
        if ftype == "multiselect":
            # form.getlist would be ideal but we collapse to comma-separated for
            # the textarea fallback path. The Starlette form gives a list when
            # the same key appears multiple times (handled by the route below).
            if isinstance(raw, list):
                values = [v.strip() for v in raw if v and v.strip()]
            elif isinstance(raw, str):
                values = [v.strip() for v in raw.split(",") if v.strip()]
            else:
                values = []
            payload[name] = values
        elif ftype == "checkbox":
            payload[name] = bool(raw)
        elif ftype == "select":
            payload[name] = (raw or "").strip() or None
            if name == "prioritaet":
                payload[name] = enums.normalise_priority(payload[name])
            elif name == "outlet_typ":
                payload[name] = enums.normalise_outlet_type(payload[name])
            elif name == "kategorie":
                payload[name] = enums.normalise_negative_category(payload[name])
        else:  # text, textarea
            payload[name] = (raw or "").strip() or None
    return payload


def _form_value_for(field: Dict[str, Any], row: Optional[Dict[str, Any]]) -> Any:
    """Pre-fill helper: returns the value to render in the edit form."""
    if not row:
        return [] if field["type"] == "multiselect" else (False if field["type"] == "checkbox" else "")
    val = row.get(field["name"])
    if field["type"] == "multiselect":
        return val if isinstance(val, list) else []
    if field["type"] == "checkbox":
        return bool(val)
    return val if val is not None else ""


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/export-config")
def export_config():
    """Dump the current config tables as a seed-format JSON download.

    Purpose: market curators mail their improvements back so Jordan can fold
    them into the official seed packs. The file is import-compatible with
    ``app.seed_import.import_seed_file(mode="merge")`` — round-trip works.
    """
    from datetime import date

    payload: Dict[str, Any] = {
        "market": "custom-export",
        "seed_version": 1,
        "keywords": [_row_to_seed(row, "keywords") for row in database.list_config("keywords")],
        "feeds": [_row_to_seed(row, "feeds") for row in database.list_config("feeds")],
        "outlets": [_row_to_seed(row, "outlets") for row in database.list_config("outlets")],
        "negative_keywords": [_row_to_seed(row, "negative_keywords") for row in database.list_config("negative_keywords")],
    }
    filename = f"config-export_{date.today().isoformat()}.json"
    return JSONResponse(
        content=payload,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# Fields dropped when exporting rows back to seed format (auto-managed by DB).
_EXPORT_STRIP = {"id", "created_at", "updated_at"}


def _row_to_seed(row: Dict[str, Any], table: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for k, v in row.items():
        if k in _EXPORT_STRIP:
            continue
        out[k] = v
    if table == "outlets":
        out["hat_rss"] = bool(out.get("hat_rss"))
    return out


# ---------------------------------------------------------------------------
# Keyword suggestions admin page (v0.2.0 F2). Registered BEFORE the
# catch-all ``/{slug}`` handler so FastAPI matches the literal path first.
# ---------------------------------------------------------------------------


@router.get("/suggestions", response_class=HTMLResponse)
def admin_suggestions(request: Request, status: str = Query("pending")):
    """List suggested keywords, grouped by normalized phrase."""
    normalised_status = status if status in {"pending", "approved", "dismissed"} else "pending"
    groups = database.list_keyword_suggestions(status=normalised_status)
    counts = {
        "pending": database.count_keyword_suggestions(status="pending"),
        "approved": database.count_keyword_suggestions(status="approved"),
        "dismissed": database.count_keyword_suggestions(status="dismissed"),
    }
    return templates.TemplateResponse(
        request,
        "suggestions.html",
        {
            "groups": groups,
            "counts": counts,
            "status": normalised_status,
            "all_markets": VALID_MARKETS,
            "all_themes": VALID_THEMES,
        },
    )


@router.post("/suggestions/{suggestion_id}/approve")
async def approve_suggestion(suggestion_id: int, request: Request):
    """Create a keywords row from the suggestion and mark the whole group approved.

    Idempotent: refuses if the group's underlying suggestion is no longer
    pending (already approved / dismissed), so a double-click or two admins
    racing on the same approve button don't produce duplicate keyword rows.
    """
    from fastapi.responses import RedirectResponse

    suggestion = database.suggestion_by_id(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Unknown suggestion")
    if suggestion["status"] != "pending":
        # Silently redirect back — no error, but no duplicate keyword row.
        # The second-clicker sees the fresh list without their action.
        return RedirectResponse(url="/admin/suggestions?status=pending", status_code=303)

    form = await request.form()
    suchbegriff = (form.get("suchbegriff") or suggestion["phrase"]).strip()
    if not suchbegriff:
        raise HTTPException(status_code=400, detail="Empty keyword phrase")

    markets = [str(v) for v in form.getlist("markt") if str(v) in VALID_MARKETS]
    themes = [str(v) for v in form.getlist("themenbereich") if str(v) in VALID_THEMES]
    prioritaet = enums.normalise_priority(form.get("prioritaet") or "High") or "High"
    raw_lang = (form.get("sprache") or suggestion["sprache"] or "").strip().upper() or None
    sprache = raw_lang if raw_lang in set(enums.LANGUAGE_OPTIONS) else None

    # Flip the group status FIRST (single UPDATE that filters status='pending').
    # If it returns 0 rows, someone else already approved/dismissed — bail
    # without creating a keyword row. This makes the SELECT+UPDATE+INSERT
    # sequence race-safe: the UPDATE is the barrier, and rowcount=0 tells us
    # the barrier was crossed by another request.
    n_flipped = database.set_suggestion_group_status(
        suggestion["phrase_normalized"],
        status="approved",
        approved_keyword_id=None,  # patched below once the keyword exists
    )
    if n_flipped == 0:
        return RedirectResponse(url="/admin/suggestions?status=pending", status_code=303)

    new_id = database.insert_config_row(
        "keywords",
        {
            "suchbegriff": suchbegriff,
            "markt": markets,
            "themenbereich": themes,
            "prioritaet": prioritaet,
            "sprache": sprache,
            "notizen": f"Approved from suggestion #{suggestion_id}",
        },
    )
    # Patch approved_keyword_id on the flipped rows — separate step so the
    # race guard above stays a single UPDATE.
    database.attach_keyword_to_approved_group(
        suggestion["phrase_normalized"], keyword_id=new_id
    )
    return RedirectResponse(url="/admin/suggestions?status=pending", status_code=303)


@router.post("/suggestions/{suggestion_id}/dismiss")
def dismiss_suggestion(suggestion_id: int):
    """Dismiss the whole group with the same normalized phrase."""
    suggestion = database.suggestion_by_id(suggestion_id)
    if suggestion is None:
        raise HTTPException(status_code=404, detail="Unknown suggestion")
    n = database.set_suggestion_group_status(
        suggestion["phrase_normalized"], status="dismissed"
    )
    return JSONResponse({"dismissed": n})


@router.post("/suggestions/dismiss-all")
def dismiss_all_suggestions():
    """Bulk-dismiss every pending suggestion. Used when the pending queue
    has ballooned with hundreds of low-signal proposals and the user
    wants a clean slate before the next AI run."""
    n = database.dismiss_all_pending_suggestions()
    return JSONResponse({"dismissed": n})


# ---------------------------------------------------------------------------
# Golden-set admin page (v0.2.0 F1). Registered BEFORE the catch-all
# ``/{slug}`` handler so FastAPI matches the literal path first.
# ---------------------------------------------------------------------------

_GOLDEN_LABELS_UI = frozenset({"relevant", "noise"})


@router.get("/golden", response_class=HTMLResponse)
def admin_golden(request: Request, label: Optional[str] = Query(None)):
    """Golden-set overview: labeled articles + counts."""
    import json as _json

    label_filter = label if label in _GOLDEN_LABELS_UI else None
    rows = database.list_golden_labels(label=label_filter)
    articles = []
    for row in rows:
        item = dict(row)
        # themes_parsed for the template
        raw_themes = item.get("themes")
        themes_list: List[str] = []
        if raw_themes:
            try:
                loaded = _json.loads(raw_themes) if isinstance(raw_themes, str) else raw_themes
                if isinstance(loaded, list):
                    themes_list = [str(t) for t in loaded]
            except (TypeError, ValueError, _json.JSONDecodeError):
                pass
        item["themes_parsed"] = themes_list
        item["final_score"] = (
            (item.get("relevance_score") or 0)
            + (item.get("trust_bonus") or 0)
            + (item.get("feedback_bonus") or 0)
        )
        articles.append(item)
    return templates.TemplateResponse(
        request,
        "golden.html",
        {
            "articles": articles,
            "counts": {
                "total": database.count_golden_labels(),
                "relevant": database.count_golden_labels(label="relevant"),
                "noise": database.count_golden_labels(label="noise"),
            },
            "filter_label": label_filter,
        },
    )


@router.get("/{slug}", response_class=HTMLResponse)
def list_view(request: Request, slug: str, market: str = Query("")):
    table = _resolve(slug)
    rows = database.list_config(table)
    # ?market=<Market> filter: rows whose markt contains the market OR is
    # empty (empty markt = global). Unknown values are ignored (no filter).
    selected_market = market if market in VALID_MARKETS else ""
    if selected_market:
        rows = [
            r for r in rows
            if local_config.markt_applies(r.get("markt") or [], [selected_market])
        ]
    return templates.TemplateResponse(
        request,
        "admin_list.html",
        {
            "slug": slug,
            "table": table,
            "meta": TABLE_META[table],
            "rows": rows,
            "all_markets": VALID_MARKETS,
            "selected_market": selected_market,
        },
    )


@router.get("/{slug}/new", response_class=HTMLResponse)
def new_form(request: Request, slug: str):
    table = _resolve(slug)
    meta = TABLE_META[table]
    return templates.TemplateResponse(
        request,
        "admin_edit.html",
        {
            "slug": slug,
            "table": table,
            "meta": meta,
            "row": None,
            "row_id": None,
            "form_value_for": _form_value_for,
            "post_url": f"/admin/{slug}/new",
            "title": f"New {meta['singular']}",
        },
    )


@router.post("/{slug}/new")
async def create(slug: str, request: Request):
    table = _resolve(slug)
    form = await request.form()
    # Convert MultiDict to a dict that preserves multi-value entries as lists.
    payload_raw: Dict[str, Any] = {}
    for key in form.keys():
        values = form.getlist(key)
        payload_raw[key] = values if len(values) > 1 else (values[0] if values else "")
    data = _form_to_payload(table, payload_raw)
    database.insert_config_row(table, data)
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)


@router.get("/{slug}/{row_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, slug: str, row_id: int):
    table = _resolve(slug)
    rows = database.list_config(table)
    row = next((r for r in rows if r["id"] == row_id), None)
    if not row:
        raise HTTPException(status_code=404, detail="Row not found")
    meta = TABLE_META[table]
    return templates.TemplateResponse(
        request,
        "admin_edit.html",
        {
            "slug": slug,
            "table": table,
            "meta": meta,
            "row": row,
            "row_id": row_id,
            "form_value_for": _form_value_for,
            "post_url": f"/admin/{slug}/{row_id}/edit",
            "title": f"Edit {meta['singular']}",
        },
    )


@router.post("/{slug}/{row_id}/edit")
async def update(slug: str, row_id: int, request: Request):
    table = _resolve(slug)
    form = await request.form()
    payload_raw: Dict[str, Any] = {}
    for key in form.keys():
        values = form.getlist(key)
        payload_raw[key] = values if len(values) > 1 else (values[0] if values else "")
    data = _form_to_payload(table, payload_raw)
    database.update_config_row(table, row_id, data)
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)


@router.post("/{slug}/{row_id}/delete")
def delete(slug: str, row_id: int):
    table = _resolve(slug)
    database.delete_config_row(table, row_id)
    return RedirectResponse(url=f"/admin/{slug}", status_code=303)

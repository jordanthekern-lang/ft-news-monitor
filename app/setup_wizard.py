"""First-run setup wizard (Task 4).

Mounted at ``/setup``. Walks the operator through:

1. Home markets   → ``runtime_settings.home_markets``
2. AI credentials → ``.env`` (only the provided keys, via
   :func:`config.write_env_values` + :func:`config.reload_env`)
3. Test AI connection (uses :func:`providers.test_connection`)
4. Import seed packs (:func:`seed_import.import_seed_file`, ``mode="merge"``)
5. Schedule daily run → ``runtime_settings.scheduler_enabled`` +
   ``scheduler_time`` (engine wired in a later task).
6. Finish → sets ``runtime_settings.setup_completed = True`` and redirects
   to ``/``.

Security invariants:

* Submitted secret values are NEVER echoed back into any response. The UI
  only shows a boolean "configured" state per env var.
* Nothing that came from a password field lives in the database — secrets
  stay in ``.env``.
* ``.env`` writes always go through :func:`config.write_env_values` so
  unrelated lines and comments are preserved.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config as _config
from . import outlook_poller, providers, runtime_settings, scheduler, seed_import
from .config import (
    PROJECT_ROOT,
    TEMPLATES_DIR,
    VALID_MARKETS,
    get_settings,
)


router = APIRouter(prefix="/setup")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Same HH:MM regex the /settings form uses; keep them consistent.
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# .env keys the wizard is allowed to write. Anything else is ignored.
_WIZARD_ENV_KEYS: tuple = (
    "AI_PROVIDER",
    "AWS_PROFILE",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_REGION",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_API_KEY",
    # v0.6.1 — timezone (derived from home_markets; user can override).
    "TIMEZONE",
    # v0.3.0 — newsletter ingest via Outlook-COM (replaces v0.2.0 IMAP).
    "OUTLOOK_ENABLED",
    "OUTLOOK_FOLDER_PATH",
    "OUTLOOK_STORE_NAME",
    "OUTLOOK_MAX_AGE_DAYS",
    "OUTLOOK_MAX_LINKS_PER_EMAIL",
)

# v0.6.1 M11: default timezone per market. When home_markets is set at
# the wizard step, we auto-fill TIMEZONE from the first match here.
# Users can override in the /settings page later.
_MARKET_TIMEZONE_DEFAULT = {
    "DACH": "Europe/Berlin",
    "EU": "Europe/Brussels",
    "Italy": "Europe/Rome",
    "France": "Europe/Paris",
    "Benelux": "Europe/Brussels",
    "Portugal": "Europe/Lisbon",
    "Nordics": "Europe/Stockholm",
}

# Keys treated as secrets — status is boolean ("configured" or not),
# submitted values are NEVER echoed back into any response.
_SECRET_ENV_KEYS: frozenset = frozenset(
    {
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "ANTHROPIC_API_KEY",
    }
)

_REGION_CHOICES: tuple = (
    "eu-west-1",
    "eu-central-1",
    "eu-north-1",
    "eu-west-3",
)


def _seeds_dir() -> Path:
    return PROJECT_ROOT / "seeds"


def _seed_available(market: str) -> bool:
    """A market has a wizard-importable seed iff ``seeds/<lower>.json`` exists.

    The underscore-prefixed ``_pending_research.json`` file is NOT a market
    pack — it holds legacy DACH/EU rows and stays invisible to the wizard.
    We reach into the fixed :data:`VALID_MARKETS` list rather than globbing
    the seeds directory, which makes ``_pending_research.json`` unreachable
    by construction.
    """
    return (_seeds_dir() / f"{market.lower()}.json").exists()


def _current_state() -> dict:
    """Snapshot the current wizard state for template rendering.

    ``env_configured`` reports booleans only — never the values.
    """
    settings = get_settings()
    home_markets = runtime_settings.get(runtime_settings.HOME_MARKETS) or []
    if not isinstance(home_markets, list):
        home_markets = []

    env_configured = {
        "AI_PROVIDER": bool(settings.ai_provider),
        "AWS_PROFILE": bool(settings.aws_profile),
        "AWS_ACCESS_KEY_ID": bool(settings.aws_access_key_id),
        "AWS_SECRET_ACCESS_KEY": bool(settings.aws_secret_access_key),
        "AWS_REGION": bool(settings.aws_region),
        "ANTHROPIC_MODEL": bool(settings.anthropic_model),
        "ANTHROPIC_API_KEY": bool(settings.anthropic_api_key),
    }

    # An AWS auth path is "ready" when either a profile is set OR both keys
    # are set. Anthropic direct mode needs ANTHROPIC_API_KEY instead.
    # Cowork mode delegates scoring to an external Cowork session — no
    # local credentials are needed, so the step is always ready.
    ai_ready = False
    if settings.ai_provider == "cowork":
        ai_ready = True
    elif settings.ai_provider == "anthropic":
        ai_ready = bool(settings.anthropic_api_key)
    else:  # bedrock (default)
        ai_ready = bool(settings.aws_profile) or (
            bool(settings.aws_access_key_id)
            and bool(settings.aws_secret_access_key)
        )

    scheduler_enabled = bool(runtime_settings.get(runtime_settings.SCHEDULER_ENABLED))
    scheduler_time = runtime_settings.get(runtime_settings.SCHEDULER_TIME) or "06:00"

    # v0.3.0 — Outlook-COM newsletter ingest block. All non-secret; uses
    # the locally running Outlook desktop app for auth (no separate password).
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

    return {
        "home_markets": home_markets,
        "env_configured": env_configured,
        "ai_provider": settings.ai_provider or "bedrock",
        "aws_region": settings.aws_region or "eu-west-1",
        "anthropic_model": settings.anthropic_model,
        "auth_mode": "profile" if settings.aws_profile else "keys",
        "ai_ready": ai_ready,
        "scheduler_enabled": scheduler_enabled,
        "scheduler_time": scheduler_time,
        "outlook": outlook_state,
        "setup_completed": bool(
            runtime_settings.get(runtime_settings.SETUP_COMPLETED)
        ),
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
def wizard_home(request: Request):
    state = _current_state()
    # Preselect EU + stored home markets for the seed-import step.
    seed_preselect = set(state["home_markets"]) | {"EU"}
    seed_markets = [
        {
            "name": m,
            "available": _seed_available(m),
            "preselected": m in seed_preselect,
        }
        for m in VALID_MARKETS
    ]
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "state": state,
            "all_markets": VALID_MARKETS,
            "regions": _REGION_CHOICES,
            "seed_markets": seed_markets,
        },
    )


@router.post("/markets")
async def post_markets(request: Request):
    form = await request.form()
    submitted = [str(v) for v in form.getlist("home_markets")]
    if not submitted:
        raise HTTPException(
            status_code=400,
            detail="Pick at least one home market.",
        )
    invalid = [m for m in submitted if m not in VALID_MARKETS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid market(s): {', '.join(invalid)}",
        )
    # Preserve submission order but de-dupe.
    seen: list = []
    for m in submitted:
        if m not in seen:
            seen.append(m)
    runtime_settings.set(runtime_settings.HOME_MARKETS, seen)

    # v0.6.1 M11: auto-set TIMEZONE from the first market if the user
    # hasn't set it manually yet. Users can override in /settings later.
    # Only writes when the current TIMEZONE is missing or matches the
    # default (Europe/Berlin) — otherwise their explicit choice wins.
    import os as _os
    current_tz = _os.environ.get("TIMEZONE", "").strip()
    if current_tz in ("", "Europe/Berlin"):
        for m in seen:
            tz_guess = _MARKET_TIMEZONE_DEFAULT.get(m)
            if tz_guess:
                try:
                    _config.write_env_values({"TIMEZONE": tz_guess})
                    _config.reload_env()
                except Exception:  # pragma: no cover - defensive
                    pass
                break

    return RedirectResponse(url="/setup#step-ai", status_code=303)


@router.post("/aws")
async def post_aws(request: Request):
    form = await request.form()
    auth_mode = str(form.get("auth_mode") or "keys").strip().lower()
    provider = str(form.get("ai_provider") or "bedrock").strip().lower()
    if provider not in ("bedrock", "anthropic", "cowork"):
        raise HTTPException(status_code=400, detail="Invalid AI provider.")

    region = str(form.get("aws_region") or "").strip()
    if region and region not in _REGION_CHOICES:
        raise HTTPException(status_code=400, detail="Invalid AWS region.")

    model = str(form.get("anthropic_model") or "").strip()

    to_write: dict = {"AI_PROVIDER": provider}

    if auth_mode == "profile":
        profile = str(form.get("aws_profile") or "").strip()
        if profile:
            to_write["AWS_PROFILE"] = profile
    elif auth_mode == "keys":
        access_key = str(form.get("aws_access_key_id") or "").strip()
        secret_key = str(form.get("aws_secret_access_key") or "").strip()
        if access_key:
            to_write["AWS_ACCESS_KEY_ID"] = access_key
        if secret_key:
            to_write["AWS_SECRET_ACCESS_KEY"] = secret_key
    else:
        raise HTTPException(status_code=400, detail="Invalid auth mode.")

    if region:
        to_write["AWS_REGION"] = region
    if model:
        to_write["ANTHROPIC_MODEL"] = model

    # Anthropic API key only relevant to the direct provider. Written
    # unconditionally when provided so users can pre-seed it before switching
    # providers, but never echoed back.
    anthropic_key = str(form.get("anthropic_api_key") or "").strip()
    if anthropic_key:
        to_write["ANTHROPIC_API_KEY"] = anthropic_key

    # Guard: only known keys ever land in the write set (defence in depth).
    to_write = {k: v for k, v in to_write.items() if k in _WIZARD_ENV_KEYS}

    _config.write_env_values(to_write)
    _config.reload_env()
    return RedirectResponse(url="/setup#step-ai", status_code=303)


@router.post("/test-ai")
async def post_test_ai():
    result = await providers.test_connection(get_settings())
    # test_connection already returns a plain dict {ok, model, latency_ms, error}.
    return JSONResponse(result)


@router.post("/test-outlook")
async def post_test_outlook(request: Request):
    """v0.3.1 — dry-run the Outlook COM connection using the submitted form
    values (NOT the saved ``.env``), so the user can validate a new folder
    path before committing it. Read-only: never inserts, moves, or modifies
    mail items.

    Response shape mirrors ``providers.test_connection`` so the JS badge
    logic is reused: ``{ok, error, latency_ms, folder_name?, item_count?}``.
    """
    form = await request.form()
    cfg = outlook_poller.OutlookConfig(
        folder_path=str(form.get("outlook_folder_path") or "").strip(),
        store_name=str(form.get("outlook_store_name") or "").strip(),
    )
    result = await outlook_poller.test_connection(cfg)
    return JSONResponse(result)


@router.post("/import-seeds")
async def post_import_seeds(request: Request):
    form = await request.form()
    submitted = [str(v) for v in form.getlist("markets")]
    invalid = [m for m in submitted if m not in VALID_MARKETS]
    if invalid:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid market(s): {', '.join(invalid)}",
        )

    per_market: list = []
    for market in submitted:
        seed_path = _seeds_dir() / f"{market.lower()}.json"
        if not seed_path.exists():
            per_market.append({"market": market, "status": "not available yet"})
            continue
        report = seed_import.import_seed_file(seed_path, mode="merge")
        # v0.4.0 — bump the imported_seed_versions marker so the startup
        # auto-reseeder doesn't redundantly re-merge the same file on the
        # very next launch. mark_seed_imported reads the version out of the
        # file itself (missing → no-op).
        try:
            shipped_version = seed_import._read_seed_version(seed_path)
            seed_import.mark_seed_imported(market, shipped_version)
        except Exception:  # pragma: no cover - defensive
            pass
        per_market.append(
            {
                "market": market,
                "status": "ok",
                "report": report,
            }
        )

    state = _current_state()
    seed_preselect = set(state["home_markets"]) | {"EU"}
    seed_markets = [
        {
            "name": m,
            "available": _seed_available(m),
            "preselected": m in seed_preselect,
        }
        for m in VALID_MARKETS
    ]
    return templates.TemplateResponse(
        request,
        "setup.html",
        {
            "state": state,
            "all_markets": VALID_MARKETS,
            "regions": _REGION_CHOICES,
            "seed_markets": seed_markets,
            "seed_result": {
                "requested": submitted,
                "per_market": per_market,
            },
        },
    )


@router.post("/schedule")
async def post_schedule(request: Request):
    form = await request.form()
    enabled = bool(form.get("scheduler_enabled"))
    time_value = str(form.get("scheduler_time") or "").strip()
    if not _TIME_RE.match(time_value):
        raise HTTPException(
            status_code=400,
            detail="Scheduler time must be in HH:MM format (e.g. 06:30).",
        )
    runtime_settings.set(runtime_settings.SCHEDULER_ENABLED, enabled)
    runtime_settings.set(runtime_settings.SCHEDULER_TIME, time_value)
    # Take effect immediately: if the scheduler is already running, swap
    # the daily job in place. If it's not (fresh install), the lifespan
    # startup will pick up the new settings the next time the app boots.
    scheduler.reload()
    # Land on step 6 (Outlook), not the Finish card — otherwise first-time
    # operators scroll past the newsletter step entirely.
    return RedirectResponse(url="/setup#step-outlook", status_code=303)


@router.post("/outlook")
async def post_outlook(request: Request):
    """v0.3.0 — optional newsletter ingest via the locally running Outlook
    desktop app (COM automation, no passwords, no OAuth).

    Save semantics:

    * Enable OFF + any populated field ⇒ 400 (user likely forgot the
      checkbox; refuse to silently drop input).
    * Enable OFF + every field blank ⇒ explicit disable path, clears the
      ``OUTLOOK_*`` keys.
    * Enable ON but ``folder_path`` empty ⇒ 400 (no default is applied —
      an empty path would risk reading the entire mailbox once
      trust-folder-style semantics were assumed; we refuse instead).
    * Enable ON with folder_path ⇒ persist values.
    """
    form = await request.form()
    enabled = bool(form.get("outlook_enabled"))

    populated = any(
        str(form.get(k) or "").strip()
        for k in (
            "outlook_folder_path", "outlook_store_name",
            "outlook_max_age_days", "outlook_max_links_per_email",
        )
    )

    if not enabled and populated:
        raise HTTPException(
            status_code=400,
            detail=(
                "You filled in Outlook fields but didn't tick "
                "'Enable Outlook newsletter ingest'. Nothing was saved. "
                "Tick the checkbox and save again, or clear all fields "
                "first if you really want to disable Outlook ingest."
            ),
        )

    to_write: dict = {"OUTLOOK_ENABLED": "true" if enabled else "false"}

    if not enabled:
        # Explicit disable — clear values so a stale folder-path or
        # store-name doesn't quietly re-activate if the operator later
        # flips the flag.
        for key in ("OUTLOOK_FOLDER_PATH", "OUTLOOK_STORE_NAME",
                    "OUTLOOK_MAX_AGE_DAYS", "OUTLOOK_MAX_LINKS_PER_EMAIL"):
            to_write[key] = ""

    if enabled:
        folder_path = str(form.get("outlook_folder_path") or "").strip()
        if not folder_path:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Please specify a folder path (e.g. 'Posteingang/BT Verkehr'). "
                    "No default is applied — the tool only reads the folder "
                    "you name here."
                ),
            )
        to_write["OUTLOOK_FOLDER_PATH"] = folder_path

        store_name = str(form.get("outlook_store_name") or "").strip()
        to_write["OUTLOOK_STORE_NAME"] = store_name  # empty = default store

        max_age = str(form.get("outlook_max_age_days") or "").strip()
        if max_age:
            try:
                n = int(max_age)
                if 1 <= n <= 90:
                    to_write["OUTLOOK_MAX_AGE_DAYS"] = str(n)
            except ValueError:
                pass

        max_links = str(form.get("outlook_max_links_per_email") or "").strip()
        if max_links:
            try:
                n = int(max_links)
                if 1 <= n <= 200:
                    to_write["OUTLOOK_MAX_LINKS_PER_EMAIL"] = str(n)
            except ValueError:
                pass

    to_write = {k: v for k, v in to_write.items() if k in _WIZARD_ENV_KEYS}
    _config.write_env_values(to_write)
    _config.reload_env()
    # A hidden "return_to" field lets the Settings page reuse this exact
    # route without landing the operator back in the wizard. Only allow-list
    # a fixed set of destinations to avoid an open-redirect.
    return_to = str(form.get("return_to") or "").strip()
    if return_to == "settings":
        return RedirectResponse(url="/settings?saved=1", status_code=303)
    return RedirectResponse(url="/setup#step-finish", status_code=303)


@router.post("/finish")
async def post_finish():
    home_markets = runtime_settings.get(runtime_settings.HOME_MARKETS) or []
    if not isinstance(home_markets, list) or not home_markets:
        raise HTTPException(
            status_code=400,
            detail="Pick at least one home market before finishing setup.",
        )
    runtime_settings.set(runtime_settings.SETUP_COMPLETED, True)
    return RedirectResponse(url="/", status_code=303)

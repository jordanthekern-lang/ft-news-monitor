"""File-bridge fallback for Cowork ranking.

Purpose: when the Cowork sandbox cannot reach the local dashboard HTTP
server on ``127.0.0.1:8766`` (VM loopback != Windows-host loopback), the
same request/response envelopes flow through JSON files inside the
tool's ``data/`` directory. Cowork mounts ``data/`` as a Plan-9 share
and reads/writes files there; a background asyncio task in the app
watches ``data/cowork_inbox/`` for requests and drops responses into
``data/cowork_outbox/``.

Envelope shape (JSON):

  Request  (data/cowork_inbox/<uuid>.json):
      {
        "id": "<uuid>",
        "action": "list_pending" | "submit_scores" | "health",
        "payload": { ... action-specific ... }
      }
  Response (data/cowork_outbox/<uuid>.json):
      {
        "id": "<uuid>",
        "status": "ok" | "error",
        "result": { ... }  # or  "error": "..."
      }

Processed inbox files are moved to ``data/cowork_archive/<yyyymmdd>/``
so the same request is never processed twice. Malformed JSON files are
moved to ``data/cowork_archive/malformed/`` with an accompanying
``.error.txt`` describing the failure.

Only starts when ``AI_PROVIDER=cowork`` — no cost in bedrock/anthropic
mode.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from . import database, pipeline, progress
from .config import DATA_DIR, normalise_category, normalise_theme

logger = logging.getLogger(__name__)


INBOX_DIR = DATA_DIR / "cowork_inbox"
OUTBOX_DIR = DATA_DIR / "cowork_outbox"
ARCHIVE_DIR = DATA_DIR / "cowork_archive"

# Poll cadence for the watcher. Kept short — the skill polls the outbox
# at its own cadence so anything longer than a couple of seconds adds
# perceptible latency to every round-trip.
_POLL_INTERVAL_SECONDS = 2.0

# Outbox files are cleaned up by the watcher itself once they age out.
# The Cowork skill sometimes can't delete them (plan-9 permission quirk)
# so we don't rely on Cowork to tidy up.
_OUTBOX_MAX_AGE_SECONDS = 3600.0

# Same required fields as POST /api/articles/scores.
_SCORE_REQUIRED = ("id", "relevance_score", "category", "themes", "reasoning")

# Same projection as GET /api/articles/pending.
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


def ensure_dirs() -> None:
    for path in (INBOX_DIR, OUTBOX_DIR, ARCHIVE_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _today_archive() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    dest = ARCHIVE_DIR / stamp
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _write_response(request_id: str, body: Dict[str, Any]) -> None:
    """Atomic write: tmp file in outbox + os.replace so the Cowork side
    never sees a partial file."""
    OUTBOX_DIR.mkdir(parents=True, exist_ok=True)
    dest = OUTBOX_DIR / f"{request_id}.json"
    tmp = OUTBOX_DIR / f".{request_id}.{uuid.uuid4().hex}.tmp"
    payload = {"id": request_id, **body}
    tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    tmp.replace(dest)


def _archive_inbox(path: Path, *, malformed: bool = False) -> None:
    dest_dir = ARCHIVE_DIR / "malformed" if malformed else _today_archive()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    try:
        path.replace(dest)
    except OSError:
        logger.exception("cowork_bridge: failed to archive %s", path)


async def handle_list_pending(payload: Dict[str, Any]) -> Dict[str, Any]:
    limit = int(payload.get("limit") or 200)
    limit = max(1, min(1000, limit))
    from .config import VALID_CATEGORIES, VALID_THEMES

    rows = database.fetch_articles_for_ai()[:limit]
    articles = []
    for row in rows:
        item: Dict[str, Any] = {}
        keys = row.keys()
        for field in _PENDING_FIELDS:
            item[field] = row[field] if field in keys else None
        articles.append(item)
    return {
        "articles": articles,
        "total": len(articles),
        "categories": VALID_CATEGORIES,
        "themes": VALID_THEMES,
    }


async def handle_submit_scores(payload: Dict[str, Any]) -> Dict[str, Any]:
    scores = payload.get("scores")
    if not isinstance(scores, list):
        raise ValueError("payload.scores must be a list")

    updated = 0
    skipped = 0
    errors = []
    for idx, entry in enumerate(scores):
        if not isinstance(entry, dict):
            errors.append({"index": idx, "error": "entry must be an object"})
            continue
        missing = [k for k in _SCORE_REQUIRED if k not in entry]
        if missing:
            errors.append(
                {"index": idx, "error": f"missing fields: {', '.join(missing)}"}
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

    return {"updated": updated, "skipped": skipped, "errors": errors}


async def handle_health(_payload: Dict[str, Any]) -> Dict[str, Any]:
    pending = database.fetch_articles_for_ai()
    return {"status": "ok", "pending_count": len(pending)}


async def handle_pipeline_status(_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return the same shape as GET /pipeline-status.

    Skill uses this to know when a running scrape is done — the loop
    stays inside the file bridge so no HTTP round-trip is needed.
    """
    return progress.snapshot()


async def handle_run_collection(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Trigger a full pipeline run in the background.

    Returns immediately with ``{"status": "started"}`` (or
    ``{"status": "already_running"}`` when a run is in flight). The
    caller must poll ``pipeline_status`` to know when it's done —
    exactly the same protocol the HTTP endpoints use, so the skill's
    loop shape doesn't change.

    Payload options (all optional):
        - ``dry_run`` (bool, default False)
        - ``markets`` (list of strings, default all home markets)
    """
    if pipeline._run_lock.locked():
        return {"status": "already_running"}

    dry_run = bool(payload.get("dry_run") or False)
    markets = payload.get("markets")
    if markets is not None and not isinstance(markets, list):
        raise ValueError("payload.markets must be a list or omitted")

    async def _fire_and_forget():
        try:
            await pipeline.run_pipeline(markets=markets, dry_run=dry_run)
        except Exception:  # pragma: no cover - defensive
            logger.exception("cowork_bridge: background pipeline run failed")

    asyncio.create_task(_fire_and_forget(), name="cowork_bridge_scrape")
    return {"status": "started"}


_ACTIONS = {
    "list_pending": handle_list_pending,
    "submit_scores": handle_submit_scores,
    "health": handle_health,
    "pipeline_status": handle_pipeline_status,
    "run_collection": handle_run_collection,
}


async def process_request(request: Dict[str, Any]) -> Dict[str, Any]:
    """Dispatch a single envelope. Returns the response body without
    the request id (added by _write_response)."""
    action = str(request.get("action") or "").strip()
    handler = _ACTIONS.get(action)
    if handler is None:
        return {"status": "error", "error": f"Unknown action: {action!r}"}
    try:
        result = await handler(request.get("payload") or {})
        return {"status": "ok", "result": result}
    except Exception as exc:  # noqa: BLE001 — surface to Cowork
        logger.exception("cowork_bridge: handler %s failed", action)
        return {"status": "error", "error": str(exc)}


async def process_file(path: Path) -> Optional[str]:
    """Process one inbox file. Returns the request id on success or None
    when the file was malformed. Always archives the file before
    returning so it isn't re-processed."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("cowork_bridge: cannot read %s: %s", path, exc)
        _archive_inbox(path, malformed=True)
        return None
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("cowork_bridge: malformed JSON in %s: %s", path, exc)
        (ARCHIVE_DIR / "malformed").mkdir(parents=True, exist_ok=True)
        _archive_inbox(path, malformed=True)
        return None
    request_id = str(envelope.get("id") or uuid.uuid4().hex)
    body = await process_request(envelope)
    _write_response(request_id, body)
    _archive_inbox(path)
    return request_id


def _sweep_stale_outbox(now_ts: float) -> None:
    """Delete outbox response files older than _OUTBOX_MAX_AGE_SECONDS.

    Cowork's Linux VM sometimes can't unlink files from the plan-9 share
    (EPERM). We can — this side has full ownership — so the watcher
    tidies up on Cowork's behalf. Absent this, an unbounded number of
    JSON responses accumulate on disk.
    """
    try:
        entries = list(OUTBOX_DIR.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.is_file():
            continue
        try:
            age = now_ts - entry.stat().st_mtime
        except OSError:
            continue
        if age > _OUTBOX_MAX_AGE_SECONDS:
            try:
                entry.unlink()
            except OSError:
                pass


async def _watch_loop(stop_event: asyncio.Event) -> None:
    logger.info(
        "cowork_bridge: watching %s (interval=%ss)",
        INBOX_DIR,
        _POLL_INTERVAL_SECONDS,
    )
    import time as _time

    sweep_counter = 0
    while not stop_event.is_set():
        ensure_dirs()
        try:
            for path in sorted(INBOX_DIR.glob("*.json")):
                if stop_event.is_set():
                    break
                if path.name.startswith("."):
                    continue
                await process_file(path)
        except Exception:  # pragma: no cover - defensive
            logger.exception("cowork_bridge: watch loop iteration failed")

        # Sweep stale outbox files roughly every minute (30 * 2s poll).
        sweep_counter += 1
        if sweep_counter >= 30:
            sweep_counter = 0
            _sweep_stale_outbox(_time.time())

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=_POLL_INTERVAL_SECONDS)
        except asyncio.TimeoutError:
            pass


_STOP_EVENT: Optional[asyncio.Event] = None
_TASK: Optional[asyncio.Task] = None


def start() -> None:
    """Start the file-bridge watcher. Safe to call more than once — the
    second call is a no-op if a task is already running."""
    global _STOP_EVENT, _TASK
    if _TASK is not None and not _TASK.done():
        return
    ensure_dirs()
    _STOP_EVENT = asyncio.Event()
    _TASK = asyncio.create_task(_watch_loop(_STOP_EVENT), name="cowork_bridge")


async def stop() -> None:
    global _STOP_EVENT, _TASK
    if _STOP_EVENT is not None:
        _STOP_EVENT.set()
    if _TASK is not None:
        try:
            await asyncio.wait_for(_TASK, timeout=2.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    _STOP_EVENT = None
    _TASK = None

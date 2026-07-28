"""Tests for the v0.7.6 Cowork file-bridge fallback (app/cowork_bridge.py).

The bridge is only exercised via ``process_file`` synchronously here;
the asyncio watch loop is not started because tests should not depend
on wall-clock polling.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest


@pytest.fixture()
def bridge(isolated_db):
    """Reload the bridge module so it picks up the isolated DATA_DIR
    monkeypatch from ``conftest.isolated_db``."""
    from app import cowork_bridge

    importlib.reload(cowork_bridge)
    cowork_bridge.ensure_dirs()
    return cowork_bridge


def _insert_article(slug: str) -> int:
    from app import database

    article_id = database.insert_article(
        url=f"https://example.test/{slug}",
        title=f"Article {slug}",
        source_name="Test",
        source_type="feed",
        country="DACH",
        published_at=datetime.now(timezone.utc),
        snippet="short",
        raw_content=None,
        language="de",
    )
    assert article_id is not None
    return article_id


def _write_inbox(bridge, envelope: dict) -> Path:
    """Write a request JSON to inbox and return the path."""
    request_id = envelope.get("id", "req-1")
    path = bridge.INBOX_DIR / f"{request_id}.json"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    return path


def _read_outbox(bridge, request_id: str) -> dict:
    path = bridge.OUTBOX_DIR / f"{request_id}.json"
    assert path.exists(), f"Expected outbox file for {request_id}"
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# process_file
# ---------------------------------------------------------------------------


def test_health_action_returns_pending_count(bridge):
    _insert_article("a1")
    _insert_article("a2")
    path = _write_inbox(bridge, {"id": "h1", "action": "health", "payload": {}})
    result_id = asyncio.run(bridge.process_file(path))
    assert result_id == "h1"

    response = _read_outbox(bridge, "h1")
    assert response["status"] == "ok"
    assert response["result"]["pending_count"] >= 2


def test_pipeline_status_action_returns_snapshot(bridge):
    path = _write_inbox(
        bridge, {"id": "ps1", "action": "pipeline_status", "payload": {}}
    )
    asyncio.run(bridge.process_file(path))
    response = _read_outbox(bridge, "ps1")
    assert response["status"] == "ok"
    assert "phase" in response["result"]


def test_run_collection_action_starts_or_reports_already_running(bridge, monkeypatch):
    """We don't want to actually run the pipeline in tests — mock
    ``pipeline.run_pipeline`` to a no-op coroutine."""
    from app import pipeline as _pipeline

    async def _noop(**_kwargs):
        return {"status": "completed"}

    monkeypatch.setattr(_pipeline, "run_pipeline", _noop)

    path = _write_inbox(
        bridge, {"id": "rc1", "action": "run_collection", "payload": {}}
    )
    asyncio.run(bridge.process_file(path))
    response = _read_outbox(bridge, "rc1")
    assert response["status"] == "ok"
    assert response["result"]["status"] in {"started", "already_running"}


def test_sweep_stale_outbox_removes_old_files(bridge):
    """The watcher tidies outbox files Cowork can't unlink itself."""
    import os
    import time

    stale = bridge.OUTBOX_DIR / "old.json"
    stale.write_text('{"id":"x"}', encoding="utf-8")
    ancient = time.time() - bridge._OUTBOX_MAX_AGE_SECONDS - 60
    os.utime(stale, (ancient, ancient))

    fresh = bridge.OUTBOX_DIR / "new.json"
    fresh.write_text('{"id":"y"}', encoding="utf-8")

    bridge._sweep_stale_outbox(time.time())
    assert not stale.exists()
    assert fresh.exists()


def test_list_pending_returns_articles(bridge):
    _insert_article("a1")
    _insert_article("a2")
    path = _write_inbox(
        bridge, {"id": "lp1", "action": "list_pending", "payload": {"limit": 10}}
    )
    asyncio.run(bridge.process_file(path))
    response = _read_outbox(bridge, "lp1")
    assert response["status"] == "ok"
    assert response["result"]["total"] >= 2
    first = response["result"]["articles"][0]
    for field in ("id", "title", "url", "source_name", "country", "language"):
        assert field in first


def test_submit_scores_updates_db(bridge):
    article_id = _insert_article("a1")
    path = _write_inbox(
        bridge,
        {
            "id": "sub1",
            "action": "submit_scores",
            "payload": {
                "scores": [
                    {
                        "id": article_id,
                        "relevance_score": 7,
                        "category": "Vehicle funding",
                        "themes": ["E-buses"],
                        "reasoning": "test",
                    }
                ]
            },
        },
    )
    asyncio.run(bridge.process_file(path))
    response = _read_outbox(bridge, "sub1")
    assert response["status"] == "ok"
    assert response["result"]["updated"] == 1
    from app import database

    row = database.article_by_id(article_id)
    assert row["relevance_score"] == 7
    assert row["ai_filtered"] == 1


def test_submit_scores_reports_field_errors(bridge):
    article_id = _insert_article("a1")
    path = _write_inbox(
        bridge,
        {
            "id": "err1",
            "action": "submit_scores",
            "payload": {
                "scores": [
                    {
                        "id": article_id,
                        # Missing all required fields except id
                    }
                ]
            },
        },
    )
    asyncio.run(bridge.process_file(path))
    response = _read_outbox(bridge, "err1")
    assert response["status"] == "ok"  # status of the envelope, not the entry
    assert response["result"]["updated"] == 0
    assert response["result"]["errors"], "Expected an error entry"


def test_unknown_action_returns_error(bridge):
    path = _write_inbox(
        bridge, {"id": "u1", "action": "wtf", "payload": {}}
    )
    asyncio.run(bridge.process_file(path))
    response = _read_outbox(bridge, "u1")
    assert response["status"] == "error"
    assert "wtf" in response["error"]


def test_processed_file_moves_to_archive(bridge):
    path = _write_inbox(bridge, {"id": "arc1", "action": "health", "payload": {}})
    assert path.exists()
    asyncio.run(bridge.process_file(path))
    assert not path.exists(), "Inbox file should have moved to archive"
    # Archive dir contains the file under a dated subfolder.
    archived = list(bridge.ARCHIVE_DIR.rglob("arc1.json"))
    assert archived, "Processed file must land in the archive"


def test_malformed_json_moves_to_malformed_dir(bridge):
    bad = bridge.INBOX_DIR / "broken.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    result_id = asyncio.run(bridge.process_file(bad))
    assert result_id is None
    assert not bad.exists()
    malformed = list((bridge.ARCHIVE_DIR / "malformed").rglob("broken.json"))
    assert malformed


def test_health_action_response_carries_request_id(bridge):
    path = _write_inbox(bridge, {"id": "req-42", "action": "health", "payload": {}})
    asyncio.run(bridge.process_file(path))
    body = _read_outbox(bridge, "req-42")
    assert body["id"] == "req-42"


# ---------------------------------------------------------------------------
# start()/stop() lifecycle
# ---------------------------------------------------------------------------


def test_start_stop_lifecycle(bridge):
    async def _run():
        bridge.start()
        # Give the loop a tick to schedule the task.
        await asyncio.sleep(0)
        await bridge.stop()

    asyncio.run(_run())

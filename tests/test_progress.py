"""Tests for the v0.7.4 pipeline-progress reporter + endpoint."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import progress
from app.main import app


@pytest.fixture()
def client():
    return TestClient(app)


@pytest.fixture(autouse=True)
def _reset_progress():
    """Restore idle state around every test so ordering doesn't matter —
    reset both before and after because a previous test module might
    have left non-idle state on the module-level singleton."""
    progress._state.__init__()
    yield
    progress._state.__init__()


def test_snapshot_idle_by_default():
    snap = progress.snapshot()
    assert snap["phase"] == "idle"
    assert snap["running"] is False
    assert snap["elapsed_seconds"] == 0


def test_start_run_flips_phase_to_collecting():
    progress.start_run(markets=["DACH", "EU"])
    snap = progress.snapshot()
    assert snap["phase"] == "collecting"
    assert snap["running"] is True
    assert snap["markets"] == ["DACH", "EU"]


def test_set_phase_and_step_update_snapshot():
    progress.start_run()
    progress.set_phase("ai_scoring", step="AI relevance scoring", current=3, total=87)
    snap = progress.snapshot()
    assert snap["phase"] == "ai_scoring"
    assert snap["step"] == "AI relevance scoring"
    assert snap["current"] == 3
    assert snap["total"] == 87


def test_set_step_preserves_phase():
    progress.start_run()
    progress.set_phase("collecting", step="Feeds 1/10", current=0, total=10)
    progress.set_step("Keywords 5/134", current=5, total=134)
    snap = progress.snapshot()
    assert snap["phase"] == "collecting"  # unchanged
    assert snap["step"] == "Keywords 5/134"
    assert snap["current"] == 5


def test_bump_current_advances_counter():
    progress.start_run()
    progress.set_phase("collecting", total=10)
    progress.bump_current()
    progress.bump_current(2)
    assert progress.snapshot()["current"] == 3


def test_unknown_phase_silently_ignored():
    """Never raise from telemetry code — defence against typos in the
    pipeline that would otherwise crash the whole run."""
    progress.start_run()
    progress.set_phase("collecting")
    progress.set_phase("bogus-phase-name")  # ignored
    assert progress.snapshot()["phase"] == "collecting"


def test_finish_run_marks_done():
    progress.start_run()
    progress.set_phase("ai_scoring")
    progress.finish_run()
    snap = progress.snapshot()
    assert snap["phase"] == "done"
    assert snap["running"] is False


def test_finish_run_with_error_captures_message():
    progress.start_run()
    progress.finish_run(error="Bedrock throttled")
    snap = progress.snapshot()
    assert snap["last_error"] == "Bedrock throttled"


def test_pipeline_status_endpoint_returns_snapshot(client):
    progress.start_run(markets=["DACH"])
    progress.set_phase("translating", step="Translating IT article", current=2, total=5)
    resp = client.get("/pipeline-status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["phase"] == "translating"
    assert body["step"] == "Translating IT article"
    assert body["current"] == 2
    assert body["total"] == 5
    assert body["running"] is True


def test_pipeline_status_endpoint_ok_when_idle(client):
    """The endpoint must return 200 even when no run has happened yet,
    so the dashboard poller doesn't have to special-case startup."""
    resp = client.get("/pipeline-status")
    assert resp.status_code == 200
    assert resp.json()["phase"] == "idle"
    assert resp.json()["running"] is False

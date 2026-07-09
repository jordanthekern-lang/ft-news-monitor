"""v0.6.1 security middleware: TrustedHostMiddleware + Origin/Referer guard.

Both middlewares harden the localhost-only posture against the two
"localhost is safe" myths — DNS rebinding and CSRF drive-by. See
app/main.py for the docstring explaining the design.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app


# ---------------------------------------------------------------------------
# TrustedHostMiddleware — DNS rebinding
# ---------------------------------------------------------------------------


def test_trusted_host_accepts_loopback():
    """127.0.0.1 / localhost / testserver Host headers all pass."""
    client = TestClient(app, base_url="http://127.0.0.1:8766")
    r = client.get("/api/stats")
    assert r.status_code == 200
    r2 = client.get("/api/stats", headers={"host": "localhost:8766"})
    assert r2.status_code == 200


def test_trusted_host_rejects_arbitrary_domain():
    """A DNS-rebound Host header (evil.example) is 400'd by TrustedHost."""
    client = TestClient(app)
    r = client.get("/api/stats", headers={"host": "evil.example"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Origin guard — CSRF
# ---------------------------------------------------------------------------


def test_origin_guard_allows_no_header_client():
    """CLI / curl / test-harness clients that omit Origin/Referer pass —
    the guard only rejects when a browser-set header IS present and doesn't
    match loopback."""
    client = TestClient(app)
    r = client.post("/admin/suggestions/dismiss-all")
    # 200 = the request wasn't rejected by the CSRF guard (endpoint behaviour
    # is 200 + dismissed=0 on an empty queue).
    assert r.status_code == 200


def test_origin_guard_allows_loopback_origin():
    """Browser POST from the loopback UI passes."""
    client = TestClient(app)
    r = client.post(
        "/admin/suggestions/dismiss-all",
        headers={"origin": "http://127.0.0.1:8766"},
    )
    assert r.status_code == 200


def test_origin_guard_rejects_cross_origin_post():
    """Browser POST from evil.example (drive-by CSRF) is 403'd."""
    client = TestClient(app)
    r = client.post(
        "/admin/suggestions/dismiss-all",
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 403


def test_origin_guard_accepts_loopback_referer_when_origin_missing():
    """Some old browsers send only Referer; still counts as loopback."""
    client = TestClient(app)
    r = client.post(
        "/admin/suggestions/dismiss-all",
        headers={"referer": "http://localhost:8766/admin/suggestions"},
    )
    assert r.status_code == 200


def test_origin_guard_ignored_for_get():
    """GET requests aren't state-changing → the guard doesn't kick in even
    with a hostile Origin. (TrustedHost still blocks a hostile Host header —
    but Origin without a matching Host is only weird, not dangerous.)"""
    client = TestClient(app)
    r = client.get(
        "/api/stats",
        headers={"origin": "http://evil.example"},
    )
    assert r.status_code == 200

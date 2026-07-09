"""Tests for the P3 first-run setup wizard (Task 4).

Covers:
  * The setup gate (fresh DB redirects HTML GETs to /setup; /setup itself renders).
  * The happy-path flow: markets → aws → test-ai → import-seeds → schedule → finish.
  * Validation: empty markets, invalid market, bad time, finish before markets.
  * .env writer preserves unrelated lines and comments; new keys are appended.

External side effects — provider probe, .env writes, .env reload, seed imports —
are all monkeypatched; no test writes to a real .env or hits the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app import config as app_config
from app import providers, runtime_settings, seed_import, setup_wizard
from app.main import app


@pytest.fixture()
def client(fresh_setup):
    return TestClient(app)


@pytest.fixture()
def wizard_mocks(monkeypatch, tmp_path):
    """Snapshot every external side-effect the wizard performs.

    ``env_writes`` — recorded ``write_env_values(dict)`` calls.
    ``reloads``   — count of ``reload_env()`` calls.
    ``imports``   — recorded ``import_seed_file`` (path, mode) pairs.
    ``test_result`` — mutable dict the mock provider probe returns.
    ``set_seeds_dir(mapping)`` — install fake seed files (name → payload).
    """
    env_writes: list = []
    reloads: dict = {"count": 0}
    imports: list = []
    test_result: dict = {
        "ok": True,
        "model": "eu.anthropic.claude-opus-4-7",
        "latency_ms": 42,
        "error": None,
    }

    def fake_write_env_values(values):
        env_writes.append(dict(values))

    def fake_reload_env():
        reloads["count"] += 1

    async def fake_test_connection(settings):
        return dict(test_result)

    seeds_dir = tmp_path / "seeds"
    seeds_dir.mkdir()
    monkeypatch.setattr(setup_wizard, "_seeds_dir", lambda: seeds_dir)

    def fake_import_seed_file(path, mode="merge", **kwargs):
        imports.append((Path(path).name, mode))
        return {
            "keywords": {"inserted": 3, "skipped": 0, "markt_unioned": 0},
            "feeds": {"inserted": 1, "skipped": 0, "markt_unioned": 0},
            "outlets": {"inserted": 2, "skipped": 0, "markt_unioned": 0},
            "negative_keywords": {"inserted": 0, "skipped": 0, "markt_unioned": 0},
            "total": {"inserted": 6, "skipped": 0, "markt_unioned": 0},
        }

    def set_seeds_dir(mapping: dict) -> None:
        for name, payload in mapping.items():
            (seeds_dir / name).write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(app_config, "write_env_values", fake_write_env_values)
    monkeypatch.setattr(app_config, "reload_env", fake_reload_env)
    monkeypatch.setattr(setup_wizard._config, "write_env_values", fake_write_env_values)
    monkeypatch.setattr(setup_wizard._config, "reload_env", fake_reload_env)
    monkeypatch.setattr(providers, "test_connection", fake_test_connection)
    monkeypatch.setattr(setup_wizard.providers, "test_connection", fake_test_connection)
    monkeypatch.setattr(seed_import, "import_seed_file", fake_import_seed_file)
    monkeypatch.setattr(setup_wizard.seed_import, "import_seed_file", fake_import_seed_file)

    return {
        "env_writes": env_writes,
        "reloads": reloads,
        "imports": imports,
        "test_result": test_result,
        "set_seeds_dir": set_seeds_dir,
        "seeds_dir": seeds_dir,
    }


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_gate_redirects_dashboard_on_fresh_db(client):
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/setup"


def test_gate_lets_setup_page_render(client):
    resp = client.get("/setup")
    assert resp.status_code == 200
    # Stepper markers rendered in English.
    assert "Home markets" in resp.text
    assert "AI credentials" in resp.text
    assert "Import seed packs" in resp.text
    # Secret placeholders are password inputs, not text inputs.
    assert 'type="password"' in resp.text


def test_gate_exempts_api_stats(client):
    resp = client.get("/api/stats")
    assert resp.status_code == 200


def test_gate_stops_redirecting_after_finish(client, wizard_mocks):
    # Minimum requirement to finish: at least one home market.
    runtime_settings.set(runtime_settings.HOME_MARKETS, ["Italy"])
    resp = client.post("/setup/finish", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert runtime_settings.get(runtime_settings.SETUP_COMPLETED) is True

    # Now GET / must render, not redirect.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Happy-path flow
# ---------------------------------------------------------------------------


def test_full_wizard_flow(client, wizard_mocks):
    # Step 1 — home markets
    resp = client.post(
        "/setup/markets",
        data={"home_markets": ["Italy"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert runtime_settings.get(runtime_settings.HOME_MARKETS) == ["Italy"]

    # v0.6.1 M11: /setup/markets writes TIMEZONE from the first market
    # (auto-fill; user can override in /settings). Consume it here so
    # the AWS-step assertion below can still check for a single write.
    assert wizard_mocks["env_writes"] == [{"TIMEZONE": "Europe/Rome"}]
    wizard_mocks["env_writes"].clear()

    # Step 2 — aws (keys mode)
    resp = client.post(
        "/setup/aws",
        data={
            "auth_mode": "keys",
            "ai_provider": "bedrock",
            "aws_access_key_id": "AKIA_TEST",
            "aws_secret_access_key": "sekret",
            "aws_region": "eu-central-1",
            "anthropic_model": "eu.anthropic.claude-opus-4-7",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # Exactly one write, containing exactly the submitted keys (plus AI_PROVIDER,
    # AWS_REGION, ANTHROPIC_MODEL — everything the user actually filled in).
    assert len(wizard_mocks["env_writes"]) == 1
    written = wizard_mocks["env_writes"][0]
    assert written == {
        "AI_PROVIDER": "bedrock",
        "AWS_ACCESS_KEY_ID": "AKIA_TEST",
        "AWS_SECRET_ACCESS_KEY": "sekret",
        "AWS_REGION": "eu-central-1",
        "ANTHROPIC_MODEL": "eu.anthropic.claude-opus-4-7",
    }
    # reloads: 1 for the markets-step TIMEZONE-autofill, 1 for AWS. If the
    # markets step accidentally skipped its reload, this would flag it.
    assert wizard_mocks["reloads"]["count"] == 2
    # And the response body must not echo the submitted secret back.
    assert "AKIA_TEST" not in resp.text
    assert "sekret" not in resp.text

    # Step 3 — test AI (mocked ok)
    resp = client.post("/setup/test-ai")
    assert resp.status_code == 200
    payload = resp.json()
    assert payload["ok"] is True
    assert payload["model"] == "eu.anthropic.claude-opus-4-7"
    assert payload["latency_ms"] == 42

    # Step 4 — import seeds. Only italy.json exists in the fake seeds dir;
    # eu.json is missing → wizard must report "not available yet" for EU.
    wizard_mocks["set_seeds_dir"]({"italy.json": {"market": "Italy"}})

    resp = client.post(
        "/setup/import-seeds",
        data={"markets": ["Italy", "EU"]},
    )
    assert resp.status_code == 200
    assert wizard_mocks["imports"] == [("italy.json", "merge")]
    # The rendered response surfaces both statuses.
    assert "not available yet" in resp.text
    assert "Italy" in resp.text

    # Step 5 — schedule
    resp = client.post(
        "/setup/schedule",
        data={"scheduler_enabled": "1", "scheduler_time": "06:30"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert runtime_settings.get(runtime_settings.SCHEDULER_ENABLED) is True
    assert runtime_settings.get(runtime_settings.SCHEDULER_TIME) == "06:30"

    # Step 6 — finish
    resp = client.post("/setup/finish", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"
    assert runtime_settings.get(runtime_settings.SETUP_COMPLETED) is True

    # Gate now lets / through.
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_markets_rejects_empty(client, wizard_mocks):
    resp = client.post("/setup/markets", data={}, follow_redirects=False)
    assert resp.status_code == 400


def test_markets_rejects_invalid_name(client, wizard_mocks):
    resp = client.post(
        "/setup/markets",
        data={"home_markets": ["Italy", "Narnia"]},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Narnia" in resp.text


def test_schedule_calls_scheduler_reload(client, wizard_mocks, monkeypatch):
    """Wiring regression: POST /setup/schedule must invoke
    ``scheduler.reload()`` so the operator's toggle takes effect immediately
    on a running app (and so a fresh install kicks the scheduler on)."""
    reload_mock = MagicMock()
    monkeypatch.setattr(setup_wizard.scheduler, "reload", reload_mock)
    resp = client.post(
        "/setup/schedule",
        data={"scheduler_enabled": "1", "scheduler_time": "06:30"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert reload_mock.called


def test_schedule_rejects_bad_time(client, wizard_mocks):
    resp = client.post(
        "/setup/schedule",
        data={"scheduler_enabled": "1", "scheduler_time": "25:99"},
        follow_redirects=False,
    )
    assert resp.status_code == 400


def test_finish_rejects_before_markets(client, wizard_mocks):
    resp = client.post("/setup/finish", follow_redirects=False)
    assert resp.status_code == 400
    # Flag must NOT have been set.
    assert not runtime_settings.get(runtime_settings.SETUP_COMPLETED)


# ---------------------------------------------------------------------------
# Import-seeds — mapping + underscore-file invisibility
# ---------------------------------------------------------------------------


def test_import_seeds_ignores_underscore_pending_file(client, wizard_mocks):
    """The wizard MUST NOT touch ``_pending_research.json`` — it's not a market pack."""
    # Both files present in the fake seeds dir.
    wizard_mocks["set_seeds_dir"](
        {
            "italy.json": {"market": "Italy"},
            "_pending_research.json": {"market": "legacy"},
        }
    )
    resp = client.post("/setup/import-seeds", data={"markets": ["Italy"]})
    assert resp.status_code == 200
    # Only italy.json was imported; the underscore file must never appear.
    imported_names = [name for name, _ in wizard_mocks["imports"]]
    assert imported_names == ["italy.json"]
    assert "_pending_research.json" not in imported_names


# ---------------------------------------------------------------------------
# write_env_values — patch-in-place semantics (uses real writer, no mocks)
# ---------------------------------------------------------------------------


def test_write_env_values_preserves_unrelated_and_comments(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# region\n"
        "AWS_ACCESS_KEY_ID=xxx\n"
        "SOME_UNRELATED=keep_me\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_config, "env_path", lambda: env_file)

    app_config.write_env_values({"FOO": "bar"})

    text = env_file.read_text(encoding="utf-8")
    assert "# region" in text
    assert "AWS_ACCESS_KEY_ID=xxx" in text
    assert "SOME_UNRELATED=keep_me" in text
    assert "FOO=bar" in text


def test_write_env_values_replaces_in_place(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# top\n"
        "AWS_ACCESS_KEY_ID=old_value\n"
        "OTHER=keep\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(app_config, "env_path", lambda: env_file)

    app_config.write_env_values({"AWS_ACCESS_KEY_ID": "new_value"})

    text = env_file.read_text(encoding="utf-8")
    # Old value is gone; new value is in the same file.
    assert "old_value" not in text
    assert "AWS_ACCESS_KEY_ID=new_value" in text
    # Unrelated line and comment survive.
    assert "# top" in text
    assert "OTHER=keep" in text


def test_write_env_values_quotes_values_with_spaces(monkeypatch, tmp_path):
    env_file = tmp_path / ".env"
    monkeypatch.setattr(app_config, "env_path", lambda: env_file)

    app_config.write_env_values({"NOTE": "hello world"})
    text = env_file.read_text(encoding="utf-8")
    assert 'NOTE="hello world"' in text


def test_write_env_values_is_atomic_on_crash(monkeypatch, tmp_path):
    """If the atomic swap raises, the original .env must be untouched.

    Simulates a crash by making ``os.replace`` raise: with the atomic
    tmp-file + replace pattern, the original file's content survives intact,
    and the tmp file is cleaned up (no ``.env.*`` litter left behind).
    """
    env_file = tmp_path / ".env"
    original = (
        "# region\n"
        "AWS_ACCESS_KEY_ID=original_value\n"
        "OTHER=keep_me\n"
    )
    env_file.write_text(original, encoding="utf-8")
    monkeypatch.setattr(app_config, "env_path", lambda: env_file)

    import os as _os

    def boom(src, dst):
        raise OSError("simulated crash during atomic replace")

    monkeypatch.setattr(_os, "replace", boom)

    with pytest.raises(OSError):
        app_config.write_env_values({"AWS_ACCESS_KEY_ID": "new_value"})

    # Original file content is intact — no truncation, no partial write.
    assert env_file.read_text(encoding="utf-8") == original
    # And no leftover tmp file in the directory.
    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".env.")]
    assert leftovers == [], f"tmp file not cleaned up: {leftovers}"


# ---------------------------------------------------------------------------
# /setup/aws — secrets never echoed, blank fields never written
# ---------------------------------------------------------------------------


def test_aws_response_never_echoes_submitted_secret(client, wizard_mocks):
    """Happy path: follow the 303 redirect and confirm the rendered setup
    page contains neither the submitted access key nor the secret key."""
    # Markets step must be completed first so the setup gate keeps letting
    # us through.
    client.post("/setup/markets", data={"home_markets": ["Italy"]})

    secret_key = "AKIAIOSFODNN7EXAMPLE"
    secret_secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    r = client.post(
        "/setup/aws",
        data={
            "auth_mode": "keys",
            "ai_provider": "bedrock",
            "aws_access_key_id": secret_key,
            "aws_secret_access_key": secret_secret,
            "aws_region": "eu-west-1",
            "anthropic_model": "eu.anthropic.claude-opus-4-7",
        },
        follow_redirects=True,
    )
    assert r.status_code == 200
    body = r.text
    assert secret_key not in body
    assert secret_secret not in body
    # Also assert no last-4 masking pattern leaks the tail of the secret.
    assert secret_secret[-4:] not in body


def test_aws_error_response_never_echoes_secret(client, wizard_mocks):
    """Error path: invalid region triggers a 400 — the secret in the payload
    must not appear in the error response body."""
    client.post("/setup/markets", data={"home_markets": ["Italy"]})

    secret_secret = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
    r = client.post(
        "/setup/aws",
        data={
            "auth_mode": "keys",
            "ai_provider": "bedrock",
            "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
            "aws_secret_access_key": secret_secret,
            "aws_region": "not-a-valid-region-xyz",
            "anthropic_model": "eu.anthropic.claude-opus-4-7",
        },
    )
    # Handler raises HTTPException(400) for an invalid region.
    assert r.status_code >= 400
    assert secret_secret not in r.text
    assert "AKIAIOSFODNN7EXAMPLE" not in r.text


def test_aws_blank_secret_leaves_env_untouched(client, wizard_mocks):
    """When the operator submits the AWS step with blank secret fields
    (only region / model / provider), :func:`write_env_values` must NOT be
    called with those secret keys — otherwise the "leave blank to keep"
    workflow silently blanks the .env on the next submit."""
    client.post("/setup/markets", data={"home_markets": ["Italy"]})
    # v0.6.1 M11: consume the TIMEZONE auto-fill from the markets step so
    # the "exactly one write" assertion below still holds for the AWS step.
    wizard_mocks["env_writes"].clear()

    r = client.post(
        "/setup/aws",
        data={
            "auth_mode": "keys",
            "ai_provider": "bedrock",
            "aws_access_key_id": "",       # blank
            "aws_secret_access_key": "",   # blank
            "aws_region": "eu-west-1",
            "anthropic_model": "eu.anthropic.claude-opus-4-7",
            "anthropic_api_key": "",       # blank
        },
        follow_redirects=False,
    )
    assert r.status_code < 400  # 303 redirect is the accept path.

    # Exactly one write_env_values call, and it MUST NOT carry any of the
    # secret keys (the handler skips empty-string values before writing).
    assert len(wizard_mocks["env_writes"]) == 1
    written = wizard_mocks["env_writes"][0]
    assert "AWS_ACCESS_KEY_ID" not in written
    assert "AWS_SECRET_ACCESS_KEY" not in written
    assert "ANTHROPIC_API_KEY" not in written
    # Non-secret fields still get written — that's expected.
    assert written.get("AI_PROVIDER") == "bedrock"
    assert written.get("AWS_REGION") == "eu-west-1"

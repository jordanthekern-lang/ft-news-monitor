"""Tests for app/providers.py — AI provider abstraction.

TDD: these tests are written before the implementation exists and should
initially fail with ImportError / AttributeError.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# create_ai_client
# ---------------------------------------------------------------------------

def _make_settings(provider: str = "bedrock", api_key: str = ""):
    """Build a minimal Settings-like object."""
    from app.config import Settings
    return Settings(
        ai_provider=provider,
        anthropic_api_key=api_key,
        aws_profile="",
        aws_access_key_id="",
        aws_secret_access_key="",
        aws_session_token="",
        aws_region="eu-west-1",
        anthropic_model="eu.anthropic.claude-opus-4-7",
        ai_max_tokens=512,
        dashboard_port=8766,
        timezone="Europe/Berlin",
        max_age_days=180,
        highlight_days=30,
        ai_filter_max_parallel=5,
        ai_skip_prescreen_days=7,
    )


def test_create_ai_client_returns_bedrock_by_default(monkeypatch):
    """create_ai_client with provider='bedrock' returns AsyncAnthropicBedrock."""
    from anthropic import AsyncAnthropicBedrock

    # Patch the bedrock constructor to avoid real credential resolution
    fake_creds = {
        "aws_access_key": "FAKE",
        "aws_secret_key": "FAKESECRET",
        "aws_region": "eu-west-1",
    }
    from app import providers
    monkeypatch.setattr(providers, "resolve_aws_credentials", lambda s: fake_creds)

    settings = _make_settings(provider="bedrock")
    client = providers.create_ai_client(settings)
    assert isinstance(client, AsyncAnthropicBedrock)


def test_create_ai_client_returns_anthropic_direct(monkeypatch):
    """create_ai_client with provider='anthropic' returns AsyncAnthropic."""
    from anthropic import AsyncAnthropic

    settings = _make_settings(provider="anthropic", api_key="sk-ant-test")
    from app import providers
    client = providers.create_ai_client(settings)
    assert isinstance(client, AsyncAnthropic)


def test_create_ai_client_bedrock_raises_when_no_creds(monkeypatch):
    """create_ai_client raises ValueError when bedrock chosen but no credentials."""
    from app import providers
    monkeypatch.setattr(providers, "resolve_aws_credentials", lambda s: None)

    settings = _make_settings(provider="bedrock")
    with pytest.raises(ValueError, match="AWS credentials"):
        providers.create_ai_client(settings)


def test_create_ai_client_raises_on_invalid_provider():
    """create_ai_client raises ValueError when ai_provider is invalid."""
    from app import providers

    settings = _make_settings(provider="aws")
    with pytest.raises(ValueError, match="Invalid AI_PROVIDER.*aws.*bedrock.*anthropic"):
        providers.create_ai_client(settings)


# ---------------------------------------------------------------------------
# test_connection
# ---------------------------------------------------------------------------

def test_connection_returns_ok_false_on_exception(monkeypatch):
    """test_connection returns ok=False with error string when call raises."""
    from app import providers

    # Monkeypatch create_ai_client to return a mock whose messages.create raises
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=RuntimeError("no network"))
    monkeypatch.setattr(providers, "create_ai_client", lambda s: fake_client)

    settings = _make_settings(provider="anthropic", api_key="x")
    result = asyncio.run(providers.test_connection(settings))

    assert result["ok"] is False
    assert isinstance(result["error"], str)
    assert "no network" in result["error"]
    assert isinstance(result["latency_ms"], int)
    assert isinstance(result["model"], str)


def test_connection_returns_ok_true_on_success(monkeypatch):
    """test_connection returns ok=True with latency and model when successful."""
    from app import providers

    # Build a fake response matching what the Anthropic SDK returns
    fake_content = MagicMock()
    fake_content.type = "text"
    fake_content.text = "pong"

    fake_response = MagicMock()
    fake_response.model = "eu.anthropic.claude-opus-4-7"
    fake_response.content = [fake_content]

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)
    monkeypatch.setattr(providers, "create_ai_client", lambda s: fake_client)

    settings = _make_settings(provider="anthropic", api_key="x")
    result = asyncio.run(providers.test_connection(settings))

    assert result["ok"] is True
    assert result["error"] is None
    assert result["latency_ms"] >= 0
    assert result["model"] == "eu.anthropic.claude-opus-4-7"


def test_connection_result_shape(monkeypatch):
    """test_connection always returns a dict with the 4 required keys."""
    from app import providers

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(side_effect=Exception("boom"))
    monkeypatch.setattr(providers, "create_ai_client", lambda s: fake_client)

    settings = _make_settings(provider="anthropic", api_key="x")
    result = asyncio.run(providers.test_connection(settings))

    assert set(result.keys()) >= {"ok", "model", "latency_ms", "error"}


# ---------------------------------------------------------------------------
# v0.6.1 — friendly error messages + credential surface
# ---------------------------------------------------------------------------


def test_profile_not_found_surfaces_actionable_error(monkeypatch):
    """v0.6.1 M1: resolve_aws_credentials raises CredentialResolutionError
    with the mistyped profile name in the message, and test_connection
    surfaces it verbatim so the Test-AI wizard button shows
    'AWS profile flixbus-it not found' instead of the generic
    'credentials are required'."""
    from app import providers

    settings = _make_settings(provider="bedrock")
    # Override the empty aws_profile with a bogus name.
    settings_with_profile = _make_settings(provider="bedrock")
    from dataclasses import replace
    settings_with_profile = replace(settings_with_profile, aws_profile="typo-xyz-42")

    result = asyncio.run(providers.test_connection(settings_with_profile))
    assert result["ok"] is False
    assert "typo-xyz-42" in result["error"]
    assert "profile" in result["error"].lower()


def test_static_keys_include_session_token_when_set():
    """v0.6.1 M2: resolve_aws_credentials must forward AWS_SESSION_TOKEN
    when set. STS-temporary (ASIA) keys need it; without forwarding,
    Bedrock would reject the request with an opaque error."""
    from dataclasses import replace
    from app import providers

    settings = replace(
        _make_settings(provider="bedrock"),
        aws_access_key_id="ASIAEXAMPLE",
        aws_secret_access_key="secret",
        aws_session_token="the-session-token",
    )
    creds = providers.resolve_aws_credentials(settings)
    assert creds is not None
    assert creds.get("aws_session_token") == "the-session-token"


def test_static_keys_omit_session_token_when_unset():
    """Permanent IAM (AKIA) keys don't have a session token; the returned
    kwargs must NOT contain a None or empty aws_session_token, or the
    AsyncAnthropicBedrock constructor could send an empty header."""
    from dataclasses import replace
    from app import providers

    settings = replace(
        _make_settings(provider="bedrock"),
        aws_access_key_id="AKIAEXAMPLE",
        aws_secret_access_key="secret",
        aws_session_token="",
    )
    creds = providers.resolve_aws_credentials(settings)
    assert creds is not None
    assert "aws_session_token" not in creds


def test_friendlier_bedrock_error_translates_403(monkeypatch):
    """AccessDenied surface: users get 'ask IT to grant bedrock:InvokeModel'
    instead of a raw boto3 stacktrace."""
    from app import providers

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        side_effect=Exception("AccessDeniedException: 403 not authorized")
    )
    monkeypatch.setattr(providers, "create_ai_client", lambda s: fake_client)

    settings = _make_settings(provider="anthropic", api_key="x")
    result = asyncio.run(providers.test_connection(settings))
    assert result["ok"] is False
    assert "InvokeModel" in result["error"] or "permission" in result["error"].lower()


def test_friendlier_bedrock_error_translates_expired_token(monkeypatch):
    """ExpiredToken → hint to run aws-azure-login."""
    from app import providers

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        side_effect=Exception("ExpiredTokenException: token is expired")
    )
    monkeypatch.setattr(providers, "create_ai_client", lambda s: fake_client)

    settings = _make_settings(provider="anthropic", api_key="x")
    result = asyncio.run(providers.test_connection(settings))
    assert result["ok"] is False
    assert "expired" in result["error"].lower()
    assert "sso" in result["error"].lower() or "azure-login" in result["error"].lower()


def test_error_message_redacts_account_id(monkeypatch):
    """AWS account IDs (12 digits) redacted from error responses so DNS
    rebinding attacks can't scrape them via /setup/test-ai."""
    from app import providers

    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(
        side_effect=Exception("boom for account 123456789012 in eu-west-1")
    )
    monkeypatch.setattr(providers, "create_ai_client", lambda s: fake_client)

    settings = _make_settings(provider="anthropic", api_key="x")
    result = asyncio.run(providers.test_connection(settings))
    assert "123456789012" not in result["error"]
    assert "<account-id>" in result["error"]


# ---------------------------------------------------------------------------
# _coerce_score fallback category (in ai_filter)
# ---------------------------------------------------------------------------

def test_coerce_score_invalid_category_falls_back_to_Other():
    """_coerce_score must map invalid category to 'Other', which is in VALID_CATEGORIES."""
    from app.ai_filter import _coerce_score
    from app.config import VALID_CATEGORIES

    assert "Other" in VALID_CATEGORIES, "'Other' must be in VALID_CATEGORIES"

    score = _coerce_score({
        "relevance_score": 5,
        "category": "Bullshit",
        "themes": [],
        "reasoning": "test",
    })
    assert score.category == "Other"
    assert score.category in VALID_CATEGORIES


def test_coerce_score_valid_category_preserved():
    """_coerce_score keeps a valid English category as-is."""
    from app.ai_filter import _coerce_score

    score = _coerce_score({
        "relevance_score": 7,
        "category": "Vehicle funding",
        "themes": [],
        "reasoning": "direct funding",
    })
    assert score.category == "Vehicle funding"

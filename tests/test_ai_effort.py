"""Effort parameter + refusal handling for the AI filter (Sonnet 5.5 switch)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import ai_filter, config


class _FakeMessages:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


class _FakeClient:
    def __init__(self, response):
        self.messages = _FakeMessages(response)


def _resp(text: str, stop_reason: str = "end_turn"):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


def _row():
    return {
        "id": 1, "title": "E-Reisebus Förderung", "snippet": "BMV startet Aufruf",
        "source_name": "busplaner", "country": "DACH", "published_at": "2026-10-28",
        "url": "https://www.busplaner.de/x", "language": "de",
        "title_en": None, "snippet_en": None,
    }


def test_create_with_retry_sends_effort():
    client = _FakeClient(_resp("{}"))
    asyncio.run(ai_filter._create_with_retry(
        client, model="m", system="s", user_prompt="u", article_id=1,
        max_tokens=100, effort="medium",
    ))
    assert client.messages.calls[0]["output_config"] == {"effort": "medium"}


def test_create_with_retry_omits_output_config_without_effort():
    client = _FakeClient(_resp("{}"))
    asyncio.run(ai_filter._create_with_retry(
        client, model="m", system="s", user_prompt="u", article_id=1,
        max_tokens=100,
    ))
    assert "output_config" not in client.messages.calls[0]


def test_score_one_returns_none_on_refusal():
    client = _FakeClient(_resp("", stop_reason="refusal"))
    score = asyncio.run(ai_filter._score_one(
        client, model="m", max_tokens=100, row=_row(),
        sem=asyncio.Semaphore(1), examples=[], effort="medium",
    ))
    assert score is None


def test_settings_defaults_sonnet_55_medium(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.delenv("AI_EFFORT", raising=False)
    s = config.get_settings()
    assert s.anthropic_model == "eu.anthropic.claude-sonnet-5-5"
    assert s.ai_effort == "medium"


def test_settings_empty_effort_disables(monkeypatch):
    monkeypatch.setenv("AI_EFFORT", "")
    assert config.get_settings().ai_effort == ""

"""Tests für den LLM-Gate der Follow-up-Erkennung (``_expects_reply_llm``).

Kein echtes Ollama: der HTTP-Call ist via ``respx`` gemockt, ``get_settings``
liefert ein Fake-Setting. Geprüft werden die drei Ausgänge des Drei-Zustands-
Gates: offene Frage (True), bewusst kein Nachfassen (False) und transienter
Fehler (None -> nicht persistieren).
"""

from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest
import respx

import app.services.followup as fu

_OLLAMA_URL = "http://ollama:11434/v1/chat/completions"


def _settings(model="ollama/qwen3.6:latest", base="http://ollama:11434"):
    return SimpleNamespace(triage_model=model, ollama_base_url=base)


@pytest.mark.asyncio
@respx.mock
async def test_gate_detects_open_question():
    respx.post(_OLLAMA_URL).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"erwartet_antwort": true, "grund": "Offene Frage zum Angebot"}'}}]},
        )
    )
    with patch.object(fu, "get_settings", return_value=_settings()):
        decision, reason = await fu._expects_reply_llm(
            "Angebot Phase 2", "Bitte gib mir bis Freitag Bescheid.", "kunde@example.ch"
        )
    assert decision is True
    assert "Angebot" in reason


@pytest.mark.asyncio
@respx.mock
async def test_gate_skips_confirmation():
    respx.post(_OLLAMA_URL).mock(
        return_value=httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"erwartet_antwort": false, "grund": "Reine Terminzusage"}'}}]},
        )
    )
    with patch.object(fu, "get_settings", return_value=_settings()):
        decision, reason = await fu._expects_reply_llm(
            "Angenommen: Weekly", "", "kunde@example.ch"
        )
    assert decision is False
    assert reason == "Reine Terminzusage"


@pytest.mark.asyncio
@respx.mock
async def test_gate_transient_error_returns_none():
    respx.post(_OLLAMA_URL).mock(return_value=httpx.Response(500))
    with patch.object(fu, "get_settings", return_value=_settings()):
        decision, reason = await fu._expects_reply_llm("x", "y", "z@example.ch")
    assert decision is None
    assert reason == "unklar"


@pytest.mark.asyncio
@respx.mock
async def test_gate_invalid_json_returns_none():
    respx.post(_OLLAMA_URL).mock(
        return_value=httpx.Response(200, json={"choices": [{"message": {"content": "kein json"}}]})
    )
    with patch.object(fu, "get_settings", return_value=_settings()):
        decision, reason = await fu._expects_reply_llm("x", "y", "z@example.ch")
    assert decision is None
    assert reason == "unklar"


@pytest.mark.asyncio
@respx.mock
async def test_gate_sends_no_think_prompt():
    """Der System-Prompt enthält /no_think und fordert JSON-Output."""
    route = respx.post(_OLLAMA_URL).mock(
        return_value=httpx.Response(
            200, json={"choices": [{"message": {"content": '{"erwartet_antwort": false, "grund": "x"}'}}]}
        )
    )
    with patch.object(fu, "get_settings", return_value=_settings()):
        await fu._expects_reply_llm("Betreff", "Text", "a@b.ch")
    import json as _json

    sent = _json.loads(route.calls.last.request.content)
    assert sent["temperature"] == 0
    assert sent["response_format"] == {"type": "json_object"}
    assert "/no_think" in sent["messages"][0]["content"]

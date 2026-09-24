"""Ein Abbruch am Hermes-Schleifenwaechter ist keine Antwort.

Ab dem 03.09.2026 brach rund jede fuenfte Mail-Triage an ``same_tool_failure_halt``
ab (Werkzeugnamen aus Hermes 0.20 gegen die Bruecke von 0.21). Nachfass-Lauf und
Structured-Reask behandelten den Waechtertext als Analyse, und das Modell ordnete
ihn ein: es entstanden Aufgaben wie «Die Automation nach 'same_tool_failure_halt'
ist stehen geblieben», und echte Mails landeten als System/fyi im Archiv.

Geprueft wird die Logik, nicht das Modell: Agent und DB sind Attrappen.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.hermes_worker as hw

_HALT = {
    "action": "halt",
    "code": "same_tool_failure_halt",
    "message": "Tool-call guardrail stopped the loop.",
    "tool_name": "tool_call",
    "count": 5,
    "signature": {"tool_name": "tool_call", "args_hash": "abc"},
}

_META = {
    "email_message_id": "M1",
    "subject": "Offerte Q4",
    "from_address": "kunde@example.ch",
    "conversation_id": "CONV1",
}


class _HaltAgent:
    """Hermes-Attrappe: jeder Lauf endet am Schleifenwaechter."""

    model = "qwen3.6:latest"
    session_total_tokens = 0
    request_overrides = None
    max_iterations = 90

    def __init__(self, halt: bool = True):
        self.halt = halt
        self.prompts: list[str] = []

    def run_conversation(self, prompt, system_message=None):
        self.prompts.append(prompt)
        result = {"final_response": _HALT["message"]}
        if self.halt:
            result["guardrail"] = dict(_HALT)
        return result


class _RecordingDB:
    def __init__(self, log):
        self._log = log
        self.commit = AsyncMock()

    async def execute(self, stmt, *args, **kwargs):
        self._log.append(stmt)
        res = MagicMock()
        res.scalar_one_or_none.return_value = None
        return res


class _RecordingSession:
    def __init__(self, log):
        self._db = _RecordingDB(log)

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *args):
        return False


def _params(statements) -> list[dict]:
    return [s.compile().params for s in statements if hasattr(s, "compile")]


@pytest.fixture(autouse=True)
def _job_state():
    hw._job_guardrail_halts.clear()
    yield
    hw._job_guardrail_halts.clear()


# ── _run_agent_sync ──────────────────────────────────────────────────────────


def test_run_agent_sync_wirft_bei_halt():
    with pytest.raises(hw.GuardrailHalt) as exc:
        hw._run_agent_sync(_HaltAgent(), "prompt", False)
    assert exc.value.guardrail["code"] == "same_tool_failure_halt"
    assert "tool_call" in str(exc.value)
    assert hw._job_guardrail_halts == [_HALT]


def test_run_agent_sync_ohne_halt_liefert_antwort():
    assert hw._run_agent_sync(_HaltAgent(halt=False), "prompt", False) == _HALT["message"]
    assert hw._job_guardrail_halts == []


# ── Triage: fail-closed statt Einordnung des Waechtertexts ───────────────────


@pytest.mark.asyncio
async def test_triage_halt_geht_direkt_in_den_rueckfall():
    agent = _HaltAgent()
    log: list = []
    fallback = AsyncMock(return_value="completed")
    post_process = AsyncMock()
    reask = AsyncMock()
    with (
        patch.object(hw, "async_session", lambda: _RecordingSession(log)),
        patch.object(hw, "_fallback_unparsed_triage", new=fallback),
        patch.object(hw, "_post_process_triage", new=post_process),
        patch.object(hw, "_structured_triage_reask", new=reask),
    ):
        await hw._process_job(agent, "job-1", "email_triage", "PROMPT", dict(_META))

    # Genau ein Lauf: kein Nachfass, der den Waechtertext zu sehen bekaeme.
    assert agent.prompts == ["PROMPT"]
    post_process.assert_not_awaited()
    reask.assert_not_awaited()
    fallback.assert_awaited_once()
    assert fallback.await_args.kwargs["guardrail"]["code"] == "same_tool_failure_halt"

    final = [p for p in _params(log) if p.get("status") == "completed"]
    assert final, "Job wurde nicht abgeschlossen"
    assert final[-1]["metadata"]["guardrail_halts"] == [_HALT]


@pytest.mark.asyncio
async def test_rueckfall_nennt_das_werkzeug_und_legt_keine_aufgabe_an():
    log: list = []
    create_task = AsyncMock()
    with (
        patch.object(hw, "async_session", lambda: _RecordingSession(log)),
        patch.object(
            hw, "_finalize_email_state",
            new=AsyncMock(return_value=hw.FinalizeResult(None, "M1")),
        ) as finalize,
        patch.object(hw, "_persist_final_message_id", new=AsyncMock()),
        patch.object(hw, "_create_email_task", new=create_task),
    ):
        status = await hw._fallback_unparsed_triage("job-1", dict(_META), guardrail=dict(_HALT))

    assert status == "completed"
    create_task.assert_not_awaited()
    assert finalize.await_args.kwargs["needs_review"] is True
    action = next(p["suggested_action"] for p in _params(log) if "suggested_action" in p)
    assert action["triage_class"] == "fyi"
    assert action["needs_review"] is True
    assert action["guardrail_halt"]["tool_name"] == "tool_call"
    assert "tool_call" in action["rationale"]


# ── Andere Jobtypen scheitern sichtbar ───────────────────────────────────────


@pytest.mark.asyncio
async def test_generischer_job_mit_halt_ist_failed():
    log: list = []
    with (
        patch.object(hw, "async_session", lambda: _RecordingSession(log)),
        patch.object(hw, "record_episode", new=AsyncMock()),
    ):
        await hw._process_job(_HaltAgent(), "job-2", "task", "PROMPT", {})

    failed = [p for p in _params(log) if p.get("status") == "failed"]
    assert failed
    assert "same_tool_failure_halt" in failed[-1]["error_message"]


@pytest.mark.asyncio
async def test_recherche_mit_halt_liefert_kein_dossier():
    """Der Waechtertext darf nicht als «belegter Fachkontext» in den Schreib-Pass."""
    with (
        patch.object(hw, "_init_gather_agent", new=AsyncMock(return_value=_HaltAgent())),
        patch.object(hw, "_build_gather_prompt", new=AsyncMock(return_value="SAMMELN")),
    ):
        assert await hw._gather_draft_context(dict(_META)) is None
    assert hw._job_guardrail_halts == [_HALT]

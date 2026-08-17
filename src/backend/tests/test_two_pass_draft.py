"""Tests fuer den Zwei-Pass-Entwurf (Klassifikation vom Schreiben getrennt).

Deckt ab:
- ``_draft_sampling_overrides``: Prosa-Sampling-Dict + Thinking-Schalter.
- ``_run_agent_sync``: ``overrides`` werden gesetzt und wieder zurueckgesetzt.
- ``_post_process_triage`` im Zwei-Pass: auto_reply loest den separaten Schreib-Pass
  aus; mit Entwurf -> awaiting_approval, ohne Entwurf -> fail-closed fyi (kein Task).
- ``_build_draft_prompt``: fokussierter Schreib-Prompt mit erzwungenem reply_to_id.

Kein LLM/keine echte DB -- externe Effekte sind gemockt; geprueft wird die *Logik*.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.services.hermes_worker as hw


# ── Fakes (DB-Session + Seiteneffekte) ───────────────────────────────────────


class _FakeDB:
    def __init__(self, job=None):
        self._job = job
        self.commit = AsyncMock()

    async def execute(self, *args, **kwargs):
        res = MagicMock()
        res.scalar_one_or_none.return_value = self._job
        return res


class _FakeSession:
    def __init__(self, job=None):
        self._db = _FakeDB(job)

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *args):
        return False


def _session_factory(job=None):
    return lambda: _FakeSession(job)


_META = {
    "email_message_id": "M1",
    "subject": "Testbetreff",
    "from_address": "kunde@example.ch",
    "from_name": "Kundin",
    "conversation_id": "CONV1",
}


def _postprocess_patches(job=None, two_pass=True):
    """DB-Session + alle externen Seiteneffekte + get_settings (two_pass)."""
    settings = SimpleNamespace(
        two_pass_draft=two_pass, triage_low_confidence_threshold=0.5
    )
    return [
        patch.object(hw, "async_session", _session_factory(job)),
        patch.object(hw, "_create_email_task", new=AsyncMock(return_value=None)),
        patch.object(hw, "_finalize_email_state", new=AsyncMock(return_value=None)),
        patch.object(hw, "record_episode", new=AsyncMock()),
        patch.object(hw, "notify_agent_awaiting_approval", new=AsyncMock()),
        patch.object(hw, "_snapshot_agent_draft", new=AsyncMock(return_value=None)),
        patch.object(
            hw, "_ensure_draft_in_thread",
            new=AsyncMock(side_effect=lambda d, m, s: (d, s)),
        ),
        patch.object(hw, "get_settings", lambda: settings),
    ]


# ── _draft_sampling_overrides ────────────────────────────────────────────────


def _sampling_settings(**over):
    base = dict(
        draft_temperature=0.7,
        draft_top_p=0.8,
        draft_top_k=20,
        draft_presence_penalty=0.0,
        draft_reasoning_effort="none",
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_draft_sampling_overrides_carries_config_and_thinking_off():
    with patch.object(hw, "get_settings", lambda: _sampling_settings()):
        ov = hw._draft_sampling_overrides(disable_thinking=True)
    assert ov["temperature"] == 0.7
    assert ov["top_p"] == 0.8
    assert ov["presence_penalty"] == 0.0
    assert ov["extra_body"]["top_k"] == 20
    assert ov["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_draft_sampling_overrides_thinking_on_omits_flag():
    with patch.object(hw, "get_settings", lambda: _sampling_settings()):
        ov = hw._draft_sampling_overrides(disable_thinking=False)
    assert "chat_template_kwargs" not in ov["extra_body"]
    assert "reasoning_effort" not in ov


def test_draft_sampling_overrides_sets_reasoning_effort_for_local():
    """Ollama ignoriert ``enable_thinking``; wirksam ist nur ``reasoning_effort``."""
    with patch.object(hw, "get_settings", lambda: _sampling_settings()):
        ov = hw._draft_sampling_overrides(disable_thinking=True, local=True)
    assert ov["reasoning_effort"] == "none"


def test_draft_sampling_overrides_omits_reasoning_effort_for_cloud():
    """"none" ist kein Standard-OpenAI-Wert -- Cloud-Provider wuerden ihn ablehnen."""
    with patch.object(hw, "get_settings", lambda: _sampling_settings()):
        ov = hw._draft_sampling_overrides(disable_thinking=True, local=False)
    assert "reasoning_effort" not in ov
    assert ov["extra_body"]["chat_template_kwargs"] == {"enable_thinking": False}


def test_draft_sampling_overrides_empty_effort_omits_param():
    with patch.object(hw, "get_settings", lambda: _sampling_settings(draft_reasoning_effort="")):
        ov = hw._draft_sampling_overrides(disable_thinking=True, local=True)
    assert "reasoning_effort" not in ov


# ── _run_agent_sync (overrides gesetzt + zurueckgesetzt) ─────────────────────


class _FakeAgent:
    def __init__(self):
        self.request_overrides = "SENTINEL"
        self.seen = None

    def run_conversation(self, prompt, system_message=None):
        self.seen = self.request_overrides
        return "fertig"


def test_run_agent_sync_applies_and_restores_overrides():
    agent = _FakeAgent()
    out = hw._run_agent_sync(agent, "prompt", False, {"temperature": 0.7})
    assert out == "fertig"
    # Waehrend des Laufs waren die Overrides aktiv ...
    assert agent.seen == {"temperature": 0.7}
    # ... und danach ist der vorherige Zustand wiederhergestellt.
    assert agent.request_overrides == "SENTINEL"


def test_run_agent_sync_no_overrides_leaves_state():
    agent = _FakeAgent()
    hw._run_agent_sync(agent, "prompt", False)
    assert agent.request_overrides == "SENTINEL"
    assert agent.seen == "SENTINEL"


# ── Zwei-Pass im Post-Processing ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_two_pass_auto_reply_generates_draft_and_awaits_approval():
    """auto_reply ohne Draft im Loop -> Schreib-Pass liefert Entwurf -> awaiting_approval."""
    content = '{"triage_class": "auto_reply", "label": "Wichtig"}'
    job = SimpleNamespace(metadata_json={})
    ctx = _postprocess_patches(job=job, two_pass=True)
    gen = AsyncMock(return_value="DRAFT-NEU")
    with ctx[0], ctx[1] as create_task, ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], \
            patch.object(hw, "_generate_reply_draft", gen):
        status = await hw._post_process_triage(
            uuid.uuid4(), content, dict(_META), None, [], None
        )
    gen.assert_awaited_once()
    assert status == "awaiting_approval"
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_two_pass_draft_failure_falls_back_to_fyi_no_task():
    """Schreib-Pass liefert keinen Entwurf -> fail-closed fyi, kein Task."""
    content = '{"triage_class": "auto_reply", "label": "Wichtig"}'
    ctx = _postprocess_patches(two_pass=True)
    gen = AsyncMock(return_value=None)
    with ctx[0], ctx[1] as create_task, ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], \
            patch.object(hw, "_generate_reply_draft", gen):
        status = await hw._post_process_triage(
            uuid.uuid4(), content, dict(_META), None, [], None
        )
    gen.assert_awaited_once()
    assert status == "completed"
    create_task.assert_not_called()


@pytest.mark.asyncio
async def test_two_pass_replaces_stub_draft_from_classification_pass():
    """Ein Entwurf aus Pass 1 darf den Schreib-Pass NICHT unterdruecken.

    Regression zum Vorfall vom 03.08.2026: Der Klassifikations-Lauf hatte entgegen
    dem Prompt einen Platzhalter erstellt (nur Anrede und Gruss). Weil damals nur
    bei fehlender draft_id geschrieben wurde, ging der Platzhalter als fertiger
    Antwortvorschlag in die Freigabe. Jetzt gilt: der Schreib-Pass laeuft immer,
    und der Entwurf aus Pass 1 wird verworfen.
    """
    content = '{"triage_class": "auto_reply", "label": "Wichtig"}'
    job = SimpleNamespace(metadata_json={})
    ctx = _postprocess_patches(job=job, two_pass=True)
    gen = AsyncMock(return_value="DRAFT-NEU")
    delete = AsyncMock()
    with ctx[0], ctx[1], ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], \
            patch.object(hw, "_generate_reply_draft", gen), \
            patch.object(hw, "_delete_draft", delete):
        status = await hw._post_process_triage(
            uuid.uuid4(), content, dict(_META), "DRAFT-STUB",
            ["search_my_replies", "search_sender_history", "get_sender_profile"], None,
        )
    gen.assert_awaited_once()
    delete.assert_awaited_once_with("DRAFT-STUB")
    assert job.metadata_json["draft_id"] == "DRAFT-NEU"
    assert status == "awaiting_approval"


@pytest.mark.asyncio
async def test_stub_draft_discarded_when_write_pass_fails():
    """Scheitert der Schreib-Pass, ist der Pass-1-Entwurf keine Rueckfallebene."""
    content = '{"triage_class": "auto_reply", "label": "Wichtig"}'
    ctx = _postprocess_patches(two_pass=True)
    gen = AsyncMock(return_value=None)
    delete = AsyncMock()
    with ctx[0], ctx[1] as create_task, ctx[2], ctx[3], ctx[4], ctx[5], ctx[6], ctx[7], \
            patch.object(hw, "_generate_reply_draft", gen), \
            patch.object(hw, "_delete_draft", delete):
        status = await hw._post_process_triage(
            uuid.uuid4(), content, dict(_META), "DRAFT-STUB", [], None,
        )
    delete.assert_awaited_once_with("DRAFT-STUB")
    create_task.assert_not_called()
    assert status == "completed"


# ── _build_draft_prompt ──────────────────────────────────────────────────────


def _draft_prompt_patches():
    """Alle asynchronen Kontext-Builder von _build_draft_prompt neutralisieren."""
    return [
        patch.object(hw, "_style_skill_available", lambda: True),
        patch.object(hw, "_build_sender_style_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_build_rules_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_build_recall_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_build_style_anchor_block", new=AsyncMock(return_value="")),
        patch.object(hw, "_load_email_body_text", new=AsyncMock(return_value="Voller Mailtext.")),
    ]


@pytest.mark.asyncio
async def test_build_draft_prompt_forces_reply_to_id_and_calibration():
    p = _draft_prompt_patches()
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        prompt = await hw._build_draft_prompt(dict(_META))
    assert 'reply_to_id="M1"' in prompt
    assert "create_draft" in prompt
    assert 'search_my_replies("kunde@example.ch")' in prompt
    assert 'get_email("M1")' in prompt
    assert "skill_view(name='email-style')" in prompt
    # Der Schreib-Pass verlangt KEIN erneutes Klassifizieren.
    assert "Klassifiziere NICHT" in prompt
    # Voller Body ist eingebettet, Datum-Kontext vorhanden.
    assert "Voller Mailtext." in prompt
    assert "Heute:" in prompt
    # Eine blosse Grussformel ist ausdruecklich kein Entwurf.
    assert "Inhaltsteil zwischen Anrede und Schlussformel" in prompt


@pytest.mark.asyncio
async def test_build_draft_prompt_includes_briefing_from_parsed():
    p = _draft_prompt_patches()
    parsed = {"rationale": "Kundin bittet um Offerte", "label": "Wichtig"}
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        prompt = await hw._build_draft_prompt(dict(_META), parsed)
    assert "BRIEFING AUS DER KLASSIFIKATION" in prompt
    assert "Kundin bittet um Offerte" in prompt


@pytest.mark.asyncio
async def test_build_draft_prompt_injects_calendar_step_for_scheduling():
    p = _draft_prompt_patches()
    meta = dict(_META)
    meta["subject"] = "Terminanfrage nächste Woche"
    meta["body_preview"] = "Hast du Zeit für ein kurzes Meeting?"
    with p[0], p[1], p[2], p[3], p[4], p[5]:
        prompt = await hw._build_draft_prompt(meta)
    assert "find_free_slots" in prompt
    assert "innosmith.ch/termin" in prompt


def test_build_draft_briefing_empty_without_signals():
    assert hw._build_draft_briefing(None) == ""
    assert hw._build_draft_briefing({}) == ""


def test_looks_like_scheduling_detects_and_ignores():
    assert hw._looks_like_scheduling("Kurzer Call morgen?", "Wann passt es dir?")
    assert hw._looks_like_scheduling("Meeting", "")
    assert not hw._looks_like_scheduling("Rechnung Juni", "Anbei die Rechnung.")


def test_calendar_step_absent_for_non_scheduling():
    assert hw._build_calendar_draft_step("Rechnung", "Anbei die Rechnung") == ""

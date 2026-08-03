"""Tests für die Hermes-Gesamtintegration (Pakete A–D).

Abgedeckt:
- Chat-Agent-Prompt: Lern-Schicht (Regeln/Recall) + Task-Briefing + Verlaufsfenster
- Generischer Job-Prompt: Regel-Scope 'task', Datums-Kontext, Recall
- Tool-Scoping: reduzierte Triage-Allowlist (Core ohne web + graph + taskpilot)
- LLM-Override: request_overrides nur für echte lokale Modelle

Kein DB/Netz nötig — alle async Helfer sind gemockt.
"""

import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import app.services.hermes_worker as hw
from app.routers import chat as chat_router


# ---------------------------------------------------------------------------
# Paket C — Tool-Scoping
# ---------------------------------------------------------------------------

def test_triage_allowlist_excludes_web_and_irrelevant_servers():
    with patch.object(hw, "get_configured_server_keys", return_value=list(hw._KNOWN_MCP_SERVERS)):
        allow = hw.build_triage_allowlist()
    assert "web" not in allow
    assert "graph" in allow and "taskpilot" in allow
    # Fachlich irrelevante Server sind nicht enthalten.
    for server in ("pipedrive", "bexio", "toggl", "signa", "sandbox", "scripts"):
        assert server not in allow
    # Kern-Faehigkeiten bleiben erhalten.
    for core in ("skills", "memory", "clarify", "vision"):
        assert core in allow


def test_triage_allowlist_respects_configured_servers():
    with patch.object(hw, "get_configured_server_keys", return_value=["graph"]):
        allow = hw.build_triage_allowlist()
    assert "graph" in allow
    assert "taskpilot" not in allow


def test_triage_allowlist_excludes_graph_admin():
    """Der Triage-Agent darf den Outlook-Zustand nicht selbst mutieren.

    Die zustandsveraendernden Graph-Tools (Kategorie, Move, Versand, gelesen) liegen
    in der zweiten Registrierung ``graphAdmin``. Faellt sie in die Triage-Allowlist,
    kann das LLM wieder unvalidiert schreiben -- die Ursache der 80 erfundenen
    Kategorien und der Fehlmoves.
    """
    with patch.object(hw, "get_configured_server_keys", return_value=list(hw._KNOWN_MCP_SERVERS)):
        allow = hw.build_triage_allowlist()
    assert "graphAdmin" not in allow


def test_chat_gets_graph_admin_when_graph_selected():
    """Im Chat bleibt es eine Auswahl: «Graph» aktivieren heisst auch verschieben."""
    assert hw.expand_graph_admin(["graph"]) == ["graph", "graphAdmin"]
    assert hw.expand_graph_admin(["taskpilot"]) == ["taskpilot"]
    # Idempotent -- keine Duplikate bei erneutem Aufruf.
    assert hw.expand_graph_admin(["graph", "graphAdmin"]) == ["graph", "graphAdmin"]


def test_triage_tool_scoping_flag_default_on():
    from app.config import Settings

    assert Settings().triage_tool_scoping is True


# ---------------------------------------------------------------------------
# Recherche-Lauf (Pass 2a) — eigener Werkzeug-Umfang
# ---------------------------------------------------------------------------

def test_gather_allowlist_includes_the_capacity_system():
    """Der Sammel-Lauf teilte bisher die Allowlist der Klassifikation und sah damit
    nur Graph und die TaskPilot-DB.

    Stunden- und Verfuegbarkeitsfragen musste er darum aus dem Mailarchiv beantworten
    -- so entstand am 03.08.2026 eine Budgetzahl vom 02.07. als heutiger Stand.
    """
    with patch.object(hw, "get_configured_server_keys", return_value=list(hw._KNOWN_MCP_SERVERS)):
        allow = hw.build_gather_allowlist(wide=False)

    assert "capacity" in allow
    assert "graph" in allow and "taskpilot" in allow


def test_gather_allowlist_keeps_web_out_and_cannot_mutate_outlook():
    """Datenminimierung: keine E-Mail-Inhalte in externe Suchanfragen. Und der
    Recherche-Lauf soll lesen, nicht den Postfach-Zustand aendern."""
    with patch.object(hw, "get_configured_server_keys", return_value=list(hw._KNOWN_MCP_SERVERS)):
        for wide in (False, True):
            allow = hw.build_gather_allowlist(wide=wide)
            assert "web" not in allow
            assert "graphAdmin" not in allow


def test_wide_scope_adds_the_remaining_specialist_systems():
    """Leitprinzip Team-Modell: dieselben Fachsysteme wie der Mensch. Ob der
    zusaetzliche Prompt-Ballast die Werkzeugwahl verschlechtert, ist eine Messfrage
    -- darum ein Flag und kein fester Umfang."""
    with patch.object(hw, "get_configured_server_keys", return_value=list(hw._KNOWN_MCP_SERVERS)):
        narrow = hw.build_gather_allowlist(wide=False)
        wide = hw.build_gather_allowlist(wide=True)

    for server in ("toggl", "pipedrive", "bexio", "signa"):
        assert server not in narrow
        assert server in wide


def test_gather_allowlist_respects_configured_servers():
    """Ein nicht registrierter Server darf nicht in der Allowlist landen."""
    with patch.object(hw, "get_configured_server_keys", return_value=["graph"]):
        allow = hw.build_gather_allowlist(wide=True)

    assert "graph" in allow
    for server in ("capacity", "taskpilot", "bexio"):
        assert server not in allow


def test_system_map_only_names_tools_that_really_exist():
    """Beim Bauen stand «search_deals» in der Systemkarte -- Pipedrive kennt aber nur
    «search_crm». Ein erfundener Werkzeugname kostet eine Recherche-Runde und
    hinterlaesst einen Fehler im Trace, ohne dass jemand die Ursache sieht.

    Die Namen der Karte sind darum an die tatsaechlichen MCP-Definitionen gebunden.
    """
    import importlib.util
    import sys
    from pathlib import Path

    src = Path(__file__).resolve().parents[2]

    def tool_names(subdir: str, *libs: str) -> set[str]:
        for lib in libs:
            sys.path.insert(0, str(src / lib))
        spec = importlib.util.spec_from_file_location(f"probe_{subdir}", src / subdir / "server.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return {t.name for t in module.TOOLS}

    available = (
        tool_names("mcp-graph", "email-graph")
        | tool_names("mcp-pipedrive", "pipedrive")
        | tool_names("mcp-bexio", "bexio")
        | tool_names("mcp-toggl", "toggl")
        | tool_names("mcp-capacity", "capacity", "toggl")
    )

    from app.services.draft_prompt import GATHER_TASK_TEMPLATE

    # Nur snake_case: fett gesetzte deutsche Woerter («**zuerst**») sind keine Werkzeuge.
    pattern = r"\*\*([a-z]+(?:_[a-z]+)+)\*\*"
    named = set(re.findall(pattern, GATHER_TASK_TEMPLATE))
    with patch.object(hw, "get_settings", lambda: SimpleNamespace(draft_context_wide_tools=True)):
        named |= set(re.findall(pattern, hw._gather_extra_systems()))

    # ``semantic_search_documents`` liegt im taskpilot-Server, den dieser Test nicht
    # laedt (er braucht eine DB-Verbindung beim Import).
    named.discard("semantic_search_documents")

    assert named, "Systemkarte nennt keine Werkzeuge -- Test waere wirkungslos"
    assert named <= available, f"Unbekannte Werkzeuge in der Systemkarte: {named - available}"


def test_wide_tools_flag_defaults_off():
    """Der Kapazitaets-Server deckt den belegten Fehlerfall ab; der breite Umfang
    bleibt bis zur Messung aus."""
    from app.config import Settings

    assert Settings().draft_context_wide_tools is False


# ---------------------------------------------------------------------------
# Paket B — LLM-Override (request_overrides)
# ---------------------------------------------------------------------------

def test_local_override_builds_request_overrides():
    ov = hw._local_override_request(
        {"llm_override": "ollama/qwen3.6:8b"}, "qwen3.6:latest", disable_thinking=False,
    )
    assert ov == {"model": "qwen3.6:8b"}


def test_local_override_adds_thinking_switch():
    ov = hw._local_override_request(
        {"llm_override": "ollama/qwen3.6:8b"}, "qwen3.6:latest", disable_thinking=True,
    )
    assert ov["extra_body"] == {"chat_template_kwargs": {"enable_thinking": False}}
    # Ollama wertet nur ``reasoning_effort`` aus -- ohne den Parameter bleibt Thinking an.
    assert ov["reasoning_effort"] == "none"


def test_override_skipped_for_placeholder_cloud_and_same_model():
    assert hw._local_override_request({"llm_override": "hermes"}, "qwen3.6:latest", False) is None
    assert hw._local_override_request({"llm_override": "openai/gpt-5.5"}, "qwen3.6:latest", False) is None
    assert hw._local_override_request({"llm_override": "ollama/qwen3.6:latest"}, "qwen3.6:latest", False) is None
    assert hw._local_override_request({}, "qwen3.6:latest", False) is None


# ---------------------------------------------------------------------------
# Paket B — Generischer Job-Prompt (Scope 'task', Datum, Recall)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generic_prompt_uses_task_scope_date_and_recall():
    job = SimpleNamespace(
        id="job-1",
        job_type="task",
        task_id=None,
        metadata_json={"description": "Quartalsbericht vorbereiten"},
    )
    rules_mock = AsyncMock(return_value="\n[RULES-BLOCK]\n")
    recall_mock = AsyncMock(return_value="\n[RECALL-BLOCK]\n")
    with (
        patch.object(hw, "_load_projects_context", AsyncMock(return_value="## PROJEKTE")),
        patch.object(hw, "_build_rules_block", rules_mock),
        patch.object(hw, "_build_recall_block", recall_mock),
    ):
        prompt = await hw._build_generic_prompt(job)

    rules_mock.assert_awaited_once_with("task")
    recall_kwargs = recall_mock.await_args.kwargs
    assert recall_kwargs["job_type"] == "task"
    assert "Quartalsbericht" in recall_kwargs["query"]
    assert "[RULES-BLOCK]" in prompt
    assert "[RECALL-BLOCK]" in prompt
    assert "Heute ist" in prompt


@pytest.mark.asyncio
async def test_generic_prompt_chat_agent_keeps_chat_scope():
    job = SimpleNamespace(
        id="job-2",
        job_type="chat_agent",
        task_id=None,
        metadata_json={"description": "Frage beantworten"},
    )
    rules_mock = AsyncMock(return_value="")
    with (
        patch.object(hw, "_load_projects_context", AsyncMock(return_value="")),
        patch.object(hw, "_build_rules_block", rules_mock),
        patch.object(hw, "_build_recall_block", AsyncMock(return_value="")),
    ):
        await hw._build_generic_prompt(job)
    rules_mock.assert_awaited_once_with("chat")


# ---------------------------------------------------------------------------
# Paket A — Chat-Agent-Prompt (Lern-Schicht + Task-Briefing)
#
# Verlauf und angepinnte Dokumente laufen seit der Kontext-Vereinheitlichung
# NICHT mehr als Textblock im Prompt, sondern als echtes Message-Array über
# run_conversation(conversation_history=...) — siehe test_context_bridge.py.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_agent_prompt_injects_learning_layer_and_briefing():
    rules_mock = AsyncMock(return_value="\n[CHAT-RULES]\n")
    recall_mock = AsyncMock(return_value="\n[CHAT-RECALL]\n")
    briefing_mock = AsyncMock(return_value="\n[TASK-BRIEFING]\n")
    with (
        patch.object(hw, "_build_rules_block", rules_mock),
        patch.object(hw, "_build_recall_block", recall_mock),
        patch.object(chat_router, "_build_task_briefing", briefing_mock),
        patch.object(chat_router, "_load_agent_skills", return_value="(keine)"),
    ):
        prompt = await chat_router._build_agent_prompt(
            "Wie war das letzte Meeting?", task_id="t-1",
        )

    rules_mock.assert_awaited_once_with("chat")
    assert recall_mock.await_args.kwargs["query"].startswith("Wie war das letzte Meeting?")
    briefing_mock.assert_awaited_once_with("t-1")
    assert "[CHAT-RULES]" in prompt
    assert "[CHAT-RECALL]" in prompt
    assert "[TASK-BRIEFING]" in prompt


@pytest.mark.asyncio
async def test_chat_agent_prompt_has_no_inline_history_block():
    """Kein '## Verlauf'-Textblock mehr — Historie geht als Message-Array raus."""
    with (
        patch.object(hw, "_build_rules_block", AsyncMock(return_value="")),
        patch.object(hw, "_build_recall_block", AsyncMock(return_value="")),
        patch.object(chat_router, "_build_task_briefing", AsyncMock(return_value="")),
        patch.object(chat_router, "_load_agent_skills", return_value="(keine)"),
    ):
        prompt = await chat_router._build_agent_prompt("Frage")

    assert "## Verlauf" not in prompt
    assert "## Anfrage" in prompt
    assert "Frage" in prompt

#!/usr/bin/env python3
"""Library-Sonde für hermes-agent -- vor jedem Anheben der Fassung laufen lassen.

Prüft jede Stelle, an der TaskPilot und AI9 Hermes anfassen, auch die privaten.
Kein Netz, kein LLM-Aufruf -- nur Import, Init und Signatur.

Die erste Fassung prüfte nur die öffentliche Library-API und blieb beim Wechsel
0.18 -> 0.21 grün, obwohl Hermes die MCP-Werkzeuge von ``mcp_<server>_<tool>``
auf ``mcp__<server>__<tool>`` umbenannt und ``tool_search: auto`` zu «immer an»
gemacht hatte. Beides brach die Mail-Triage für drei Wochen, ohne dass ein Test
fiel. Darum prüft die Sonde auch die Konventionen, nicht nur die Signaturen.

Aufruf (venv mit ``pip install -e /pfad/zu/hermes-agent[mcp]``):

    .venv/bin/python scripts/eval/check_hermes_api.py
"""

from __future__ import annotations

import inspect
import sys

# Jeder Parameter, den TaskPilot (hermes_worker.py) oder AI9 (ai9/hermes.py) an
# ``AIAgent`` übergibt.
_AGENT_PARAMS = (
    "api_key", "api_mode", "base_url", "clarify_callback", "enabled_toolsets",
    "max_iterations", "model", "provider", "quiet_mode", "reasoning_callback",
    "request_overrides", "save_trajectories", "session_id", "skip_context_files",
    "skip_memory", "stream_delta_callback", "tool_complete_callback", "tool_delay",
    "tool_start_callback",
)

_fehler: list[str] = []


def _pruefe(bedingung: bool, meldung: str) -> None:
    if not bedingung:
        _fehler.append(meldung)


def _agent() -> None:
    from run_agent import AIAgent

    sig = inspect.signature(AIAgent.__init__)
    for name in _AGENT_PARAMS:
        _pruefe(name in sig.parameters, f"AIAgent.__init__ fehlt Parameter {name}")
    _pruefe(hasattr(AIAgent, "run_conversation"), "AIAgent.run_conversation fehlt")
    _pruefe(hasattr(AIAgent, "interrupt"), "AIAgent.interrupt fehlt (AI9-Abbruch)")

    agent = AIAgent(
        base_url="http://127.0.0.1:9/v1",
        api_key="probe",
        provider="custom",
        api_mode="chat_completions",
        model="probe",
        enabled_toolsets=[],
        skip_memory=True,
        skip_context_files=True,
        quiet_mode=True,
        max_iterations=1,
        request_overrides={"reasoning_effort": "none"},
    )
    _pruefe(hasattr(agent, "session_total_tokens"), "session_total_tokens fehlt nach Init")
    _pruefe(
        getattr(agent, "request_overrides", None) == {"reasoning_effort": "none"},
        "request_overrides nicht übernommen",
    )
    # Der Worker liest den Abbruch des Schleifenwächters aus dem Ergebnis.
    _pruefe(
        hasattr(agent, "_tool_guardrail_halt_decision"),
        "_tool_guardrail_halt_decision fehlt -- result['guardrail'] vermutlich umbenannt",
    )


def _worker_interna() -> None:
    from agent import session_persistence
    from agent.trajectory import save_trajectory

    _pruefe(
        hasattr(session_persistence, "_save_trajectory_to_file"),
        "agent.session_persistence._save_trajectory_to_file fehlt (Trajektorien-Shim)",
    )
    params = inspect.signature(save_trajectory).parameters
    _pruefe("filename" in params, "save_trajectory kennt 'filename' nicht mehr")

    from model_tools import get_tool_definitions

    params = inspect.signature(get_tool_definitions).parameters
    for name in ("enabled_toolsets", "quiet_mode"):
        _pruefe(name in params, f"get_tool_definitions fehlt Parameter {name}")

    from agent.tool_guardrails import ToolGuardrailDecision
    from tools.mcp_tool_discovery import discover_mcp_tools  # noqa: F401
    from tools.mcp_tool_lifecycle import shutdown_mcp_servers  # noqa: F401
    from tools.skills_tool import skill_view  # noqa: F401
    from tools.web_tools import _get_search_backend  # noqa: F401

    _pruefe(hasattr(ToolGuardrailDecision, "to_metadata"), "ToolGuardrailDecision.to_metadata fehlt")


def _konventionen() -> None:
    from tools.mcp_tool_schema import mcp_prefixed_tool_name

    name = mcp_prefixed_tool_name("graph", "get_email")
    _pruefe(
        name == "mcp__graph__get_email",
        f"MCP-Namensschema geändert: {name!r} -- app/services/tool_names.py und Skills prüfen",
    )

    from tools.tool_search import ToolSearchConfig

    _pruefe(
        ToolSearchConfig.from_raw({"enabled": "off"}).enabled == "off",
        "tool_search lässt sich nicht mehr mit enabled=off abschalten",
    )


def _ai9_interna() -> None:
    from hermes_cli.plugins import get_plugin_manager
    from hermes_constants import get_hermes_home, set_hermes_home_override  # noqa: F401

    _pruefe(
        isinstance(getattr(get_plugin_manager(), "_hooks", None), dict),
        "PluginManager._hooks ist kein dict mehr (ai9.hermes-Wächter)",
    )


def main() -> int:
    import warnings

    # Ein Kompatibilitätspfad ist ein Fehler mit Aufschub: Hermes kündigt ihn
    # mit Datum ab, und dann bricht der Import zur Laufzeit.
    warnings.filterwarnings("error", message=r"hermes plugin compat")
    for pruefung in (_agent, _worker_interna, _konventionen, _ai9_interna):
        try:
            pruefung()
        except Exception as exc:  # noqa: BLE001 - jede Stufe meldet für sich
            _fehler.append(f"{pruefung.__name__}: {type(exc).__name__}: {exc}")

    from importlib.metadata import version

    fassung = version("hermes-agent")
    if _fehler:
        print(f"FEHLER hermes-agent {fassung}:", file=sys.stderr)
        for meldung in _fehler:
            print(f"  - {meldung}", file=sys.stderr)
        return 1
    print(f"PROBE_OK hermes-agent {fassung}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

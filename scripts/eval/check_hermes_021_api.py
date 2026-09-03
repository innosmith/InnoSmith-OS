#!/usr/bin/env python3
"""Library-Sonde für hermes-agent 0.21.0 (Tag v2026.8.31).

Prüft die Felder, die TaskPilot und AI9 tatsächlich nutzen. Kein Netz, kein
LLM-Aufruf -- nur Init und Signatur. Scheitert die Sonde, gilt der
0.19-Fallback (Plan Versions-Audit), nicht ein «irgendwie uv».

Aufruf (Wegwerf-venv mit ``pip install -e /pfad/zu/hermes-agent[mcp]``):

    /pfad/zum/probe-venv/bin/python scripts/eval/check_hermes_021_api.py
"""

from __future__ import annotations

import inspect
import sys


def main() -> int:
    try:
        from run_agent import AIAgent
    except Exception as exc:
        print(f"FEHLER: from run_agent import AIAgent -- {exc}", file=sys.stderr)
        return 1

    sig = inspect.signature(AIAgent.__init__)
    for name in ("enabled_toolsets", "request_overrides"):
        if name not in sig.parameters:
            print(f"FEHLER: AIAgent.__init__ fehlt Parameter {name}", file=sys.stderr)
            return 1

    if not hasattr(AIAgent, "run_conversation"):
        print("FEHLER: AIAgent.run_conversation fehlt", file=sys.stderr)
        return 1

    try:
        from tools.skills_tool import skill_view  # noqa: F401
    except Exception as exc:
        print(f"FEHLER: skill_view nicht importierbar -- {exc}", file=sys.stderr)
        return 1

    try:
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
    except Exception as exc:
        print(f"FEHLER: AIAgent-Init -- {exc}", file=sys.stderr)
        return 1

    if not hasattr(agent, "session_total_tokens"):
        print("FEHLER: session_total_tokens fehlt nach Init", file=sys.stderr)
        return 1
    if getattr(agent, "request_overrides", None) != {"reasoning_effort": "none"}:
        print("FEHLER: request_overrides nicht übernommen", file=sys.stderr)
        return 1

    print("PROBE_OK hermes-agent 0.21 Library-API")
    print("  AIAgent, run_conversation, session_total_tokens,")
    print("  request_overrides, enabled_toolsets, skill_view")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

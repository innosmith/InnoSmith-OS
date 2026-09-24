"""Namen der MCP-Werkzeuge, wie Hermes sie registriert.

Hermes bildet den Namen, unter dem das Modell ein MCP-Werkzeug aufruft, aus
Server-Schlüssel und Werkzeugname. Diese Konvention gehört Hermes und wird hier
**abgeleitet, nie abgeschrieben**: Hermes 0.21 stellte von ``mcp_<server>_<tool>``
auf ``mcp__<server>__<tool>`` um, und die abgeschriebenen Namen in Prompt, Skills
und Callback-Konstanten liefen ab dem 03.09.2026 drei Wochen lang ins Leere. Rund
jede fünfte Mail-Triage brach am Schleifenwächter ab, und kein Test fiel, weil die
Tests dieselbe Abschrift trugen.

Hermes kürzt überlange Namen zudem mit einem Hash-Suffix auf 64 Zeichen -- auch
das kann eine Abschrift nicht nachbilden.
"""

from __future__ import annotations

from tools.mcp_tool_schema import MCP_TOOL_NAME_PREFIX, mcp_prefixed_tool_name

_DELIM = "__"


def mcp_tool(server: str, tool: str) -> str:
    """Name, unter dem Hermes das Werkzeug ``tool`` des Servers ``server`` führt."""
    return mcp_prefixed_tool_name(server, tool)


def mcp_server_of(name: str) -> str | None:
    """Server-Schlüssel eines MCP-Werkzeugnamens, ``None`` bei anderen Werkzeugen."""
    if not name.startswith(MCP_TOOL_NAME_PREFIX):
        return None
    server, sep, _tool = name[len(MCP_TOOL_NAME_PREFIX):].partition(_DELIM)
    return server if sep and server else None

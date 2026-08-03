"""Rauchtest des Kapazitäts-MCP-Servers gegen die laufende Datenbank.

Ruft dieselben Funktionen auf, die der Agent über MCP erreicht, und gibt die
Rohantwort aus. Zweck: prüfen, dass Plan und Ist gegen echte Daten plausibel sind,
bevor der Agent damit E-Mails schreibt.

    python scripts/smoke_capacity_mcp.py [YYYY-MM] [Kundenfilter]
"""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "mcp-capacity"))

import server  # noqa: E402


async def main() -> None:
    month = sys.argv[1] if len(sys.argv) > 1 else None
    client = sys.argv[2] if len(sys.argv) > 2 else None

    args: dict = {}
    if month:
        args["month"] = month
    if client:
        args["client"] = client

    for tool, payload in (("get_capacity_overview", args), ("get_absences", {})):
        result = await server.call_tool(tool, payload)
        print(f"\n=== {tool} {payload or ''} ===")
        print(result[0].text if result else "(leer)")


if __name__ == "__main__":
    asyncio.run(main())

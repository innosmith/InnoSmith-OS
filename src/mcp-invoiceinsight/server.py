"""MCP-Server (stdio) für InvoiceInsight Kreditoren-Analyse.

Proxy zum HTTP-MCP von InvoiceInsight — exponiert die wichtigsten Tools
und Ressourcen als stdio-MCP, damit der Hermes-Agent darauf zugreifen kann.
"""

import asyncio
import json
import logging
import os
import sys

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

# Pfad zum Backend-`services`-Paket robust auflösen: im Dev-Layout liegt es unter
# ``../backend/app`` (src/backend/app), im Docker-Image flach unter ``../app`` (/app/app).
# Wir nehmen das erste Verzeichnis, das ``services/invoiceinsight_client.py`` enthält.
_here = os.path.dirname(os.path.abspath(__file__))
for _candidate in (
    os.path.join(_here, "..", "backend", "app"),  # Dev: src/backend/app
    os.path.join(_here, "..", "app"),             # Docker: /app/app
):
    if os.path.isfile(os.path.join(_candidate, "services", "invoiceinsight_client.py")):
        sys.path.insert(0, os.path.abspath(_candidate))
        break
from services.invoiceinsight_client import InvoiceInsightClient  # noqa: E402

logger = logging.getLogger("mcp_invoiceinsight")

TOOLS = [
    Tool(
        name="get_kpis",
        description=(
            "Finanz-KPIs aus InvoiceInsight laden: Gesamtkosten, Anzahl Rechnungen, "
            "Durchschnitt, grösster Kreditor. Optional nach Jahr filtern."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "year_from": {"type": "integer", "description": "Ab welchem Jahr"},
                "year_to": {"type": "integer", "description": "Bis welches Jahr"},
            },
        },
    ),
    Tool(
        name="get_cost_distribution",
        description="Kostenverteilung nach Kategorien (Lieferanten, Kostenarten).",
        inputSchema={
            "type": "object",
            "properties": {
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="get_yoy_comparison",
        description="Jahr-zu-Jahr-Vergleich der Kreditorenkosten.",
        inputSchema={
            "type": "object",
            "properties": {
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="get_renewal_calendar",
        description=(
            "Anstehende Vertragsverlängerungen und Zahlungen. "
            "Zeigt fällige Rechnungen der nächsten Monate."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "months_ahead": {"type": "integer", "default": 3, "description": "Monate voraus"},
            },
        },
    ),
    Tool(
        name="get_anomalies",
        description="Auffällige Rechnungen und Anomalien in den Kreditorendaten.",
        inputSchema={
            "type": "object",
            "properties": {
                "year_from": {"type": "integer"},
                "year_to": {"type": "integer"},
            },
        },
    ),
    Tool(
        name="get_cashflow_forecast",
        description="Cashflow-Prognose basierend auf bekannten Kreditoren-Rechnungen.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_vendor_overview",
        description="Übersicht aller Lieferanten (Top-Kreditoren, Volumen, Anzahl Rechnungen).",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_upcoming_payments",
        description="Nächste anstehende Zahlungen mit Betrag, Fälligkeitsdatum und Lieferant.",
        inputSchema={
            "type": "object",
            "properties": {
                "days_ahead": {"type": "integer", "default": 30},
            },
        },
    ),
    Tool(
        name="get_invoice_details",
        description="Details einer einzelnen Kreditorenrechnung (PDF-Pfad, Positionen, Betrag).",
        inputSchema={
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string", "description": "Rechnungs-ID"},
            },
            "required": ["invoice_id"],
        },
    ),
]

_client: InvoiceInsightClient | None = None


def _get_client() -> InvoiceInsightClient:
    global _client
    if _client is None:
        url = os.environ.get("TP_INVOICEINSIGHT_URL", "http://127.0.0.1:8055/mcp")
        api_key = os.environ.get("TP_INVOICEINSIGHT_API_KEY", "")
        if not api_key:
            logger.warning("TP_INVOICEINSIGHT_API_KEY ist leer — Verbindung wird vermutlich fehlschlagen")
        _client = InvoiceInsightClient(url=url, api_key=api_key)
    return _client


def _json_response(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, default=str, ensure_ascii=False))]


async def list_tools() -> list[Tool]:
    return TOOLS


async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    c = _get_client()

    try:
        if name == "get_kpis":
            result = await c.get_kpis(
                year_from=arguments.get("year_from"),
                year_to=arguments.get("year_to"),
            )
            return _json_response(result)

        if name == "get_cost_distribution":
            result = await c.get_cost_distribution(
                year_from=arguments.get("year_from"),
                year_to=arguments.get("year_to"),
            )
            return _json_response(result)

        if name == "get_yoy_comparison":
            result = await c.get_yoy_comparison(
                year_from=arguments.get("year_from"),
                year_to=arguments.get("year_to"),
            )
            return _json_response(result)

        if name == "get_renewal_calendar":
            result = await c.get_renewal_calendar(
                months_ahead=arguments.get("months_ahead"),
            )
            return _json_response(result)

        if name == "get_anomalies":
            result = await c.get_anomalies(
                year_from=arguments.get("year_from"),
                year_to=arguments.get("year_to"),
            )
            return _json_response(result)

        if name == "get_cashflow_forecast":
            result = await c.get_cashflow_forecast()
            return _json_response(result)

        if name == "get_vendor_overview":
            result = await c.get_vendor_overview()
            return _json_response(result)

        if name == "get_upcoming_payments":
            result = await c.call_tool(
                "get_upcoming_payments",
                {"days_ahead": arguments.get("days_ahead", 30)},
            )
            return _json_response(result)

        if name == "get_invoice_details":
            result = await c.call_tool(
                "get_invoice_details",
                {"invoice_id": arguments["invoice_id"]},
            )
            return _json_response(result)

    except Exception as e:
        logger.exception("InvoiceInsight Tool-Fehler: %s", name)
        return _json_response({"error": str(e), "tool": name})

    return _json_response({"error": f"Unbekanntes Tool: {name}"})



async def _on_list_tools(_ctx, _params) -> ListToolsResult:
    return ListToolsResult(tools=await list_tools())


async def _on_call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    content = await call_tool(params.name, params.arguments or {})
    return CallToolResult(content=content)


server = Server("taskpilot-invoiceinsight", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

"""MCP-Server für Bexio Buchhaltung.

Der Hermes-Agent kann diesen Server als Tool nutzen, um Kontakte, Aufträge,
Rechnungen und Projekte in Bexio zu lesen und anzulegen.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bexio"))
from bexio_client import BexioClient, BexioConfig  # noqa: E402

logger = logging.getLogger("mcp_bexio")

TOOLS = [
    Tool(
        name="test_connection",
        description="Bexio Verbindung testen und aktiven Benutzer anzeigen.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_contacts",
        description="Kontakte in Bexio auflisten (Kunden, Lieferanten).",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    Tool(
        name="get_contact",
        description="Details eines Bexio-Kontakts laden (Adresse, E-Mail, Typ).",
        inputSchema={
            "type": "object",
            "properties": {"contact_id": {"type": "integer"}},
            "required": ["contact_id"],
        },
    ),
    Tool(
        name="search_contact",
        description=(
            "Bexio-Kontakt nach Name oder E-Mail suchen. Teiltreffer genügen "
            "('GSW' findet 'GSW Treuhand AG'), gesucht wird in Firma/Nachname und "
            "Vorname. Genau eines von 'name' oder 'email' MUSS gesetzt sein.\n"
            "Beispiel: {\"name\": \"GSW\"}\n"
            "Für Umsatz- oder Summenfragen NICHT dieses Tool, sondern den Datenraum."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name oder Namensteil"},
                "email": {"type": "string", "description": "E-Mail (alternativ zum Namen)"},
            },
            "anyOf": [{"required": ["name"]}, {"required": ["email"]}],
        },
    ),
    Tool(
        name="create_contact",
        description="Neuen Kontakt in Bexio anlegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "name_1": {"type": "string", "description": "Firmenname oder Nachname"},
                "name_2": {"type": "string", "description": "Vorname (optional)"},
                "contact_type_id": {"type": "integer", "description": "1=Firma, 2=Person", "default": 1},
                "mail": {"type": "string", "description": "E-Mail-Adresse"},
                "address": {"type": "string"},
                "postcode": {"type": "string"},
                "city": {"type": "string"},
                "country_id": {"type": "integer", "description": "Land-ID (1=Schweiz)", "default": 1},
            },
            "required": ["name_1", "contact_type_id"],
        },
    ),
    Tool(
        name="list_orders",
        description="Aufträge (kb_order) in Bexio auflisten. Optional nach Kontakt filtern.",
        inputSchema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer", "description": "Kontakt-ID zum Filtern"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="get_order",
        description="Details eines Bexio-Auftrags laden.",
        inputSchema={
            "type": "object",
            "properties": {"order_id": {"type": "integer"}},
            "required": ["order_id"],
        },
    ),
    Tool(
        name="list_projects",
        description="Projekte in Bexio auflisten.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 50}},
        },
    ),
    Tool(
        name="get_project",
        description="Details eines Bexio-Projekts laden.",
        inputSchema={
            "type": "object",
            "properties": {"project_id": {"type": "integer"}},
            "required": ["project_id"],
        },
    ),
    Tool(
        name="list_invoices",
        description=(
            "Eine SEITE Rechnungen (kb_invoice) auflisten — nicht den ganzen Bestand. "
            "Für Umsatz, Summen oder Ranglisten ist dieses Tool ungeeignet: es liefert "
            "nur einen Ausschnitt. Solche Fragen über den Datenraum beantworten "
            "(datenraum_katalog, dann execute_code über /daten/bexio_rechnungen.parquet)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer", "description": "Kontakt-ID; läuft intern über die Suche"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    Tool(
        name="get_invoice",
        description="Details einer einzelnen Bexio-Rechnung laden (Positionen, Betrag, Status).",
        inputSchema={
            "type": "object",
            "properties": {"invoice_id": {"type": "integer"}},
            "required": ["invoice_id"],
        },
    ),
    Tool(
        name="search_invoices",
        description=(
            "Rechnungen filtern nach Kunde, Status und/oder Zeitraum. Dies ist der "
            "EINZIGE Weg, Rechnungen eines Kunden einzugrenzen.\n"
            "Beispiel: {\"contact_id\": 42, \"status\": \"offen\"}"
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "contact_id": {"type": "integer", "description": "Kontakt-ID des Kunden"},
                "status": {
                    "type": "string",
                    "enum": ["entwurf", "offen", "bezahlt"],
                    "description": "Rechnungsstatus",
                },
                "from_date": {"type": "string", "description": "Ab-Datum (YYYY-MM-DD)"},
                "to_date": {"type": "string", "description": "Bis-Datum (YYYY-MM-DD)"},
            },
        },
    ),
    Tool(
        name="get_journal",
        description=(
            "Buchhaltungsjournal laden (alle Buchungen im Zeitraum). "
            "Liefert debit/credit Konto, Betrag, Datum, Beschreibung."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "from_date": {"type": "string", "description": "Start-Datum (YYYY-MM-DD)"},
                "to_date": {"type": "string", "description": "End-Datum (YYYY-MM-DD)"},
            },
            "required": ["from_date", "to_date"],
        },
    ),
    Tool(
        name="list_accounts",
        description="Kontenplan (Chart of Accounts) aus Bexio laden.",
        inputSchema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "default": 500},
            },
        },
    ),
    Tool(
        name="list_bank_accounts",
        description="Alle Bankkonten mit aktuellem Saldo aus Bexio laden.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="get_business_years",
        description="Geschäftsjahre aus Bexio laden (Start, Ende, Status).",
        inputSchema={"type": "object", "properties": {}},
    ),
]

_client: BexioClient | None = None


def _get_client() -> BexioClient:
    global _client
    if _client is None:
        _client = BexioClient(BexioConfig.from_env())
    return _client


def _json_response(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, default=str, ensure_ascii=False))]


async def list_tools() -> list[Tool]:
    return TOOLS


async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    c = _get_client()

    if name == "test_connection":
        result = await c.test_connection()
        return _json_response(result)

    if name == "list_contacts":
        result = await c.list_contacts(
            limit=arguments.get("limit", 50),
            offset=arguments.get("offset", 0),
        )
        compact = [
            {"id": ct.get("id"), "name_1": ct.get("name_1"), "name_2": ct.get("name_2"),
             "mail": ct.get("mail"), "contact_type_id": ct.get("contact_type_id")}
            for ct in result
        ]
        return _json_response(compact)

    if name == "get_contact":
        result = await c.get_contact(arguments["contact_id"])
        return _json_response(result)

    if name == "search_contact":
        if (arguments.get("email") or "").strip():
            result = await c.search_contact_by_email(arguments["email"])
        elif (arguments.get("name") or "").strip():
            result = await c.search_contact_by_name(arguments["name"])
        else:
            return _json_response({
                "error": "search_contact braucht 'name' ODER 'email'",
                "beispiel": {"name": "GSW"},
            })
        compact = [
            {"id": ct.get("id"), "name_1": ct.get("name_1"), "name_2": ct.get("name_2"),
             "mail": ct.get("mail")}
            for ct in result
        ]
        return _json_response(compact)

    if name == "create_contact":
        result = await c.create_contact(arguments)
        return _json_response(result)

    if name == "list_orders":
        result = await c.list_orders(
            contact_id=arguments.get("contact_id"),
            limit=arguments.get("limit", 50),
        )
        compact = [
            {"id": o.get("id"), "title": o.get("title"), "contact_id": o.get("contact_id"),
             "total": o.get("total"), "status": o.get("kb_item_status_id")}
            for o in result
        ]
        return _json_response(compact)

    if name == "get_order":
        result = await c.get_order(arguments["order_id"])
        return _json_response(result)

    if name == "list_projects":
        result = await c.list_projects(limit=arguments.get("limit", 50))
        compact = [
            {"id": p.get("id"), "name": p.get("name"), "contact_id": p.get("contact_id"),
             "status_id": p.get("pr_state_id")}
            for p in result
        ]
        return _json_response(compact)

    if name == "get_project":
        result = await c.get_project(arguments["project_id"])
        return _json_response(result)

    if name == "list_invoices":
        result = await c.list_invoices(
            contact_id=arguments.get("contact_id"),
            limit=arguments.get("limit", 50),
            offset=arguments.get("offset", 0),
        )
        compact = [
            {"id": inv.get("id"), "document_nr": inv.get("document_nr"),
             "title": inv.get("title"), "contact_id": inv.get("contact_id"),
             "total": inv.get("total"), "total_net": inv.get("total_net"),
             "is_valid_from": inv.get("is_valid_from"),
             "is_valid_to": inv.get("is_valid_to"),
             "status": inv.get("kb_item_status_id")}
            for inv in result
        ]
        return _json_response(compact)

    if name == "get_invoice":
        result = await c.get_invoice(arguments["invoice_id"])
        return _json_response(result)

    if name == "search_invoices":
        try:
            result = await c.search_invoices(
                contact_id=arguments.get("contact_id"),
                status=arguments.get("status"),
                from_date=arguments.get("from_date"),
                to_date=arguments.get("to_date"),
            )
        except ValueError as exc:
            return _json_response({"error": str(exc)})
        compact = [
            {"id": inv.get("id"), "document_nr": inv.get("document_nr"),
             "title": inv.get("title"), "contact_id": inv.get("contact_id"),
             "total": inv.get("total"), "is_valid_from": inv.get("is_valid_from"),
             "status": inv.get("kb_item_status_id")}
            for inv in result
        ]
        return _json_response(compact)

    if name == "get_journal":
        result = await c.get_journal(
            from_date=arguments["from_date"],
            to_date=arguments["to_date"],
        )
        return _json_response(result)

    if name == "list_accounts":
        result = await c.list_accounts(limit=arguments.get("limit", 500))
        compact = [
            {"id": a.get("id"), "account_no": a.get("account_no"),
             "name": a.get("name"), "account_type": a.get("account_type")}
            for a in result
        ]
        return _json_response(compact)

    if name == "list_bank_accounts":
        result = await c.list_bank_accounts()
        return _json_response(result)

    if name == "get_business_years":
        result = await c.get_business_years()
        return _json_response(result)

    return _json_response({"error": f"Unbekanntes Tool: {name}"})



async def _on_list_tools(_ctx, _params) -> ListToolsResult:
    return ListToolsResult(tools=await list_tools())


async def _on_call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    content = await call_tool(params.name, params.arguments or {})
    return CallToolResult(content=content)


server = Server("taskpilot-bexio", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

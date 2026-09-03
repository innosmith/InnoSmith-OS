"""MCP-Server für Pipedrive CRM-Zugriff.

Der Hermes-Agent kann diesen Server als Tool nutzen, um Deals, Leads, Kontakte,
Aktivitaeten und Notizen in Pipedrive zu lesen und zu schreiben.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "pipedrive"))
from pipedrive_client import PipedriveClient, PipedriveConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_pipedrive")

TOOLS = [
    Tool(
        name="list_deals",
        description="Pipedrive Deals auflisten. Optional nach Pipeline, Stage oder Status filtern.",
        inputSchema={
            "type": "object",
            "properties": {
                "pipeline_id": {"type": "integer", "description": "Pipeline-ID zum Filtern"},
                "stage_id": {"type": "integer", "description": "Stage-ID zum Filtern"},
                "status": {"type": "string", "enum": ["open", "won", "lost", "deleted"], "default": "open"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="get_deal",
        description="Details eines einzelnen Pipedrive-Deals laden (inkl. Person, Organisation, Wert, Stage).",
        inputSchema={
            "type": "object",
            "properties": {"deal_id": {"type": "integer", "description": "Deal-ID"}},
            "required": ["deal_id"],
        },
    ),
    Tool(
        name="create_deal",
        description="Neuen Deal in Pipedrive anlegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Deal-Titel"},
                "person_id": {"type": "integer", "description": "Verknuepfte Person-ID"},
                "org_id": {"type": "integer", "description": "Verknuepfte Organisations-ID"},
                "value": {"type": "number", "description": "Deal-Wert"},
                "currency": {"type": "string", "default": "CHF"},
                "pipeline_id": {"type": "integer"},
                "stage_id": {"type": "integer"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="update_deal",
        description="Bestehenden Deal aktualisieren (Titel, Stage, Wert, Status etc.).",
        inputSchema={
            "type": "object",
            "properties": {
                "deal_id": {"type": "integer"},
                "title": {"type": "string"},
                "stage_id": {"type": "integer"},
                "status": {"type": "string", "enum": ["open", "won", "lost"]},
                "value": {"type": "number"},
            },
            "required": ["deal_id"],
        },
    ),
    Tool(
        name="list_leads",
        description="Pipedrive Leads auflisten.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    ),
    Tool(
        name="create_lead",
        description="Neuen Lead in Pipedrive anlegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "person_id": {"type": "integer"},
                "organization_id": {"type": "integer"},
            },
            "required": ["title"],
        },
    ),
    Tool(
        name="update_lead",
        description="Bestehenden Lead aktualisieren.",
        inputSchema={
            "type": "object",
            "properties": {
                "lead_id": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["lead_id"],
        },
    ),
    Tool(
        name="list_persons",
        description="Kontakte (Persons) in Pipedrive auflisten.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    ),
    Tool(
        name="get_person",
        description="Details einer Person laden (Kontaktdaten, verknuepfte Deals).",
        inputSchema={
            "type": "object",
            "properties": {"person_id": {"type": "integer"}},
            "required": ["person_id"],
        },
    ),
    Tool(
        name="create_person",
        description="Neuen Kontakt in Pipedrive anlegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
                "org_id": {"type": "integer"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="update_person",
        description="Bestehenden Kontakt aktualisieren.",
        inputSchema={
            "type": "object",
            "properties": {
                "person_id": {"type": "integer"},
                "name": {"type": "string"},
                "email": {"type": "string"},
                "phone": {"type": "string"},
            },
            "required": ["person_id"],
        },
    ),
    Tool(
        name="list_organizations",
        description="Organisationen in Pipedrive auflisten.",
        inputSchema={
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 20}},
        },
    ),
    Tool(
        name="get_organization",
        description="Details einer Organisation laden.",
        inputSchema={
            "type": "object",
            "properties": {"org_id": {"type": "integer"}},
            "required": ["org_id"],
        },
    ),
    Tool(
        name="list_activities",
        description="Pipedrive Aktivitaeten auflisten. Kann nach erledigt/offen, Deal oder Person filtern.",
        inputSchema={
            "type": "object",
            "properties": {
                "done": {"type": "boolean", "description": "true=erledigt, false=offen"},
                "deal_id": {"type": "integer"},
                "person_id": {"type": "integer"},
                "limit": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="create_activity",
        description="Neue Aktivitaet (Aufgabe, Anruf, Meeting) in Pipedrive anlegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Titel der Aktivitaet"},
                "type": {"type": "string", "default": "task", "description": "task, call, meeting, email, etc."},
                "deal_id": {"type": "integer"},
                "person_id": {"type": "integer"},
                "due_date": {"type": "string", "description": "Faelligkeitsdatum (YYYY-MM-DD)"},
                "note": {"type": "string"},
            },
            "required": ["subject"],
        },
    ),
    Tool(
        name="update_activity",
        description="Bestehende Aktivitaet aktualisieren.",
        inputSchema={
            "type": "object",
            "properties": {
                "activity_id": {"type": "integer"},
                "subject": {"type": "string"},
                "done": {"type": "boolean"},
                "due_date": {"type": "string"},
            },
            "required": ["activity_id"],
        },
    ),
    Tool(
        name="mark_activity_done",
        description="Aktivitaet als erledigt markieren.",
        inputSchema={
            "type": "object",
            "properties": {"activity_id": {"type": "integer"}},
            "required": ["activity_id"],
        },
    ),
    Tool(
        name="list_pipelines",
        description="Alle Sales-Pipelines auflisten.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_stages",
        description="Stages (Phasen) einer Pipeline auflisten.",
        inputSchema={
            "type": "object",
            "properties": {"pipeline_id": {"type": "integer"}},
        },
    ),
    Tool(
        name="add_note",
        description="Notiz an einen Deal, eine Person oder Organisation anhaengen.",
        inputSchema={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Notiz-Text (HTML erlaubt)"},
                "deal_id": {"type": "integer"},
                "person_id": {"type": "integer"},
                "org_id": {"type": "integer"},
            },
            "required": ["content"],
        },
    ),
    Tool(
        name="search_crm",
        description=(
            "Volltextsuche über Deals, Personen und Organisationen in Pipedrive. "
            "Nutze EINEN kurzen Suchbegriff (z. B. Name, Firma, E-Mail) — keine ganzen "
            "Themensätze. item_types IMMER im SINGULAR (deal,person,organization)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "term": {"type": "string", "description": "Kurzer Suchbegriff (Name/Firma/E-Mail), nicht mehrere Themen"},
                "item_types": {
                    "type": "string",
                    "default": "deal,person,organization",
                    "description": "Kommagetrennt, SINGULAR: deal, person, organization, lead, product, project",
                },
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["term"],
        },
    ),
]

_client: PipedriveClient | None = None


def _get_client() -> PipedriveClient:
    global _client
    if _client is None:
        _client = PipedriveClient(PipedriveConfig.from_env())
    return _client


def _json_response(data) -> list[TextContent]:
    return [TextContent(type="text", text=json.dumps(data, default=str, ensure_ascii=False))]


async def list_tools() -> list[Tool]:
    return TOOLS


async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    c = _get_client()

    if name == "list_deals":
        result = await c.list_deals(
            pipeline_id=arguments.get("pipeline_id"),
            stage_id=arguments.get("stage_id"),
            status=arguments.get("status", "open"),
            limit=arguments.get("limit", 20),
        )
        compact = [
            {"id": d.get("id"), "title": d.get("title"), "status": d.get("status"),
             "value": d.get("value"), "currency": d.get("currency"),
             "stage_id": d.get("stage_id"), "person_name": d.get("person_name"),
             "org_name": d.get("org_name")}
            for d in result
        ]
        return _json_response(compact)

    if name == "get_deal":
        result = await c.get_deal(arguments["deal_id"])
        return _json_response(result)

    if name == "create_deal":
        title = arguments.pop("title")
        result = await c.create_deal(title, **arguments)
        return _json_response(result)

    if name == "update_deal":
        deal_id = arguments.pop("deal_id")
        result = await c.update_deal(deal_id, **arguments)
        return _json_response(result)

    if name == "list_leads":
        result = await c.list_leads(limit=arguments.get("limit", 20))
        compact = [
            {"id": d.get("id"), "title": d.get("title"),
             "person_id": d.get("person_id"), "organization_id": d.get("organization_id")}
            for d in result
        ]
        return _json_response(compact)

    if name == "create_lead":
        title = arguments.pop("title")
        result = await c.create_lead(title, **arguments)
        return _json_response(result)

    if name == "update_lead":
        lead_id = arguments.pop("lead_id")
        result = await c.update_lead(lead_id, **arguments)
        return _json_response(result)

    if name == "list_persons":
        result = await c.list_persons(limit=arguments.get("limit", 20))
        compact = []
        for p in result:
            emails = p.get("email", [])
            email_str = emails[0].get("value", "") if isinstance(emails, list) and emails else ""
            compact.append({"id": p.get("id"), "name": p.get("name"), "email": email_str,
                            "org_name": p.get("org_name")})
        return _json_response(compact)

    if name == "get_person":
        result = await c.get_person(arguments["person_id"])
        return _json_response(result)

    if name == "create_person":
        n = arguments.pop("name")
        result = await c.create_person(n, **arguments)
        return _json_response(result)

    if name == "update_person":
        pid = arguments.pop("person_id")
        result = await c.update_person(pid, **arguments)
        return _json_response(result)

    if name == "list_organizations":
        result = await c.list_organizations(limit=arguments.get("limit", 20))
        compact = [{"id": o.get("id"), "name": o.get("name")} for o in result]
        return _json_response(compact)

    if name == "get_organization":
        result = await c.get_organization(arguments["org_id"])
        return _json_response(result)

    if name == "list_activities":
        result = await c.list_activities(
            done=arguments.get("done"),
            deal_id=arguments.get("deal_id"),
            person_id=arguments.get("person_id"),
            limit=arguments.get("limit", 20),
        )
        compact = [
            {"id": a.get("id"), "subject": a.get("subject"), "type": a.get("type"),
             "done": a.get("done"), "due_date": a.get("due_date"),
             "deal_id": a.get("deal_id"), "person_name": a.get("person_name")}
            for a in result
        ]
        return _json_response(compact)

    if name == "create_activity":
        subj = arguments.pop("subject")
        atype = arguments.pop("type", "task")
        result = await c.create_activity(subj, atype, **arguments)
        return _json_response(result)

    if name == "update_activity":
        aid = arguments.pop("activity_id")
        result = await c.update_activity(aid, **arguments)
        return _json_response(result)

    if name == "mark_activity_done":
        result = await c.mark_activity_done(arguments["activity_id"])
        return _json_response(result)

    if name == "list_pipelines":
        result = await c.list_pipelines()
        compact = [{"id": p.get("id"), "name": p.get("name"), "active": p.get("active")} for p in result]
        return _json_response(compact)

    if name == "list_stages":
        result = await c.list_stages(arguments.get("pipeline_id"))
        compact = [{"id": s.get("id"), "name": s.get("name"), "pipeline_id": s.get("pipeline_id"),
                     "order_nr": s.get("order_nr")} for s in result]
        return _json_response(compact)

    if name == "add_note":
        content = arguments.pop("content")
        result = await c.create_note(content, **arguments)
        return _json_response(result)

    if name == "search_crm":
        result = await c.search_items(
            arguments["term"],
            arguments.get("item_types", "deal,person,organization"),
            arguments.get("limit", 10),
        )
        return _json_response(result)

    return _json_response({"error": f"Unbekanntes Tool: {name}"})



async def _on_list_tools(_ctx, _params) -> ListToolsResult:
    return ListToolsResult(tools=await list_tools())


async def _on_call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    content = await call_tool(params.name, params.arguments or {})
    return CallToolResult(content=content)


server = Server("taskpilot-pipedrive", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

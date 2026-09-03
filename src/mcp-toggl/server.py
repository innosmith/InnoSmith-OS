"""MCP-Server für Toggl Track Zeiterfassung.

Der Hermes-Agent kann diesen Server als Tool nutzen, um Workspaces, Clients,
Projects und Time Entries in Toggl Track zu lesen und anzulegen.
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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "toggl"))
from toggl_client import TogglClient, TogglConfig  # noqa: E402

logger = logging.getLogger("mcp_toggl")

TOOLS = [
    Tool(
        name="test_connection",
        description="Toggl Track Verbindung testen und aktiven Benutzer anzeigen.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_workspaces",
        description="Alle Toggl Track Workspaces des Benutzers auflisten.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_clients",
        description="Kunden (Clients) im Toggl Track Workspace auflisten.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "integer", "description": "Workspace-ID (optional, nutzt Standard-Workspace)"},
            },
        },
    ),
    Tool(
        name="get_client",
        description="Details eines Toggl Track Kunden laden.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "integer"},
                "client_id": {"type": "integer"},
            },
            "required": ["workspace_id", "client_id"],
        },
    ),
    Tool(
        name="search_client",
        description="Toggl Track Kunden nach Name suchen.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Suchbegriff für Kundenname"},
                "workspace_id": {"type": "integer"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="create_client",
        description="Neuen Kunden in Toggl Track anlegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Name des Kunden"},
                "workspace_id": {"type": "integer"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="list_projects",
        description="Projekte im Toggl Track Workspace auflisten. Optional nach Client filtern.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "integer"},
                "client_ids": {"type": "array", "items": {"type": "integer"}, "description": "Client-IDs zum Filtern"},
                "active": {"type": "boolean", "default": True},
            },
        },
    ),
    Tool(
        name="create_project",
        description="Neues Projekt in Toggl Track anlegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Projektname"},
                "workspace_id": {"type": "integer"},
                "client_id": {"type": "integer", "description": "Zugehöriger Client"},
                "billable": {"type": "boolean", "default": True},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="search_project",
        description="Toggl Track Projekte nach Name suchen.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Suchbegriff für Projektname"},
                "workspace_id": {"type": "integer"},
            },
            "required": ["name"],
        },
    ),
    Tool(
        name="search_time_entries",
        description="Zeiteinträge in Toggl Track suchen (Reports API). Liefert erfasste Stunden.",
        inputSchema={
            "type": "object",
            "properties": {
                "workspace_id": {"type": "integer"},
                "start_date": {"type": "string", "description": "Startdatum (YYYY-MM-DD)"},
                "end_date": {"type": "string", "description": "Enddatum (YYYY-MM-DD)"},
                "client_ids": {"type": "array", "items": {"type": "integer"}},
                "project_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["start_date", "end_date"],
        },
    ),
]

_client: TogglClient | None = None


def _get_client() -> TogglClient:
    global _client
    if _client is None:
        _client = TogglClient(TogglConfig.from_env())
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

    if name == "list_workspaces":
        result = await c.list_workspaces()
        compact = [{"id": w.get("id"), "name": w.get("name")} for w in result]
        return _json_response(compact)

    if name == "list_clients":
        result = await c.list_clients(arguments.get("workspace_id"))
        compact = [{"id": cl.get("id"), "name": cl.get("name"), "archived": cl.get("archived")} for cl in result]
        return _json_response(compact)

    if name == "get_client":
        result = await c.get_client(arguments["workspace_id"], arguments["client_id"])
        return _json_response(result)

    if name == "search_client":
        result = await c.search_clients(arguments["name"], arguments.get("workspace_id"))
        compact = [{"id": cl.get("id"), "name": cl.get("name")} for cl in result]
        return _json_response(compact)

    if name == "create_client":
        result = await c.create_client(arguments.get("workspace_id"), arguments["name"])
        return _json_response(result)

    if name == "list_projects":
        result = await c.list_projects(
            workspace_id=arguments.get("workspace_id"),
            client_ids=arguments.get("client_ids"),
            active=arguments.get("active", True),
        )
        compact = [
            {"id": p.get("id"), "name": p.get("name"), "client_id": p.get("client_id"),
             "active": p.get("active"), "billable": p.get("billable")}
            for p in result
        ]
        return _json_response(compact)

    if name == "create_project":
        result = await c.create_project(
            workspace_id=arguments.get("workspace_id"),
            name=arguments["name"],
            client_id=arguments.get("client_id"),
            billable=arguments.get("billable", True),
        )
        return _json_response(result)

    if name == "search_project":
        result = await c.search_projects(arguments["name"], arguments.get("workspace_id"))
        compact = [
            {"id": p.get("id"), "name": p.get("name"), "client_id": p.get("client_id"), "active": p.get("active")}
            for p in result
        ]
        return _json_response(compact)

    if name == "search_time_entries":
        result = await c.search_time_entries(
            workspace_id=arguments.get("workspace_id"),
            start_date=arguments["start_date"],
            end_date=arguments["end_date"],
            client_ids=arguments.get("client_ids"),
            project_ids=arguments.get("project_ids"),
        )
        return _json_response(result)

    return _json_response({"error": f"Unbekanntes Tool: {name}"})



async def _on_list_tools(_ctx, _params) -> ListToolsResult:
    return ListToolsResult(tools=await list_tools())


async def _on_call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    content = await call_tool(params.name, params.arguments or {})
    return CallToolResult(content=content)


server = Server("taskpilot-toggl", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

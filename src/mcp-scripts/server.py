"""MCP-Server für Script-Orchestrierung (Schicht 1).

Verwaltet registrierte Scripts und führt sie über den Sandbox-Executor aus.
Dieser Prozess läuft im Backend-Container OHNE docker.sock und delegiert die
eigentliche Ausführung an den token-geschützten Executor, der die Registry
besitzt und als einziger Dienst Docker starten darf.
Security-First: Nur deklarierte Scripts, Bild/Flags/Secrets kommen aus der Registry.
"""

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import httpx
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.types import (
    CallToolRequestParams,
    CallToolResult,
    ListToolsResult,
    TextContent,
    Tool,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_scripts")

REGISTRY_PATH = Path(__file__).parent / "scripts.json"
EXECUTOR_URL = os.environ.get("TP_SANDBOX_EXECUTOR_URL", "http://127.0.0.1:8090").rstrip("/")
EXECUTOR_TOKEN = os.environ.get("TP_SANDBOX_EXECUTOR_TOKEN", "")


def _load_registry() -> dict:
    """Script-Registry aus JSON laden (für list/info; Ausführung validiert der Executor erneut)."""
    with open(REGISTRY_PATH) as f:
        return json.load(f)


async def _run_script_via_executor(
    script_id: str,
    params: dict,
    input_files: dict[str, str] | None = None,
) -> dict:
    """Script-Ausführung an den Sandbox-Executor delegieren (HTTP, token-geschützt)."""
    if not EXECUTOR_TOKEN:
        return {
            "success": False,
            "error": "Sandbox-Executor nicht konfiguriert (TP_SANDBOX_EXECUTOR_TOKEN fehlt)",
            "stdout": "", "stderr": "", "generated_files": [], "run_id": None,
        }

    payload = {"script_id": script_id, "params": params, "input_files": input_files}
    # Grosszügiger HTTP-Timeout (Scripts dürfen bis 900s laufen).
    http_timeout = httpx.Timeout(960, connect=10.0)
    logger.info("Script '%s' an Executor delegiert: %s", script_id, EXECUTOR_URL)

    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(
                f"{EXECUTOR_URL}/run-script",
                json=payload,
                headers={"Authorization": f"Bearer {EXECUTOR_TOKEN}"},
            )
    except httpx.HTTPError as e:
        logger.exception("Sandbox-Executor nicht erreichbar (%s)", EXECUTOR_URL)
        return {
            "success": False,
            "error": f"Sandbox-Executor nicht erreichbar unter {EXECUTOR_URL}: {e}",
            "stdout": "", "stderr": "", "generated_files": [], "run_id": None,
        }

    if resp.status_code != 200:
        return {
            "success": False,
            "error": f"Sandbox-Executor Fehler (HTTP {resp.status_code}): {resp.text[:500]}",
            "stdout": "", "stderr": "", "generated_files": [], "run_id": None,
        }

    return resp.json()


TOOLS = [
    Tool(
        name="list_scripts",
        description="Alle registrierten Scripts mit ihren Parametern und Beschreibungen auflisten",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="run_script",
        description="Ein registriertes Script in einem isolierten Docker-Container ausführen. Nur registrierte Scripts sind erlaubt.",
        inputSchema={
            "type": "object",
            "properties": {
                "script_id": {
                    "type": "string",
                    "description": "ID des Scripts aus der Registry (z.B. 'toggl-rapport', 'md-to-word')",
                },
                "params": {
                    "type": "object",
                    "description": "Parameter als Key-Value-Paare gemäss Script-Definition",
                },
                "input_files": {
                    "type": "object",
                    "description": "Optional: Dict von {filename: content}, die dem Script als /input/filename (read-only) bereitgestellt werden",
                    "additionalProperties": {"type": "string"},
                },
            },
            "required": ["script_id"],
        },
    ),
    Tool(
        name="get_script_info",
        description="Detailinformationen zu einem bestimmten Script (Parameter, Security-Config, Timeouts)",
        inputSchema={
            "type": "object",
            "properties": {
                "script_id": {"type": "string", "description": "ID des Scripts"},
            },
            "required": ["script_id"],
        },
    ),
]



async def list_tools() -> list[Tool]:
    return TOOLS


async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    registry = _load_registry()

    if name == "list_scripts":
        scripts_summary = []
        for s in registry["scripts"]:
            scripts_summary.append({
                "id": s["id"],
                "name": s["name"],
                "description": s["description"],
                "params": s["params"],
                "autonomy": s["autonomy"],
                "timeout": s["security"]["max_timeout_seconds"],
                "data_class_max": s["security"]["data_class_max"],
            })
        return [TextContent(
            type="text",
            text=json.dumps(scripts_summary, indent=2, ensure_ascii=False),
        )]

    elif name == "get_script_info":
        script_id = arguments["script_id"]
        script = next((s for s in registry["scripts"] if s["id"] == script_id), None)
        if not script:
            return [TextContent(type="text", text=f"Script '{script_id}' nicht in Registry gefunden")]
        return [TextContent(
            type="text",
            text=json.dumps(script, indent=2, ensure_ascii=False),
        )]

    elif name == "run_script":
        script_id = arguments["script_id"]
        script = next((s for s in registry["scripts"] if s["id"] == script_id), None)
        if not script:
            available = [s["id"] for s in registry["scripts"]]
            return [TextContent(
                type="text",
                text=f"Script '{script_id}' nicht gefunden. Verfügbar: {available}",
            )]

        params = arguments.get("params", {})
        for p_def in script["params"]:
            if p_def["required"] and p_def["name"] not in params:
                return [TextContent(
                    type="text",
                    text=f"Pflicht-Parameter '{p_def['name']}' fehlt ({p_def['description']})",
                )]

        input_files = arguments.get("input_files")

        logger.info("Script-Ausführung gestartet: %s mit params=%s", script_id, params)
        result = await _run_script_via_executor(script_id, params, input_files)

        if result.get("success"):
            output_text = f"Script '{script['name']}' erfolgreich ausgeführt.\n\n"
            if result.get("stdout"):
                output_text += f"**Output:**\n```\n{result['stdout']}\n```\n\n"
            files = result.get("generated_files", [])
            if files:
                names = ", ".join(f["name"] for f in files)
                output_text += f"**Erzeugte Dateien (im Workspace):** {names}\n"
                output_text += f"**Scope:** `{result.get('scope')}` (Abruf via Executor `/artifacts/<scope>/<name>`)\n"
            if result.get("warning"):
                output_text += f"**Hinweis:** {result['warning']}\n"
        else:
            output_text = f"Script '{script['name']}' fehlgeschlagen.\n\n"
            output_text += f"**Fehler:** {result.get('error', 'Unbekannt')}\n"
            if result.get("stderr"):
                output_text += f"**Stderr:**\n```\n{result['stderr']}\n```\n"
            if result.get("stdout"):
                output_text += f"**Stdout:**\n```\n{result['stdout']}\n```\n"

        return [TextContent(type="text", text=output_text)]

    return [TextContent(type="text", text=f"Unbekanntes Tool: {name}")]



async def _on_list_tools(_ctx, _params) -> ListToolsResult:
    return ListToolsResult(tools=await list_tools())


async def _on_call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    content = await call_tool(params.name, params.arguments or {})
    return CallToolResult(content=content)


server = Server("scripts", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

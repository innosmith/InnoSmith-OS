"""MCP-Server für Code-Sandbox-Execution (Schicht 2).

Generierter Python-Code wird in einem ephemeren, netzwerkisolierten Docker-Container
ausgeführt. Security-First: Kein Netzwerk, keine Secrets, begrenzter Speicher/CPU.
"""

import asyncio
import logging
import os
import sys

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_sandbox")

# Der eigentliche `docker run` passiert im Sandbox-Executor-Sidecar. Dieser
# MCP-Server (läuft im Backend-Container OHNE docker.sock) ruft ihn nur per
# token-geschützter HTTP-API auf.
EXECUTOR_URL = os.environ.get("TP_SANDBOX_EXECUTOR_URL", "http://127.0.0.1:8090").rstrip("/")
EXECUTOR_TOKEN = os.environ.get("TP_SANDBOX_EXECUTOR_TOKEN", "")
MAX_CODE_LENGTH = 50_000

BLOCKED_IMPORTS = [
    "subprocess", "os.system", "shutil.rmtree",
    "ctypes", "importlib", "pickle",
]

BLOCKED_PATTERNS = [
    "exec(", "eval(", "compile(",
    "__import__", "os.system", "subprocess",
    "open('/etc", "open('/proc", "open('/sys",
    "os.environ", "os.getenv",
]


def _validate_code(code: str) -> list[str]:
    """Prüft Code auf offensichtlich gefährliche Patterns (Defence-in-Depth, nicht alleiniger Schutz)."""
    warnings = []
    for pattern in BLOCKED_PATTERNS:
        if pattern in code:
            warnings.append(f"Verdächtiges Pattern gefunden: '{pattern}'")
    return warnings


async def _execute_in_sandbox(
    code: str,
    input_files: dict[str, str] | None = None,
    timeout_seconds: int = 300,
    stdin_data: str | None = None,
    workspace_key: str | None = None,
) -> dict:
    """Code in isolierter Docker-Sandbox ausführen — via Sandbox-Executor-Sidecar.

    Dieser Prozess hat KEINEN Docker-Zugriff. Die Ausführung delegiert an den
    token-geschützten Executor (``TP_SANDBOX_EXECUTOR_URL``).
    """
    if not EXECUTOR_TOKEN:
        return {
            "success": False,
            "error": "Sandbox-Executor nicht konfiguriert (TP_SANDBOX_EXECUTOR_TOKEN fehlt)",
            "stdout": "", "stderr": "", "generated_files": [],
            "run_id": None, "duration_seconds": 0,
        }

    payload = {
        "code": code,
        "input_files": input_files,
        "timeout_seconds": timeout_seconds,
        "stdin_data": stdin_data,
        "workspace_key": workspace_key,
    }
    http_timeout = httpx.Timeout(timeout_seconds + 30, connect=10.0)
    logger.info("Sandbox-Ausführung delegiert an Executor: %s (timeout=%ds)", EXECUTOR_URL, timeout_seconds)

    try:
        async with httpx.AsyncClient(timeout=http_timeout) as client:
            resp = await client.post(
                f"{EXECUTOR_URL}/execute",
                json=payload,
                headers={"Authorization": f"Bearer {EXECUTOR_TOKEN}"},
            )
    except httpx.HTTPError as e:
        logger.exception("Sandbox-Executor nicht erreichbar (%s)", EXECUTOR_URL)
        return {
            "success": False,
            "error": f"Sandbox-Executor nicht erreichbar unter {EXECUTOR_URL}: {e}",
            "stdout": "", "stderr": "", "generated_files": [],
            "run_id": None, "duration_seconds": 0,
        }

    if resp.status_code != 200:
        return {
            "success": False,
            "error": f"Sandbox-Executor Fehler (HTTP {resp.status_code}): {resp.text[:500]}",
            "stdout": "", "stderr": "", "generated_files": [],
            "run_id": None, "duration_seconds": 0,
        }

    return resp.json()


TOOLS = [
    Tool(
        name="execute_code",
        description=(
            "Python-Code in einer isolierten Sandbox ausführen. "
            "Die Sandbox ist HEADLESS (kein Terminal, kein Fenster, keine Echtzeit-Tastatur), "
            "hat KEIN Netzwerk, KEINE Secrets und begrenzte Ressourcen. "
            "Kein input() ohne bereitgestellte stdin_data. "
            "WICHTIG: Dateien, die nach /workspace/ geschrieben werden, werden dem Nutzer "
            "AUTOMATISCH INLINE im Chat angezeigt — Bilder (.png/.svg/...) als Vorschau, "
            "HTML (.html) als voll interaktive, SPIELBARE Vorschau in einem Sandbox-iframe "
            "(Tastatur/Maus/Animation funktionieren, mit Vergrössern und Vollbild). "
            "Für interaktive/grafische Aufgaben (Spiele, UIs, Visualisierungen, Dashboards) "
            "daher ein eigenständiges HTML-Artefakt (eingebettetes CSS+JS) nach /workspace/ "
            "schreiben — NICHT behaupten, das sei nicht darstellbar, und KEIN externes Hosting "
            "(z. B. GitHub Pages) anbieten. "
            "Verfügbare Packages: pandas, numpy, matplotlib, seaborn, openpyxl, scipy, pyyaml, jinja2, tabulate. "
            "Input-Dateien werden in /input/ bereitgestellt (read-only). "
            "Output-Dateien in /workspace/ schreiben."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python-Code der ausgeführt werden soll",
                },
                "input_files": {
                    "type": "object",
                    "description": "Optional: Dict von {filename: content} die als /input/filename verfügbar gemacht werden",
                    "additionalProperties": {"type": "string"},
                },
                "stdin_data": {
                    "type": "string",
                    "description": "Optional: Standard-Eingabe (stdin), die dem Programm zugeführt wird (für input()/sys.stdin)",
                },
                "workspace_key": {
                    "type": "string",
                    "description": "Optional: stabiler Schlüssel für einen persistenten Workspace über mehrere Aufrufe (z. B. Task-ID)",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Timeout in Sekunden (default: 300, max: 900)",
                    "default": 300,
                },
                "description": {
                    "type": "string",
                    "description": "Kurzbeschreibung was der Code tut (für Audit-Log)",
                },
            },
            "required": ["code"],
        },
    ),
    Tool(
        name="list_packages",
        description="Zeigt alle vorinstallierten Python-Packages im Sandbox-Container",
        inputSchema={"type": "object", "properties": {}},
    ),
]

AVAILABLE_PACKAGES = [
    "pandas 2.2.3", "numpy 2.1.3", "openpyxl 3.1.5", "xlsxwriter 3.2.0",
    "matplotlib 3.9.3", "seaborn 0.13.2", "pyyaml 6.0.2", "toml 0.10.2",
    "python-dateutil 2.9.0", "chardet 5.2.0", "regex 2024.11.6",
    "tabulate 0.9.0", "jinja2 3.1.4", "scipy 1.14.1",
]

server = Server("sandbox")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "list_packages":
        return [TextContent(
            type="text",
            text="Verfügbare Packages in der Sandbox:\n" + "\n".join(f"- {p}" for p in AVAILABLE_PACKAGES),
        )]

    elif name == "execute_code":
        code = arguments.get("code", "").strip()
        if not code:
            return [TextContent(type="text", text="Fehler: Kein Code übergeben")]

        if len(code) > MAX_CODE_LENGTH:
            return [TextContent(type="text", text=f"Fehler: Code zu lang ({len(code)} Zeichen, max {MAX_CODE_LENGTH})")]

        warnings = _validate_code(code)
        if warnings:
            warning_text = "**Sicherheits-Warnungen (Code wird trotzdem ausgeführt, Sandbox schützt):**\n"
            warning_text += "\n".join(f"- {w}" for w in warnings)
            logger.warning("Code-Warnungen für Sandbox-Ausführung: %s", warnings)

        timeout = min(arguments.get("timeout_seconds", 300), 900)
        input_files = arguments.get("input_files")
        stdin_data = arguments.get("stdin_data")
        workspace_key = arguments.get("workspace_key")
        description = arguments.get("description", "Keine Beschreibung")

        logger.info("Sandbox execute_code: description='%s', code_length=%d", description, len(code))

        result = await _execute_in_sandbox(
            code, input_files, timeout, stdin_data=stdin_data, workspace_key=workspace_key
        )

        output_text = ""
        if warnings:
            output_text += "**Sicherheits-Hinweise:**\n" + "\n".join(f"- {w}" for w in warnings) + "\n\n"

        if result["success"]:
            output_text += f"**Ausführung erfolgreich** (Dauer: {result['duration_seconds']}s)\n\n"
            if result["stdout"]:
                output_text += f"**Output:**\n```\n{result['stdout']}\n```\n\n"
            if result.get("warning"):
                output_text += f"**Hinweis:** {result['warning']}\n\n"
            if result["generated_files"]:
                output_text += "**Erzeugte Dateien (im Workspace):**\n"
                for f in result["generated_files"]:
                    size_kb = f.get("size_bytes", 0) / 1024
                    output_text += f"- {f['name']} ({size_kb:.1f} KB)\n"
                # Maschinenlesbarer Marker fuer den Backend-Callback: teilt Scope +
                # Dateinamen mit, damit die erzeugten Artefakte inline im Chat
                # gerendert werden koennen. Rein additiv (menschlicher Text bleibt).
                scope = result.get("scope")
                names = "|".join(
                    f["name"] for f in result["generated_files"] if f.get("name")
                )
                if scope and names:
                    output_text += f"\n\n<!--tp-exec:{scope}:{names}-->"
        else:
            output_text += f"**Ausführung fehlgeschlagen** (Exit-Code: {result.get('exit_code', '?')})\n\n"
            if result.get("error"):
                output_text += f"**Fehler:** {result['error']}\n\n"
            if result.get("stderr"):
                output_text += f"**Stderr:**\n```\n{result['stderr']}\n```\n\n"
            if result.get("stdout"):
                output_text += f"**Stdout:**\n```\n{result['stdout']}\n```\n"

        return [TextContent(type="text", text=output_text)]

    return [TextContent(type="text", text=f"Unbekanntes Tool: {name}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

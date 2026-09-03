"""TaskPilot MCP-Server — der Hermes-Agent kann Tasks und Agent-Jobs lesen/schreiben."""

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timezone

import asyncpg
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
logger = logging.getLogger("mcp_taskpilot")


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


async def _get_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        host=_env("TP_DB_HOST", "localhost"),
        port=int(_env("TP_DB_PORT", "5435")),
        user=_env("TP_DB_USER", "taskpilot"),
        password=_env("TP_DB_PASSWORD", "taskpilot_dev_2026"),
        database=_env("TP_DB_NAME", "taskpilot_dev"),
        min_size=1,
        max_size=3,
    )


TOOLS = [
    Tool(
        name="list_projects",
        description="Alle aktiven Projekte mit Board-Spalten auflisten",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_tasks",
        description="Tasks filtern nach Projekt, Assignee oder Status",
        inputSchema={
            "type": "object",
            "properties": {
                "project_id": {"type": "string", "description": "UUID des Projekts"},
                "assignee": {"type": "string", "description": "'me' oder 'agent'"},
                "is_completed": {"type": "boolean"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    ),
    Tool(
        name="get_task",
        description="Ein Task mit Checkliste und Tags laden",
        inputSchema={
            "type": "object",
            "properties": {"task_id": {"type": "string", "description": "UUID des Tasks"}},
            "required": ["task_id"],
        },
    ),
    Tool(
        name="create_task",
        description="Neuen Task erstellen",
        inputSchema={
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "project_id": {"type": "string"},
                "board_column_id": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string", "default": "me"},
                "pipeline_column_id": {"type": "string"},
                "due_date": {"type": "string", "description": "Faelligkeitsdatum im Format YYYY-MM-DD"},
                "recurrence_rule": {"type": "string", "description": "Cron-Ausdruck für Wiederholungen, z.B. '0 7 * * MON'"},
            },
            "required": ["title", "project_id", "board_column_id"],
        },
    ),
    Tool(
        name="update_task",
        description="Task aktualisieren (Titel, Beschreibung, Status etc.)",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "title": {"type": "string"},
                "description": {"type": "string"},
                "assignee": {"type": "string"},
                "is_completed": {"type": "boolean"},
                "due_date": {"type": "string", "description": "Faelligkeitsdatum im Format YYYY-MM-DD"},
                "recurrence_rule": {"type": "string", "description": "Cron-Ausdruck für Wiederholungen, z.B. '0 7 * * MON'"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="get_agent_job",
        description="Details eines Agent-Jobs lesen",
        inputSchema={
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    ),
    Tool(
        name="list_agent_jobs",
        description="Agent-Jobs auflisten, optional nach Status filtern",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "queued, running, completed, failed, awaiting_approval"},
            },
        },
    ),
    Tool(
        name="update_agent_job",
        description="Agent-Job-Ergebnis schreiben (Status, Output, Tokens, Kosten)",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "status": {"type": "string"},
                "output": {"type": "string"},
                "error_message": {"type": "string"},
                "llm_model": {"type": "string"},
                "tokens_used": {"type": "integer"},
                "cost_usd": {"type": "number"},
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="get_sender_profile",
        description="Absender-Profil laden. Gibt gespeicherte Beziehungsinformationen zurück (Ton, Sprache, Beziehungstyp, Organisation). Falls kein Profil existiert, wird ein leeres Profil mit Defaults zurückgegeben.",
        inputSchema={
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "E-Mail-Adresse des Absenders"},
            },
            "required": ["email"],
        },
    ),
    Tool(
        name="update_sender_profile",
        description="Absender-Profil aktualisieren oder neu anlegen. Wird nach jeder Triage aufgerufen, um das Beziehungsgedaechtnis zu pflegen.",
        inputSchema={
            "type": "object",
            "properties": {
                "email": {"type": "string", "description": "E-Mail-Adresse des Absenders"},
                "display_name": {"type": "string"},
                "organization": {"type": "string", "description": "Firma/Organisation des Absenders"},
                "relationship": {"type": "string", "enum": ["kunde", "partner", "lieferant", "intern", "hochschule", "behoerde", "unbekannt"]},
                "tone": {"type": "string", "enum": ["formell", "informell", "neutral"]},
                "language": {"type": "string", "enum": ["de", "en", "fr", "it"]},
                "notes": {"type": "string", "description": "Freitext-Notizen zum Absender"},
            },
            "required": ["email"],
        },
    ),
    Tool(
        name="semantic_search_documents",
        description=(
            "Durchsucht den lokalen semantischen Index über Anthonys E-Mails und "
            "OneDrive-Dokumente (Bedeutung + Stichwort, hybrid). Gibt pro Treffer "
            "eine Snippet-Passage samt Quelle (Titel, Typ, URL) zurück -- ideal als "
            "RAG-Grounding, z. B. 'alle Dokumente zum Thema X'. mode=hybrid (Default) "
            "kombiniert Semantik und Keyword; mode=semantic nur Bedeutung; mode=exact "
            "nur Stichwort. sources filtert auf 'email' und/oder 'onedrive'."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchanfrage in natürlicher Sprache oder Stichworte"},
                "mode": {"type": "string", "enum": ["hybrid", "semantic", "exact"], "default": "hybrid"},
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["email", "onedrive", "upload", "transcript"]},
                    "description": "Optionaler Filter auf Quelltypen",
                },
                "limit": {"type": "integer", "default": 25, "description": "Max. Treffer (bis 500)"},
            },
            "required": ["query"],
        },
    ),
]

pool: asyncpg.Pool | None = None


async def get_pool() -> asyncpg.Pool:
    global pool
    if pool is None:
        pool = await _get_pool()
    return pool


def _row_to_dict(row: asyncpg.Record) -> dict:
    d = dict(row)
    for k, v in d.items():
        if isinstance(v, datetime):
            d[k] = v.isoformat()
        elif hasattr(v, "hex"):
            d[k] = str(v)
    return d


async def list_tools() -> list[Tool]:
    return TOOLS


async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    p = await get_pool()

    if name == "list_projects":
        rows = await p.fetch(
            "SELECT id, name, color, status FROM projects WHERE status != 'archived' ORDER BY name"
        )
        result = [_row_to_dict(r) for r in rows]
        for proj in result:
            cols = await p.fetch(
                "SELECT id, name, position FROM board_columns WHERE project_id = $1 ORDER BY position",
                proj["id"] if not isinstance(proj["id"], str) else rows[result.index(proj)]["id"],
            )
            proj["board_columns"] = [_row_to_dict(c) for c in cols]
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "list_tasks":
        conditions = []
        params = []
        idx = 1
        if arguments.get("project_id"):
            conditions.append(f"t.project_id = ${idx}::uuid")
            params.append(arguments["project_id"])
            idx += 1
        if arguments.get("assignee"):
            conditions.append(f"t.assignee = ${idx}")
            params.append(arguments["assignee"])
            idx += 1
        if "is_completed" in arguments:
            conditions.append(f"t.is_completed = ${idx}")
            params.append(arguments["is_completed"])
            idx += 1

        where = "WHERE " + " AND ".join(conditions) if conditions else ""
        limit = min(int(arguments.get("limit", 50)), 500)
        idx_limit = idx
        params.append(limit)
        rows = await p.fetch(
            f"SELECT t.id, t.title, t.assignee, t.is_completed, t.due_date, p.name as project_name "
            f"FROM tasks t JOIN projects p ON p.id = t.project_id {where} "
            f"ORDER BY t.created_at DESC LIMIT ${idx_limit}",
            *params,
        )
        return [TextContent(type="text", text=json.dumps([_row_to_dict(r) for r in rows], indent=2))]

    elif name == "get_task":
        row = await p.fetchrow(
            "SELECT t.*, p.name as project_name FROM tasks t "
            "JOIN projects p ON p.id = t.project_id WHERE t.id = $1::uuid",
            arguments["task_id"],
        )
        if row is None:
            return [TextContent(type="text", text="Task nicht gefunden")]
        result = _row_to_dict(row)
        checklist = await p.fetch(
            "SELECT id, text, is_checked, position FROM checklist_items "
            "WHERE task_id = $1::uuid ORDER BY position",
            arguments["task_id"],
        )
        result["checklist_items"] = [_row_to_dict(c) for c in checklist]
        tags = await p.fetch(
            "SELECT tg.id, tg.name, tg.color FROM tags tg "
            "JOIN task_tags tt ON tt.tag_id = tg.id WHERE tt.task_id = $1::uuid",
            arguments["task_id"],
        )
        result["tags"] = [_row_to_dict(t) for t in tags]
        return [TextContent(type="text", text=json.dumps(result, indent=2))]

    elif name == "create_task":
        row = await p.fetchrow(
            "INSERT INTO tasks (title, project_id, board_column_id, description, assignee, "
            "pipeline_column_id, due_date, recurrence_rule, board_position) "
            "VALUES ($1, $2::uuid, $3::uuid, $4, $5, $6::uuid, $7::date, $8, "
            "(SELECT COALESCE(MAX(board_position), 0) + 1 FROM tasks WHERE board_column_id = $3::uuid)) "
            "RETURNING id, title",
            arguments["title"],
            arguments["project_id"],
            arguments["board_column_id"],
            arguments.get("description"),
            arguments.get("assignee", "me"),
            arguments.get("pipeline_column_id"),
            arguments.get("due_date"),
            arguments.get("recurrence_rule"),
        )
        return [TextContent(type="text", text=json.dumps(_row_to_dict(row), indent=2))]

    elif name == "update_task":
        task_id = arguments.pop("task_id")

        # Wiederkehrende Vorlagen: is_completed und due_date sind hier
        # bedeutungslos (der Cron-Ausdruck steuert die Termine) und machen die
        # Vorlage im Board unsichtbar, während der Scheduler weiterläuft.
        is_template = await p.fetchval(
            "SELECT recurrence_rule IS NOT NULL AND template_id IS NULL "
            "FROM tasks WHERE id = $1::uuid",
            task_id,
        )
        if is_template is None:
            return [TextContent(type="text", text="Task nicht gefunden")]
        if is_template:
            blocked = [k for k in ("is_completed", "due_date") if k in arguments]
            for key in blocked:
                arguments.pop(key)
            if blocked and not arguments:
                return [TextContent(
                    type="text",
                    text=(
                        "Wiederkehrende Vorlagen können nicht abgehakt oder "
                        "terminiert werden — erledige stattdessen die aktuelle "
                        "Instanz (Task mit gesetztem template_id)."
                    ),
                )]

        sets = []
        params = []
        idx = 1
        for key in ("title", "description", "assignee", "is_completed", "recurrence_rule"):
            if key in arguments:
                sets.append(f"{key} = ${idx}")
                params.append(arguments[key])
                idx += 1
        if "due_date" in arguments:
            sets.append(f"due_date = ${idx}::date")
            params.append(arguments["due_date"])
            idx += 1
        if not sets:
            return [TextContent(type="text", text="Keine Felder zum Aktualisieren")]
        params.append(task_id)
        row = await p.fetchrow(
            f"UPDATE tasks SET {', '.join(sets)}, updated_at = now() WHERE id = ${idx}::uuid RETURNING id, title, assignee, is_completed",
            *params,
        )
        return [TextContent(type="text", text=json.dumps(_row_to_dict(row), indent=2))]

    elif name == "get_agent_job":
        row = await p.fetchrow(
            "SELECT aj.*, t.title as task_title FROM agent_jobs aj "
            "JOIN tasks t ON t.id = aj.task_id WHERE aj.id = $1::uuid",
            arguments["job_id"],
        )
        if row is None:
            return [TextContent(type="text", text="Job nicht gefunden")]
        return [TextContent(type="text", text=json.dumps(_row_to_dict(row), indent=2))]

    elif name == "list_agent_jobs":
        status_filter = arguments.get("status")
        if status_filter:
            rows = await p.fetch(
                "SELECT aj.id, aj.task_id, aj.status, aj.created_at, t.title as task_title "
                "FROM agent_jobs aj JOIN tasks t ON t.id = aj.task_id "
                "WHERE aj.status = $1 ORDER BY aj.created_at DESC",
                status_filter,
            )
        else:
            rows = await p.fetch(
                "SELECT aj.id, aj.task_id, aj.status, aj.created_at, t.title as task_title "
                "FROM agent_jobs aj JOIN tasks t ON t.id = aj.task_id "
                "ORDER BY aj.created_at DESC LIMIT 20"
            )
        return [TextContent(type="text", text=json.dumps([_row_to_dict(r) for r in rows], indent=2))]

    elif name == "update_agent_job":
        job_id = arguments.pop("job_id")
        sets = []
        params = []
        idx = 1
        for key in ("status", "output", "error_message", "llm_model", "tokens_used", "cost_usd"):
            if key in arguments:
                sets.append(f"{key} = ${idx}")
                params.append(arguments[key])
                idx += 1

        if not sets:
            return [TextContent(type="text", text="Keine Felder zum Aktualisieren")]

        status_val = arguments.get("status")
        if status_val == "running":
            sets.append(f"started_at = ${idx}")
            params.append(datetime.now(timezone.utc))
            idx += 1
        if status_val in ("completed", "failed"):
            sets.append(f"completed_at = ${idx}")
            params.append(datetime.now(timezone.utc))
            idx += 1

        params.append(job_id)
        row = await p.fetchrow(
            f"UPDATE agent_jobs SET {', '.join(sets)} WHERE id = ${idx}::uuid RETURNING id, status, output",
            *params,
        )
        return [TextContent(type="text", text=json.dumps(_row_to_dict(row), indent=2))]

    elif name == "get_sender_profile":
        email = arguments["email"].lower().strip()
        row = await p.fetchrow(
            "SELECT * FROM sender_profiles WHERE email = $1", email
        )
        if row is None:
            return [TextContent(type="text", text=json.dumps({
                "email": email,
                "exists": False,
                "display_name": None,
                "organization": None,
                "relationship": "unbekannt",
                "tone": "neutral",
                "language": "de",
                "notes": None,
                "email_count": 0,
                "last_contact_at": None,
            }, indent=2, ensure_ascii=False))]
        result = _row_to_dict(row)
        result["exists"] = True
        return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "update_sender_profile":
        email = arguments["email"].lower().strip()
        row = await p.fetchrow(
            "SELECT id FROM sender_profiles WHERE email = $1", email
        )
        if row is None:
            new_row = await p.fetchrow(
                "INSERT INTO sender_profiles (email, display_name, organization, relationship, tone, language, notes, email_count, last_contact_at) "
                "VALUES ($1, $2, $3, $4, $5, $6, $7, 1, now()) RETURNING *",
                email,
                arguments.get("display_name"),
                arguments.get("organization"),
                arguments.get("relationship", "unbekannt"),
                arguments.get("tone", "neutral"),
                arguments.get("language", "de"),
                arguments.get("notes"),
            )
            result = _row_to_dict(new_row)
            result["action"] = "created"
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]
        else:
            sets = ["email_count = email_count + 1", "last_contact_at = now()"]
            params = []
            idx = 1
            for key in ("display_name", "organization", "relationship", "tone", "language", "notes"):
                if key in arguments:
                    sets.append(f"{key} = ${idx}")
                    params.append(arguments[key])
                    idx += 1
            params.append(email)
            updated = await p.fetchrow(
                f"UPDATE sender_profiles SET {', '.join(sets)} WHERE email = ${idx} RETURNING *",
                *params,
            )
            result = _row_to_dict(updated)
            result["action"] = "updated"
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

    elif name == "semantic_search_documents":
        return await _semantic_search_documents(p, arguments)

    return [TextContent(type="text", text=f"Unbekanntes Tool: {name}")]


_SEARCH_QUERY_INSTRUCT = (
    "Instruct: Given a search query, retrieve relevant documents and emails "
    "that answer or relate to it.\nQuery: "
)

# Herkunftsangaben, die JEDER Treffer tragen muss -- in beiden Zweigen der
# Hybrid-Suche identisch, damit die RRF-Fusion sie unabhaengig vom Trefferpfad
# durchreicht.
#
# Das Datum fehlte hier lange, und das war teuer: am 03.08.2026 uebernahm der
# Schreib-Pass den Satz «fuer Juli haben wir noch 14h Budget» aus einer Mail vom
# 02.07.2026 als heutigen Stand. Das Modell konnte die Aktualitaet nicht pruefen,
# weil sie nie im Trefferobjekt stand. Datum als YYYY-MM-DD (nicht als voller
# ISO-Zeitstempel) haelt den Kontext knapp und bleibt mit «heute» vergleichbar.
_PROVENANCE_COLUMNS = (
    "to_char(source_modified_at, 'YYYY-MM-DD') AS date, "
    "metadata->>'from' AS \"from\", "
)

# Ein E-Mail-Treffer ohne Absender ist ein Entwurf: nie gesendet, oft vom Agenten
# selbst geschrieben. Als Suchtreffer wirkt er wie belegte Korrespondenz. Am
# 04.08.2026 uebernahm ein Antwort-Entwurf so die Angaben eines geloeschten
# Entwurfs und erfand eine IP-Adresse fuer dessen Platzhalter. Der Indexer
# erfasst Entwuerfe inzwischen nicht mehr; dieser Filter wirkt zusaetzlich sofort
# und unabhaengig davon, was historisch im Index liegt.
_EXCLUDE_DRAFTS = " AND NOT (source_type = 'email' AND metadata->>'from' IS NULL)"


async def _embed_query(query: str) -> list[float] | None:
    """Erzeugt ein Query-Embedding via lokalem Ollama (Such-Modell). Best-effort."""
    import httpx

    base = _env("TP_OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = _env("TP_SEARCH_EMBED_MODEL", "qwen3-embedding:4b-fp16")
    dim = int(_env("TP_SEARCH_EMBED_DIM", "2560"))
    prompt = _SEARCH_QUERY_INSTRUCT + query
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base}/api/embeddings", json={"model": model, "prompt": prompt[:8000]})
            resp.raise_for_status()
            vec = resp.json().get("embedding")
    except Exception as exc:  # noqa: BLE001
        logger.warning("Query-Embedding fehlgeschlagen: %s", exc)
        return None
    if not isinstance(vec, list) or len(vec) != dim:
        return None
    return [float(x) for x in vec]


def _vec_literal(vec: list[float]) -> str:
    return "[" + ",".join(f"{x:.7f}" for x in vec) + "]"


async def _semantic_search_documents(p: asyncpg.Pool, arguments: dict) -> list[TextContent]:
    """Hybrid-Suche (pgvector + tsvector, RRF) über semantic_documents für Hermes."""
    query = (arguments.get("query") or "").strip()
    if not query:
        return [TextContent(type="text", text=json.dumps({"results": [], "note": "leere Anfrage"}))]
    mode = arguments.get("mode", "hybrid")
    limit = min(int(arguments.get("limit", 25)), 500)
    sources = arguments.get("sources") or None
    cand = max(limit, 300)

    # Scope pro Principal: entweder explizit uebergeben oder (Ein-Personen-System)
    # der Owner. So sieht der Agent nur die Dokumente seines Principals.
    principal = arguments.get("user_id")
    if not principal:
        owner_row = await p.fetchrow(
            "SELECT id FROM users WHERE role = 'owner' ORDER BY created_at LIMIT 1"
        )
        principal = str(owner_row["id"]) if owner_row else None

    semantic: list[dict] = []
    keyword: list[dict] = []

    if mode in ("hybrid", "semantic"):
        vec = await _embed_query(query)
        if vec is not None:
            params = [_vec_literal(vec)]
            filt = " WHERE embedding IS NOT NULL"
            if sources:
                params.append(sources)
                filt += f" AND source_type = ANY(${len(params)})"
            if principal:
                params.append(principal)
                filt += f" AND user_id = ${len(params)}::uuid"
            filt += _EXCLUDE_DRAFTS
            rows = await p.fetch(
                "SELECT source_type, source_id, title, url, mime, "
                + _PROVENANCE_COLUMNS +
                "left(content_text, 260) AS snippet, "
                "1 - (embedding <=> $1::halfvec) AS similarity "
                "FROM semantic_documents" + filt +
                " ORDER BY embedding <=> $1::halfvec LIMIT " + str(cand),
                *params,
            )
            seen = set()
            for r in rows:
                key = (r["source_type"], r["source_id"])
                if key in seen:
                    continue
                seen.add(key)
                semantic.append(_row_to_dict(r))

    if mode in ("hybrid", "exact"):
        params = [query]
        filt = ""
        if sources:
            params.append(sources)
            filt += f" AND source_type = ANY(${len(params)})"
        if principal:
            params.append(principal)
            filt += f" AND user_id = ${len(params)}::uuid"
        filt += _EXCLUDE_DRAFTS
        rows = await p.fetch(
            "SELECT source_type, source_id, title, url, mime, "
            + _PROVENANCE_COLUMNS +
            "ts_headline('german', content_text, q, "
            "'MaxFragments=2,MinWords=5,MaxWords=22,StartSel=<b>,StopSel=</b>') AS snippet "
            "FROM semantic_documents, websearch_to_tsquery('german', $1) q "
            "WHERE content_tsv @@ q" + filt +
            " ORDER BY ts_rank_cd(content_tsv, q) DESC LIMIT " + str(cand),
            *params,
        )
        seen = set()
        for r in rows:
            key = (r["source_type"], r["source_id"])
            if key in seen:
                continue
            seen.add(key)
            d = _row_to_dict(r)
            if d.get("snippet"):
                d["snippet"] = d["snippet"].replace("<b>", "").replace("</b>", "")
            keyword.append(d)

    # RRF-Fusion
    k = 60
    scores: dict = {}
    merged: dict = {}
    for rank, it in enumerate(semantic):
        key = (it["source_type"], it["source_id"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        merged.setdefault(key, it)
    for rank, it in enumerate(keyword):
        key = (it["source_type"], it["source_id"])
        scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
        if key not in merged:
            merged[key] = it
        elif not merged[key].get("snippet"):
            merged[key]["snippet"] = it.get("snippet")
    ranked = sorted(merged.values(), key=lambda it: scores[(it["source_type"], it["source_id"])], reverse=True)
    results = ranked[:limit]
    return [TextContent(type="text", text=json.dumps({"mode": mode, "count": len(results), "results": results}, indent=2, ensure_ascii=False))]



async def _on_list_tools(_ctx, _params) -> ListToolsResult:
    return ListToolsResult(tools=await list_tools())


async def _on_call_tool(_ctx, params: CallToolRequestParams) -> CallToolResult:
    content = await call_tool(params.name, params.arguments or {})
    return CallToolResult(content=content)


server = Server("taskpilot", on_list_tools=_on_list_tools, on_call_tool=_on_call_tool)


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

"""MCP-Server für Microsoft 365-Zugriff via Graph API.

Der Hermes-Agent kann diesen Server als Tool nutzen für:
  - E-Mails lesen, Entwürfe erstellen, versenden (nach Approval)
  - Kalender: Termine lesen/erstellen, freie Slots finden
  - Teams-Chat: Nachrichten lesen, Meeting-Transkripte abrufen
  - OneDrive/SharePoint: Dateien suchen, auflisten, lesen
  - Planner: Aufgaben lesen/erstellen/aktualisieren
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("mcp_graph")


def _html_to_text(html: str) -> str:
    """HTML in lesbaren Plain-Text konvertieren."""
    if not html:
        return ""
    text = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</(p|div|tr|li|h[1-6])>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

TOOLS = [
    Tool(
        name="list_email_folders",
        description="Alle verfügbaren E-Mail-Ordner auflisten",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="list_emails",
        description="E-Mails aus einem Ordner lesen (Standard: Posteingang)",
        inputSchema={
            "type": "object",
            "properties": {
                "folder": {
                    "type": "string",
                    "default": "inbox",
                    "description": "Ordner-Name oder ID (inbox, drafts, sentitems, etc.)",
                },
                "top": {"type": "integer", "default": 10},
                "skip": {"type": "integer", "default": 0},
                "filter": {
                    "type": "string",
                    "description": "OData-Filter, z.B. \"isRead eq false\"",
                },
            },
        },
    ),
    Tool(
        name="get_email",
        description="Eine einzelne E-Mail mit vollem Body laden",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Graph-API Message-ID"},
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="get_email_categories",
        description="CoPilot-Kategorien und Klassifizierung einer E-Mail lesen",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="get_email_attachments",
        description=(
            "Anhänge einer E-Mail abrufen. Bild-Anhänge (PNG/JPG/GIF/WebP) werden lokal "
            "gespeichert und ihr Dateipfad zurückgegeben, damit du sie mit vision_analyze "
            "(image_url=<pfad>) inhaltlich auswerten kannst. Nutze dies bei E-Mails mit "
            "hasAttachments=true, wenn der Bildinhalt für die Triage relevant ist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Graph-API Message-ID"},
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="create_draft",
        description="E-Mail-Entwurf im Drafts-Ordner erstellen. HITL: Entwurf muss vor Versand genehmigt werden.",
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "body_html": {"type": "string", "description": "HTML-Body der E-Mail"},
                "to_recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Liste von E-Mail-Adressen",
                },
                "cc_recipients": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "reply_to_id": {
                    "type": "string",
                    "description": "Message-ID der Original-Mail (für Antworten)",
                },
            },
            "required": ["subject", "body_html", "to_recipients"],
        },
    ),
    Tool(
        name="send_email",
        description="Einen bestehenden Entwurf versenden. ACHTUNG: Erfordert vorherige Genehmigung (HITL L1)!",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "ID des zu sendenden Entwurfs"},
            },
            "required": ["message_id"],
        },
    ),
    Tool(
        name="mark_as_read",
        description="E-Mail als gelesen markieren",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
            },
            "required": ["message_id"],
        },
    ),
    # ── Kalender-Tools ──────────────────────────────────────────
    Tool(
        name="list_calendar_events",
        description="Kalendertermine in einem Zeitraum abrufen",
        inputSchema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Start-Zeitpunkt ISO 8601 (z.B. 2026-04-27T00:00:00)"},
                "end": {"type": "string", "description": "End-Zeitpunkt ISO 8601 (z.B. 2026-04-27T23:59:59)"},
                "top": {"type": "integer", "default": 20},
            },
            "required": ["start", "end"],
        },
    ),
    Tool(
        name="get_calendar_event",
        description="Einzelnen Kalendertermin laden",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
            },
            "required": ["event_id"],
        },
    ),
    Tool(
        name="create_calendar_event",
        description="Termin oder Zeitblocker im Kalender erstellen. Eigene Zeitblocker: L2 (Agent erstellt). Termine mit anderen: L1 (Approval nötig).",
        inputSchema={
            "type": "object",
            "properties": {
                "subject": {"type": "string", "description": "Titel des Termins"},
                "start": {"type": "string", "description": "Start ISO 8601 (z.B. 2026-04-28T14:00:00)"},
                "end": {"type": "string", "description": "Ende ISO 8601 (z.B. 2026-04-28T16:00:00)"},
                "body": {"type": "string", "description": "Optionale Beschreibung (HTML)"},
                "location": {"type": "string", "description": "Ort (optional)"},
                "show_as": {"type": "string", "enum": ["free", "tentative", "busy", "oof"], "default": "busy"},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Outlook-Kategorien (z.B. ['Wichtig', 'Schulung']). Müssen in Outlook bereits definiert sein.",
                },
            },
            "required": ["subject", "start", "end"],
        },
    ),
    Tool(
        name="find_free_slots",
        description="Freie Zeitfenster im Kalender finden für einen bestimmten Zeitraum",
        inputSchema={
            "type": "object",
            "properties": {
                "start": {"type": "string", "description": "Start ISO 8601"},
                "end": {"type": "string", "description": "Ende ISO 8601"},
                "duration_minutes": {"type": "integer", "default": 60, "description": "Gewünschte Dauer in Minuten"},
            },
            "required": ["start", "end"],
        },
    ),
    # ── Outlook-Kategorie & Ordner-Tools ─────────────────────
    Tool(
        name="get_thread",
        description="Alle Nachrichten eines E-Mail-Threads (Konversation) chronologisch laden. Gibt den gesamten Verlauf zurück.",
        inputSchema={
            "type": "object",
            "properties": {
                "conversation_id": {"type": "string", "description": "conversationId der E-Mail (aus get_email)"},
                "top": {"type": "integer", "default": 10, "description": "Max. Anzahl Nachrichten"},
            },
            "required": ["conversation_id"],
        },
    ),
    Tool(
        name="search_sender_history",
        description="Letzte E-Mails eines bestimmten Absenders abrufen (neueste zuerst). Nützlich für Kontext über die bisherige Kommunikation.",
        inputSchema={
            "type": "object",
            "properties": {
                "sender_email": {"type": "string", "description": "E-Mail-Adresse des Absenders"},
                "top": {"type": "integer", "default": 5, "description": "Max. Anzahl E-Mails"},
            },
            "required": ["sender_email"],
        },
    ),
    Tool(
        name="search_my_replies",
        description=(
            "Letzte vom Owner (Anthony) GESENDETE E-Mails an einen bestimmten "
            "Empfänger abrufen (aus 'Gesendete Elemente', neueste zuerst). "
            "Nützlich als Stil-Anker für Antwort-Entwürfe: So schreibt Anthony "
            "wirklich an genau diesen Kontakt (Ton, Länge, Schlussformel)."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "recipient_email": {"type": "string", "description": "E-Mail-Adresse des Empfängers (der Kontakt, an den geantwortet wird)"},
                "top": {"type": "integer", "default": 3, "description": "Max. Anzahl gesendeter E-Mails"},
            },
            "required": ["recipient_email"],
        },
    ),
    Tool(
        name="search_emails",
        description="Volltextsuche über alle E-Mails. Findet relevante Nachrichten zu einem Thema.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchbegriff(e)"},
                "top": {"type": "integer", "default": 5},
            },
            "required": ["query"],
        },
    ),
    # ── Outlook-Kategorie & Ordner-Tools (original) ──────────
    Tool(
        name="set_email_categories",
        description="Outlook-Kategorien auf einer E-Mail setzen (z.B. 'Wichtig', 'Newsletter'). Ersetzt bestehende Kategorien.",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Graph-API Message-ID"},
                "categories": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Liste von Kategorienamen (z.B. ['Wichtig'] oder ['Newsletter'])",
                },
            },
            "required": ["message_id", "categories"],
        },
    ),
    Tool(
        name="move_email_to_folder",
        description="E-Mail in einen bestehenden Inbox-Subfolder verschieben. NUR diese Ordner sind erlaubt: System, Newsletter, Junk, Kalender. KEINE neuen Ordner erstellen.",
        inputSchema={
            "type": "object",
            "properties": {
                "message_id": {"type": "string", "description": "Graph-API Message-ID"},
                "folder_name": {"type": "string", "enum": ["System", "Newsletter", "Junk", "Kalender"], "description": "Ziel-Ordnername (nur System, Newsletter, Junk oder Kalender)"},
            },
            "required": ["message_id", "folder_name"],
        },
    ),
    # ── Teams-Chat-Tools ─────────────────────────────────────────
    Tool(
        name="list_chats",
        description="Alle Teams-Chats (1:1 und Gruppen) auflisten, neueste zuerst.",
        inputSchema={
            "type": "object",
            "properties": {
                "top": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="list_chat_messages",
        description="Letzte Nachrichten eines Teams-Chats lesen.",
        inputSchema={
            "type": "object",
            "properties": {
                "chat_id": {"type": "string", "description": "ID des Chats"},
                "top": {"type": "integer", "default": 20},
            },
            "required": ["chat_id"],
        },
    ),
    Tool(
        name="get_meeting_transcript",
        description="Transkript eines Online-Meetings als Text abrufen. Nützlich für Meeting-Zusammenfassungen.",
        inputSchema={
            "type": "object",
            "properties": {
                "meeting_id": {"type": "string", "description": "ID des Online-Meetings"},
            },
            "required": ["meeting_id"],
        },
    ),
    # ── OneDrive/SharePoint-Tools ────────────────────────────────
    Tool(
        name="search_files",
        description="Volltextsuche über OneDrive-Dateien. Findet Dokumente, Offerten, Rechnungen etc.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Suchbegriff(e)"},
                "top": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="list_files",
        description="Inhalte eines OneDrive-Ordners auflisten.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "/", "description": "Ordnerpfad (z.B. /Dokumente/Kunden)"},
                "top": {"type": "integer", "default": 20},
            },
        },
    ),
    Tool(
        name="read_file_metadata",
        description="Metadaten einer OneDrive-Datei lesen (Name, Grösse, URL, letztes Änderungsdatum).",
        inputSchema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "OneDrive Item-ID"},
            },
            "required": ["item_id"],
        },
    ),
    Tool(
        name="download_file",
        description=(
            "OneDrive-Datei herunterladen und lokal cachen. "
            "Bei PDFs wird der Text automatisch extrahiert. "
            "Gibt den lokalen Pfad und ggf. den extrahierten Text zurück."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "item_id": {"type": "string", "description": "OneDrive Item-ID"},
                "extract_text": {
                    "type": "boolean",
                    "default": True,
                    "description": "Bei PDF: Text automatisch extrahieren",
                },
                "max_text_chars": {
                    "type": "integer",
                    "default": 80000,
                    "description": "Maximale Textlänge bei Extraktion",
                },
            },
            "required": ["item_id"],
        },
    ),
    # ── Planner-Tools ────────────────────────────────────────────
    Tool(
        name="list_planner_tasks",
        description="Eigene Microsoft Planner-Aufgaben auflisten.",
        inputSchema={
            "type": "object",
            "properties": {
                "top": {"type": "integer", "default": 30},
            },
        },
    ),
    Tool(
        name="get_planner_task",
        description="Details einer einzelnen Planner-Aufgabe abrufen.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="create_planner_task",
        description="Neue Aufgabe in einem Planner-Plan erstellen.",
        inputSchema={
            "type": "object",
            "properties": {
                "plan_id": {"type": "string", "description": "ID des Planner-Plans"},
                "title": {"type": "string", "description": "Titel der Aufgabe"},
                "bucket_id": {"type": "string", "description": "Optionale Bucket-ID"},
                "due_date": {"type": "string", "description": "Fälligkeitsdatum ISO 8601"},
            },
            "required": ["plan_id", "title"],
        },
    ),
    Tool(
        name="list_planner_plans",
        description="Alle Planner-Pläne des Users auflisten.",
        inputSchema={
            "type": "object",
            "properties": {},
        },
    ),
]


# Tools, die den Microsoft-365-Zustand veraendern. Sie sind bewusst von den
# Lese-Tools getrennt, weil Hermes Tools nur auf Server-Ebene filtern kann
# (``enabled_toolsets``): der Triage-Agent laeuft auf einer Registrierung im
# Modus ``safe`` und kann diese Tools damit nicht aufrufen, waehrend der
# Chat-Agent eine zweite Registrierung im Modus ``admin`` erhaelt.
#
# ``create_draft`` fehlt hier absichtlich: ein Entwurf ist keine Aussenwirkung
# (der Versand bleibt HITL-pflichtig). Es wird stattdessen ueber ``_DRAFT_TOOLS``
# gesteuert, weil es je nach Betriebsart in eine andere Registrierung gehoert.
_MUTATING_TOOLS: frozenset[str] = frozenset({
    "set_email_categories",
    "move_email_to_folder",
    "send_email",
    "mark_as_read",
    "create_calendar_event",
    "create_planner_task",
})

# Entwurfs-Tools wandern im Zwei-Pass-Betrieb aus ``safe`` nach ``admin``. Grund:
# Dort schreibt ausschliesslich der separate Schreib-Pass den Entwurf, waehrend
# der Klassifikations-Lauf nur einordnet. Blieb ``create_draft`` im
# Klassifikations-Toolset, konnte dieser einen Platzhalter erzeugen (nur Anrede
# und Gruss), der den echten Schreib-Pass verdraengte -- der Prompt verbot es
# zwar, der Skill verlangte es, und das Modell folgte sporadisch dem Skill.
_DRAFT_TOOLS: frozenset[str] = frozenset({"create_draft"})


def _tool_mode() -> str:
    """Betriebsmodus des Servers: ``safe``, ``admin`` oder ``full``.

    - ``safe`` (Default): alle Tools ausser den zustandsveraendernden. Der
      Triage-Agent laeuft hierauf, damit das LLM den Outlook-Zustand nicht
      selbst mutiert -- Kategorien und Moves setzt ausschliesslich das Backend.
    - ``admin``: nur die zustandsveraendernden Tools. Ergaenzt ``safe`` fuer den
      Chat-Agenten, ohne die Lese-Tools ein zweites Mal in den Kontext zu legen.
    - ``full``: alles (Rueckfallebene, falls die Env-Var fehlt).
    """
    mode = (os.environ.get("GRAPH_TOOL_MODE") or "full").strip().lower()
    return mode if mode in ("safe", "admin", "full") else "full"


def _triage_may_draft() -> bool:
    """Darf der ``safe``-Modus Entwuerfe erstellen? Nur im Einpass-Betrieb.

    Das Backend setzt ``GRAPH_TRIAGE_DRAFT`` aus ``two_pass_draft``. Default ist
    ``1`` (erlaubt), damit eine Standalone-Nutzung des Servers ohne Backend-Config
    nicht stillschweigend Funktionalitaet verliert.
    """
    raw = (os.environ.get("GRAPH_TRIAGE_DRAFT") or "1").strip().lower()
    return raw not in ("0", "false", "no")


def _admin_tools() -> frozenset[str]:
    """Tools der ``admin``-Registrierung.

    Im Zwei-Pass-Betrieb kommen die Entwurfs-Tools dazu, damit der Schreib-Pass
    sie behaelt, waehrend die Triage sie nicht mehr sieht. Die Aufteilung
    zwischen ``safe`` und ``admin`` bleibt in beiden Betriebsarten disjunkt.
    """
    if _triage_may_draft():
        return _MUTATING_TOOLS
    return _MUTATING_TOOLS | _DRAFT_TOOLS


def _visible_tools() -> list[Tool]:
    """Tool-Liste gemaess Betriebsmodus."""
    mode = _tool_mode()
    if mode == "safe":
        return [t for t in TOOLS if t.name not in _admin_tools()]
    if mode == "admin":
        return [t for t in TOOLS if t.name in _admin_tools()]
    return TOOLS


server = Server("taskpilot-graph")
_client: GraphClient | None = None


def _get_client() -> GraphClient:
    global _client
    if _client is None:
        config = GraphConfig.from_env()
        if not config.is_configured:
            raise RuntimeError(
                "Graph API nicht konfiguriert. Setze GRAPH_TENANT_ID, "
                "GRAPH_CLIENT_ID, GRAPH_CLIENT_SECRET, GRAPH_USER_EMAIL."
            )
        _client = GraphClient(config)
    return _client


@server.list_tools()
async def list_tools() -> list[Tool]:
    return _visible_tools()


@server.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    t0 = time.monotonic()
    logger.info("Tool %s aufgerufen: %s", name, {k: str(v)[:100] for k, v in arguments.items()})
    # Zweite Verteidigungslinie: Ausblenden in ``list_tools`` genuegt nicht, weil
    # ein Modell einen Tool-Namen auch erraten kann. Der Dispatch weist ab.
    if name not in {t.name for t in _visible_tools()}:
        logger.warning("Tool %s im Modus %s nicht verfuegbar -- abgewiesen", name, _tool_mode())
        return [TextContent(
            type="text",
            text=(
                f"Tool '{name}' ist in diesem Kontext nicht verfuegbar. "
                "Kategorien und Ordner-Zuweisungen setzt das Backend deterministisch "
                "aus deiner Klassifikation -- gib sie im JSON-Ergebnis an."
            ),
        )]
    try:
        client = _get_client()
    except RuntimeError as e:
        logger.error("Client-Fehler: %s", e)
        return [TextContent(type="text", text=str(e))]

    try:
        if name == "list_email_folders":
            folders = await client.list_folders()
            result = [
                {"id": f.get("id", ""), "displayName": f.get("displayName", ""), "totalItemCount": f.get("totalItemCount", 0)}
                for f in folders
            ]
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_emails":
            data = await client.list_emails(
                folder=arguments.get("folder", "inbox"),
                top=arguments.get("top", 10),
                skip=arguments.get("skip", 0),
                filter_str=arguments.get("filter"),
            )
            emails = []
            for msg in data.get("value", []):
                emails.append({
                    "id": msg.get("id"),
                    "subject": msg.get("subject"),
                    "from": msg.get("from", {}).get("emailAddress", {}).get("address"),
                    "receivedDateTime": msg.get("receivedDateTime"),
                    "isRead": msg.get("isRead"),
                    "bodyPreview": msg.get("bodyPreview", "")[:200],
                    "categories": msg.get("categories", []),
                    "inferenceClassification": msg.get("inferenceClassification"),
                    "importance": msg.get("importance"),
                    "hasAttachments": msg.get("hasAttachments"),
                })
            return [TextContent(type="text", text=json.dumps(emails, indent=2, ensure_ascii=False))]

        elif name == "get_email":
            msg = await client.get_email(arguments["message_id"])
            return [TextContent(type="text", text=json.dumps(msg, indent=2, ensure_ascii=False))]

        elif name == "get_email_categories":
            cats = await client.get_email_categories(arguments["message_id"])
            return [TextContent(type="text", text=json.dumps(cats, indent=2, ensure_ascii=False))]

        elif name == "get_email_attachments":
            msg_id = arguments["message_id"]
            attachments = await client.get_email_attachments(msg_id)
            image_types = {
                "image/png", "image/jpeg", "image/jpg",
                "image/gif", "image/webp", "image/bmp",
            }
            safe_msg = re.sub(r"[^A-Za-z0-9_-]", "_", msg_id)[:60] or "msg"
            out_dir = Path(
                os.environ.get("TP_ATTACHMENT_DIR", "/tmp/taskpilot-attachments")
            ) / safe_msg
            results = []
            for att in attachments:
                name_a = att.get("name") or "attachment"
                ctype = (att.get("contentType") or "").lower()
                is_file = "fileAttachment" in (att.get("@odata.type") or "")
                entry = {
                    "name": name_a,
                    "content_type": ctype,
                    "size": att.get("size"),
                    "is_image": ctype in image_types,
                }
                content_b64 = att.get("contentBytes")
                if is_file and content_b64 and ctype in image_types:
                    try:
                        out_dir.mkdir(parents=True, exist_ok=True)
                        safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", name_a)[:80] or "image"
                        fpath = out_dir / safe_name
                        fpath.write_bytes(base64.b64decode(content_b64))
                        entry["path"] = str(fpath)
                    except Exception as exc:  # noqa: BLE001
                        entry["error"] = f"konnte nicht gespeichert werden: {exc}"
                results.append(entry)
            hint = (
                "Für Bild-Anhänge mit 'path' kannst du vision_analyze(image_url=<path>, "
                "user_prompt='Beschreibe den Inhalt für die E-Mail-Triage') aufrufen."
            )
            return [TextContent(
                type="text",
                text=json.dumps({"attachments": results, "hint": hint}, indent=2, ensure_ascii=False),
            )]

        elif name == "create_draft":
            draft = await client.create_draft(
                subject=arguments["subject"],
                body_html=arguments["body_html"],
                to_recipients=arguments["to_recipients"],
                cc_recipients=arguments.get("cc_recipients"),
                reply_to_id=arguments.get("reply_to_id"),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "id": draft.get("id"),
                    "subject": draft.get("subject"),
                    "status": "draft_created",
                    "approval_required": True,
                    "hinweis": "Entwurf muss vor Versand durch den Berater genehmigt werden.",
                }, indent=2, ensure_ascii=False),
            )]

        elif name == "send_email":
            await client.send_draft(arguments["message_id"])
            return [TextContent(type="text", text=json.dumps({
                "status": "sent",
                "message_id": arguments["message_id"],
            }, indent=2))]

        elif name == "mark_as_read":
            await client.mark_as_read(arguments["message_id"])
            return [TextContent(type="text", text=json.dumps({
                "status": "marked_as_read",
                "message_id": arguments["message_id"],
            }, indent=2))]

        elif name == "list_calendar_events":
            events = await client.list_events(
                start=arguments["start"],
                end=arguments["end"],
                top=arguments.get("top", 20),
            )
            result = []
            for ev in events:
                result.append({
                    "id": ev.get("id"),
                    "subject": ev.get("subject"),
                    "start": ev.get("start", {}).get("dateTime"),
                    "end": ev.get("end", {}).get("dateTime"),
                    "isAllDay": ev.get("isAllDay"),
                    "showAs": ev.get("showAs"),
                    "location": (ev.get("location") or {}).get("displayName"),
                    "bodyPreview": ev.get("bodyPreview", "")[:200],
                })
            output = {
                "timezone": "Europe/Zurich",
                "events": result,
            }
            return [TextContent(type="text", text=json.dumps(output, indent=2, ensure_ascii=False))]

        elif name == "get_calendar_event":
            ev = await client.get_event(arguments["event_id"])
            return [TextContent(type="text", text=json.dumps(ev, indent=2, ensure_ascii=False))]

        elif name == "create_calendar_event":
            ev = await client.create_event(
                subject=arguments["subject"],
                start=arguments["start"],
                end=arguments["end"],
                body=arguments.get("body"),
                categories=arguments.get("categories"),
                location=arguments.get("location"),
                show_as=arguments.get("show_as", "busy"),
            )
            return [TextContent(type="text", text=json.dumps({
                "id": ev.get("id"),
                "subject": ev.get("subject"),
                "start": ev.get("start", {}).get("dateTime"),
                "end": ev.get("end", {}).get("dateTime"),
                "timezone": "Europe/Zurich",
                "status": "event_created",
            }, indent=2, ensure_ascii=False))]

        elif name == "find_free_slots":
            slots = await client.find_free_slots(
                start=arguments["start"],
                end=arguments["end"],
                duration_minutes=arguments.get("duration_minutes", 60),
            )
            return [TextContent(type="text", text=json.dumps(slots, indent=2, ensure_ascii=False))]

        elif name == "get_thread":
            msgs = await client.get_conversation_messages(
                conversation_id=arguments["conversation_id"],
                top=arguments.get("top", 10),
            )
            thread = []
            total_chars = 0
            max_total = 15000
            for msg in msgs:
                if total_chars >= max_total:
                    break
                sender = msg.get("from", {}).get("emailAddress", {})
                body_html = msg.get("body", {}).get("content", "")
                body_text = _html_to_text(body_html)[:3000]
                total_chars += len(body_text)
                thread.append({
                    "id": msg.get("id"),
                    "from": sender.get("address"),
                    "from_name": sender.get("name"),
                    "subject": msg.get("subject"),
                    "receivedDateTime": msg.get("receivedDateTime"),
                    "body_text": body_text,
                })
            logger.info("get_thread: %d Nachrichten, %d Zeichen Kontext", len(thread), total_chars)
            result_text = json.dumps(thread, indent=2, ensure_ascii=False)
            return [TextContent(type="text", text=result_text)]

        elif name == "search_sender_history":
            msgs = await client.search_sender_emails(
                sender_email=arguments["sender_email"],
                top=arguments.get("top", 5),
            )
            history = []
            for msg in msgs:
                sender = msg.get("from", {}).get("emailAddress", {})
                body_html = msg.get("body", {}).get("content", "")
                body_text = _html_to_text(body_html)[:1500]
                history.append({
                    "id": msg.get("id"),
                    "from": sender.get("address"),
                    "from_name": sender.get("name"),
                    "subject": msg.get("subject"),
                    "receivedDateTime": msg.get("receivedDateTime"),
                    "body_text": body_text,
                    "conversationId": msg.get("conversationId"),
                })
            logger.info("search_sender_history(%s): %d Ergebnisse", arguments["sender_email"], len(history))
            result_text = json.dumps(history, indent=2, ensure_ascii=False)
            return [TextContent(type="text", text=result_text)]

        elif name == "search_my_replies":
            msgs = await client.search_my_replies_to(
                recipient_email=arguments["recipient_email"],
                top=arguments.get("top", 3),
            )
            replies = []
            for msg in msgs:
                to_recipients = [
                    r.get("emailAddress", {}).get("address")
                    for r in msg.get("toRecipients", [])
                ]
                body_html = msg.get("body", {}).get("content", "")
                body_text = _html_to_text(body_html)[:1500]
                replies.append({
                    "id": msg.get("id"),
                    "to": to_recipients,
                    "subject": msg.get("subject"),
                    "sentDateTime": msg.get("sentDateTime"),
                    "body_text": body_text,
                    "conversationId": msg.get("conversationId"),
                })
            logger.info("search_my_replies(%s): %d Ergebnisse", arguments["recipient_email"], len(replies))
            result_text = json.dumps(replies, indent=2, ensure_ascii=False)
            return [TextContent(type="text", text=result_text)]

        elif name == "search_emails":
            msgs = await client.search_emails(
                query=arguments["query"],
                top=arguments.get("top", 5),
            )
            results = []
            for msg in msgs:
                sender = msg.get("from", {}).get("emailAddress", {})
                results.append({
                    "id": msg.get("id"),
                    "from": sender.get("address"),
                    "subject": msg.get("subject"),
                    "receivedDateTime": msg.get("receivedDateTime"),
                    "bodyPreview": msg.get("bodyPreview", "")[:300],
                })
            return [TextContent(type="text", text=json.dumps(results, indent=2, ensure_ascii=False))]

        elif name == "set_email_categories":
            result = await client.set_categories(
                message_id=arguments["message_id"],
                categories=arguments["categories"],
            )
            return [TextContent(type="text", text=json.dumps({
                "status": "categories_set",
                "message_id": arguments["message_id"],
                "categories": arguments["categories"],
            }, indent=2, ensure_ascii=False))]

        elif name == "move_email_to_folder":
            allowed = {"System", "Newsletter", "Junk", "Kalender"}
            folder = arguments["folder_name"]
            if folder not in allowed:
                return [TextContent(type="text", text=json.dumps({
                    "status": "error",
                    "message": f"Ordner '{folder}' nicht erlaubt. Nur: {', '.join(sorted(allowed))}",
                }, indent=2, ensure_ascii=False))]
            result = await client.move_to_folder(
                message_id=arguments["message_id"],
                folder_name=folder,
            )
            return [TextContent(type="text", text=json.dumps({
                "status": "moved",
                "message_id": arguments["message_id"],
                "folder": arguments["folder_name"],
                "new_id": result.get("id"),
            }, indent=2, ensure_ascii=False))]

        # ── Teams-Chat ────────────────────────────────────────────
        elif name == "list_chats":
            chats = await client.list_chats(top=arguments.get("top", 20))
            result = []
            for c in chats:
                preview = c.get("lastMessagePreview") or {}
                result.append({
                    "id": c.get("id"),
                    "topic": c.get("topic"),
                    "chatType": c.get("chatType"),
                    "lastMessage": {
                        "body": (preview.get("body") or {}).get("content", "")[:200],
                        "from": (preview.get("from") or {}).get("user", {}).get("displayName"),
                        "createdDateTime": preview.get("createdDateTime"),
                    } if preview else None,
                })
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_chat_messages":
            msgs = await client.list_chat_messages(
                chat_id=arguments["chat_id"],
                top=arguments.get("top", 20),
            )
            result = []
            for m in msgs:
                body = (m.get("body") or {}).get("content", "")
                if (m.get("body") or {}).get("contentType") == "html":
                    body = _html_to_text(body)
                sender = (m.get("from") or {}).get("user", {})
                result.append({
                    "id": m.get("id"),
                    "from": sender.get("displayName"),
                    "from_id": sender.get("id"),
                    "body": body[:2000],
                    "createdDateTime": m.get("createdDateTime"),
                    "messageType": m.get("messageType"),
                })
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "get_meeting_transcript":
            meeting_id = arguments["meeting_id"]
            transcripts = await client.list_meeting_transcripts(meeting_id)
            if not transcripts:
                return [TextContent(type="text", text=json.dumps({
                    "status": "no_transcript",
                    "message": "Kein Transkript für dieses Meeting verfügbar.",
                }, indent=2, ensure_ascii=False))]
            transcript_id = transcripts[0].get("id")
            content = await client.get_meeting_transcript_content(meeting_id, transcript_id)
            return [TextContent(type="text", text=content[:50000])]

        # ── OneDrive/SharePoint ──────────────────────────────────
        elif name == "search_files":
            items = await client.search_drive(
                query=arguments["query"],
                top=arguments.get("top", 10),
            )
            result = []
            for item in items:
                result.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "lastModified": item.get("lastModifiedDateTime"),
                    "webUrl": item.get("webUrl"),
                    "isFolder": bool(item.get("folder")),
                    "path": (item.get("parentReference") or {}).get("path", ""),
                })
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "list_files":
            items = await client.list_drive_items(
                path=arguments.get("path", "/"),
                top=arguments.get("top", 20),
            )
            result = []
            for item in items:
                result.append({
                    "id": item.get("id"),
                    "name": item.get("name"),
                    "size": item.get("size"),
                    "lastModified": item.get("lastModifiedDateTime"),
                    "webUrl": item.get("webUrl"),
                    "isFolder": bool(item.get("folder")),
                })
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "read_file_metadata":
            item = await client.get_drive_item(arguments["item_id"])
            return [TextContent(type="text", text=json.dumps({
                "id": item.get("id"),
                "name": item.get("name"),
                "size": item.get("size"),
                "lastModified": item.get("lastModifiedDateTime"),
                "webUrl": item.get("webUrl"),
                "mimeType": (item.get("file") or {}).get("mimeType"),
                "path": (item.get("parentReference") or {}).get("path", ""),
            }, indent=2, ensure_ascii=False))]

        elif name == "download_file":
            item_id = arguments["item_id"]
            extract = arguments.get("extract_text", True)
            max_chars = arguments.get("max_text_chars", 80000)

            meta = await client.get_drive_item(item_id)
            filename = meta.get("name", "unknown")
            mime = (meta.get("file") or {}).get("mimeType", "")
            size = meta.get("size", 0)

            cache_dir = Path("/tmp/taskpilot-files")
            cache_dir.mkdir(parents=True, exist_ok=True)
            safe_name = re.sub(r"[^\w.\-]", "_", filename)
            id_hash = hashlib.sha256(item_id.encode()).hexdigest()[:12]
            local_path = cache_dir / f"{id_hash}_{safe_name}"

            if not local_path.exists():
                data = await client.download_drive_item(item_id)
                local_path.write_bytes(data)
                logger.info("Datei heruntergeladen: %s (%d Bytes)", local_path, len(data))

            result: dict = {
                "local_path": str(local_path),
                "filename": filename,
                "size": size,
                "mimeType": mime,
            }

            if extract and filename.lower().endswith(".pdf"):
                try:
                    import pymupdf
                    doc = pymupdf.open(str(local_path))
                    pages_text = []
                    total = 0
                    for page in doc:
                        text = page.get_text()
                        if total + len(text) > max_chars:
                            pages_text.append(text[: max_chars - total])
                            break
                        pages_text.append(text)
                        total += len(text)
                    doc.close()
                    result["extracted_text"] = "\n\n--- Seite ---\n\n".join(pages_text)
                    result["pages_extracted"] = len(pages_text)
                except Exception as ex:
                    result["extraction_error"] = str(ex)

            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        # ── Planner ──────────────────────────────────────────────
        elif name == "list_planner_tasks":
            tasks = await client.list_planner_tasks(top=arguments.get("top", 30))
            result = []
            for t in tasks:
                result.append({
                    "id": t.get("id"),
                    "title": t.get("title"),
                    "percentComplete": t.get("percentComplete"),
                    "dueDateTime": t.get("dueDateTime"),
                    "createdDateTime": t.get("createdDateTime"),
                    "planId": t.get("planId"),
                    "bucketId": t.get("bucketId"),
                })
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        elif name == "get_planner_task":
            task = await client.get_planner_task(arguments["task_id"])
            return [TextContent(type="text", text=json.dumps(task, indent=2, ensure_ascii=False))]

        elif name == "create_planner_task":
            task = await client.create_planner_task(
                plan_id=arguments["plan_id"],
                title=arguments["title"],
                bucket_id=arguments.get("bucket_id"),
                due_date=arguments.get("due_date"),
            )
            return [TextContent(type="text", text=json.dumps({
                "id": task.get("id"),
                "title": task.get("title"),
                "status": "task_created",
            }, indent=2, ensure_ascii=False))]

        elif name == "list_planner_plans":
            plans = await client.list_planner_plans()
            result = []
            for p in plans:
                result.append({
                    "id": p.get("id"),
                    "title": p.get("title"),
                    "createdDateTime": p.get("createdDateTime"),
                })
            return [TextContent(type="text", text=json.dumps(result, indent=2, ensure_ascii=False))]

        return [TextContent(type="text", text=f"Unbekanntes Tool: {name}")]

    except Exception as e:
        elapsed = (time.monotonic() - t0) * 1000
        logger.error("Tool %s fehlgeschlagen nach %.0fms: %s: %s", name, elapsed, type(e).__name__, e)
        return [TextContent(type="text", text=f"Fehler: {type(e).__name__}: {e}")]


async def main():
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())

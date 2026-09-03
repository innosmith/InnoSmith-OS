"""Router für LLM-Chat-Konversationen mit Streaming via litellm."""

import asyncio
import json
import logging
import pathlib
import re
import threading
import time
import uuid

import litellm
import markdown as md_lib
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sse_starlette.sse import EventSourceResponse

from app.auth.deps import get_current_user, require_role
from app.database import get_db, async_session
from app.models import AgentJob, BoardColumn, Project, Task, User
from app.models.models import LlmConversation, LlmMessage

litellm.drop_params = True

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/chat", tags=["chat"])

# Name des Hermes-nativen Sandbox-Tools (MCP-Praefix mcp_<server>_<tool>). Der
# Agent nutzt es fuer Code-Ausfuehrung; wir binden dessen Workspace an die
# Konversation und rendern die erzeugten Artefakte inline (Feature-Paritaet
# mit dem alten Code-Modus).
_SANDBOX_EXEC_TOOL = "mcp_sandbox_execute_code"

# Maschinenlesbarer Marker aus dem Sandbox-Tool-Ergebnis (siehe
# src/mcp-sandbox/server.py): <!--tp-exec:SCOPE:name1|name2-->. Daraus baut das
# Backend den Frontend-Artefakt-Marker (<!--tp-artifacts:...-->).
_EXEC_MARKER_RE = re.compile(r"<!--tp-exec:([A-Za-z0-9_-]+):([^>]*)-->")


# Trenner zwischen den Teilen eines Gesprächs, das als **ein** Text maskiert
# wird. Getrennte Maskierungsläufe vergäben pro Turn andere Deckadressen, und
# dann hiesse dieselbe Person in Runde drei anders als in Runde eins -- für das
# Modell zwei Menschen.
_TRENNER = "\u241e"


def _artifacts_marker(scope: str | None, names: list[str] | None) -> str:
    """Frontend-Marker fuer erzeugte Sandbox-Artefakte (gleiches Format wie
    code_execute._artifacts_marker). Das ChatPage parst ihn und rendert die
    Dateien inline (Bilder/HTML spielbar, mit Vergroessern/Vollbild)."""
    if not scope or not names:
        return ""
    joined = "|".join(n for n in names if n)
    if not joined:
        return ""
    return f"\n\n<!--tp-artifacts:{scope}:{joined}-->"

# System-Hinweis für Deep Research (der /messages-Pfad bedient nur noch den
# Deep-Research-Modus mit öffentlichen Recherche-Modellen, z. B. Perplexity).
# Der Agent-Modus (InnoPilot) läuft über den eigenen /agent-Pfad.
_DEEP_RESEARCH_SYSTEM_HINT = (
    "Du führst eine vertiefte Recherche durch. Recherchiere gründlich, gewichte "
    "die Quellen kritisch und fasse die Ergebnisse strukturiert zusammen; belege "
    "Kernaussagen mit Quellen. Angehängte/angepinnte Dokumente in dieser "
    "Konversation kennst du vollständig — beziehe dich bei Rückfragen direkt darauf. "
    "Sprache: Schweizer Hochdeutsch (ss statt ß, korrekte Umlaute)."
)


# ── Datei-Anhänge als LLM-Kontext (Dokumenten-Kontext-Brücke) ──

_UPLOADS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "uploads"
_CHAT_UPLOAD_SUBFOLDER = "chat"
_CHAT_UPLOAD_MAX_SIZE = 10 * 1024 * 1024  # 10 MB
# Erlaubte Endungen für Chat-Kontext-Dateien (Text + PDF/DOCX/XLSX). Bilder
# laufen über einen separaten Vision-Pfad und sind hier bewusst ausgeschlossen.
_CHAT_UPLOAD_ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".csv", ".json", ".xml", ".yaml", ".yml",
    ".py", ".js", ".ts", ".html", ".css", ".sql", ".sh",
    ".log", ".ini", ".toml", ".cfg", ".conf",
    ".pdf", ".docx", ".xlsx",
}


@router.post("/uploads")
async def upload_chat_context_file(
    file: UploadFile,
    _user: User = Depends(require_role("owner")),
):
    """Lädt eine Datei als Chat-/Agent-Kontext hoch (mit ClamAV-Scan).

    Liefert eine `upload_id` zurück, die als `local_upload`-Kontextquelle an die
    Chat-/Agent-Endpoints übergeben werden kann. Der Inhalt wird beim Senden
    serverseitig via `context_resolver` extrahiert.
    """
    from app.routers.uploads import _scan_with_clamav

    ext = pathlib.Path(file.filename or "datei").suffix.lower()
    if ext not in _CHAT_UPLOAD_ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Dateityp '{ext or 'unbekannt'}' wird nicht unterstützt. "
                "Erlaubt: PDF, DOCX, XLSX und Textformate."
            ),
        )

    data = await file.read()
    if len(data) > _CHAT_UPLOAD_MAX_SIZE:
        raise HTTPException(status_code=400, detail="Datei zu gross (max 10 MB)")
    if not data:
        raise HTTPException(status_code=400, detail="Leere Datei")

    is_clean = await _scan_with_clamav(data)
    if not is_clean:
        raise HTTPException(status_code=422, detail="Datei wurde als schädlich erkannt")

    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = _UPLOADS_DIR / _CHAT_UPLOAD_SUBFOLDER / stored_name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)

    return {
        "upload_id": f"{_CHAT_UPLOAD_SUBFOLDER}/{stored_name}",
        "name": file.filename or stored_name,
        "mime_type": file.content_type or "",
    }


@router.get("/conversations/{conversation_id}/context-items")
async def list_context_items(
    conversation_id: uuid.UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Angepinnte Kontext-Dokumente einer Konversation auflisten (ohne Volltext)."""
    from app.models import ConversationContextItem

    result = await db.execute(
        select(ConversationContextItem)
        .where(ConversationContextItem.conversation_id == conversation_id)
        .order_by(ConversationContextItem.created_at)
    )
    return {
        "items": [
            {
                "id": str(item.id),
                "name": item.name,
                "source_type": item.source_type,
                "char_count": item.char_count,
                "pinned": item.pinned,
                "created_at": item.created_at.isoformat(),
            }
            for item in result.scalars().all()
        ]
    }


@router.patch("/conversations/{conversation_id}/context-items/{item_id}")
async def update_context_item(
    conversation_id: uuid.UUID,
    item_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Kontext-Dokument an-/abpinnen (abgepinnte werden nicht mehr injiziert)."""
    from app.models import ConversationContextItem

    result = await db.execute(
        select(ConversationContextItem).where(
            ConversationContextItem.id == item_id,
            ConversationContextItem.conversation_id == conversation_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Kontext-Dokument nicht gefunden")
    if "pinned" in body:
        item.pinned = bool(body["pinned"])
    await db.flush()
    return {"id": str(item.id), "pinned": item.pinned}


@router.delete("/conversations/{conversation_id}/context-items/{item_id}")
async def delete_context_item(
    conversation_id: uuid.UUID,
    item_id: uuid.UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Kontext-Dokument endgültig aus der Konversation entfernen."""
    from app.models import ConversationContextItem

    result = await db.execute(
        select(ConversationContextItem).where(
            ConversationContextItem.id == item_id,
            ConversationContextItem.conversation_id == conversation_id,
        )
    )
    item = result.scalar_one_or_none()
    if not item:
        raise HTTPException(status_code=404, detail="Kontext-Dokument nicht gefunden")
    await db.delete(item)
    return {"ok": True}


@router.get("/conversations")
async def list_conversations(
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    task_id: uuid.UUID | None = None,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Alle Konversationen (paginiert, neueste zuerst).

    Eine kombinierte Abfrage mit korrelierten Subqueries vermeidet N+1.
    """

    msg_count_sq = (
        select(func.count())
        .select_from(LlmMessage)
        .where(LlmMessage.conversation_id == LlmConversation.id)
        .scalar_subquery()
        .label("msg_count")
    )

    last_preview_sq = (
        select(func.substr(LlmMessage.content, 1, 125))
        .where(LlmMessage.conversation_id == LlmConversation.id)
        .order_by(LlmMessage.created_at.desc())
        .limit(1)
        .scalar_subquery()
        .label("last_preview")
    )

    from sqlalchemy import or_
    user_filter = or_(LlmConversation.user_id == user.id, LlmConversation.user_id.is_(None))

    count_q = select(func.count()).select_from(LlmConversation).where(user_filter)
    if task_id:
        count_q = count_q.where(LlmConversation.task_id == task_id)
    total = (await db.execute(count_q)).scalar_one()

    q = (
        select(LlmConversation, msg_count_sq, last_preview_sq)
        .where(user_filter)
        .order_by(LlmConversation.updated_at.desc())
        .offset(skip)
        .limit(limit)
    )
    if task_id:
        q = q.where(LlmConversation.task_id == task_id)

    result = await db.execute(q)

    items = []
    for row in result.all():
        conv = row[0]
        msg_count = row[1] or 0
        last_raw = row[2]
        last_preview = None
        if last_raw:
            last_preview = (last_raw[:120] + "...") if len(last_raw) > 120 else last_raw

        items.append({
            "id": str(conv.id),
            "title": conv.title,
            "task_id": str(conv.task_id) if conv.task_id else None,
            "model": conv.model,
            "mode": conv.mode,
            "total_tokens": conv.total_tokens,
            "total_cost_usd": float(conv.total_cost_usd),
            "created_at": conv.created_at.isoformat(),
            "updated_at": conv.updated_at.isoformat(),
            "message_count": msg_count,
            "last_message_preview": last_preview,
        })

    return {"items": items, "total": total}


@router.post("/conversations")
async def create_conversation(
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Neue Konversation erstellen."""
    from app.services.llm_defaults import get_default_local_model_from_settings

    settings = user.settings or {}
    fallback = get_default_local_model_from_settings(settings)
    default_model = settings.get("llm_default_model") or fallback
    default_temp = settings.get("llm_default_temperature", 0.7)

    conv = LlmConversation(
        title=body.get("title"),
        task_id=body.get("task_id"),
        user_id=user.id,
        model=body.get("model", default_model),
        mode=body.get("mode", "agent"),
        temperature=body.get("temperature", default_temp),
        grounding=body.get("grounding") or {},
    )
    db.add(conv)
    await db.flush()
    return {
        "id": str(conv.id),
        "title": conv.title,
        "task_id": str(conv.task_id) if conv.task_id else None,
        "model": conv.model,
        "mode": conv.mode,
        "temperature": conv.temperature,
        "thinking_mode": conv.thinking_mode,
        "grounding": conv.grounding or {},
        "total_tokens": conv.total_tokens,
        "total_cost_usd": float(conv.total_cost_usd),
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
    }


@router.delete("/conversations")
async def delete_all_conversations(
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Alle Chat-Konversationen des Users löschen (Nachrichten per ON DELETE CASCADE)."""
    res = await db.execute(
        delete(LlmConversation).where(LlmConversation.user_id == user.id)
    )
    return {"ok": True, "deleted": res.rowcount or 0}


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Konversation mit allen Nachrichten laden."""
    result = await db.execute(
        select(LlmConversation)
        .options(selectinload(LlmConversation.messages))
        .where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    return {
        "id": str(conv.id),
        "title": conv.title,
        "task_id": str(conv.task_id) if conv.task_id else None,
        "model": conv.model,
        "mode": conv.mode,
        "temperature": conv.temperature,
        "thinking_mode": conv.thinking_mode,
        "grounding": conv.grounding or {},
        "total_tokens": conv.total_tokens,
        "total_cost_usd": float(conv.total_cost_usd),
        "created_at": conv.created_at.isoformat(),
        "updated_at": conv.updated_at.isoformat(),
        "messages": [
            {
                "id": str(msg.id),
                "conversation_id": str(msg.conversation_id),
                "role": msg.role,
                "content": msg.content,
                "model": msg.model,
                "tokens": msg.tokens,
                "cost_usd": float(msg.cost_usd) if msg.cost_usd else None,
                "attachments": msg.attachments,
                "citations": msg.citations,
                "thinking": msg.thinking,
                "residuals": msg.residuals or [],
                "created_at": msg.created_at.isoformat(),
            }
            for msg in conv.messages
        ],
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: uuid.UUID,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Konversation löschen."""
    result = await db.execute(
        select(LlmConversation).where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")
    await db.delete(conv)
    return {"ok": True}


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Konversation aktualisieren (Titel, Modell, Temperatur)."""
    result = await db.execute(
        select(LlmConversation).where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    if "title" in body:
        conv.title = body["title"]
    if "model" in body:
        conv.model = body["model"]
    if "temperature" in body:
        conv.temperature = body["temperature"]
    if "mode" in body:
        conv.mode = body["mode"]
    if "thinking_mode" in body:
        from app.services import denkstufen

        conv.thinking_mode = denkstufen.normalisiere(body["thinking_mode"])
    if "grounding" in body:
        conv.grounding = body["grounding"] or {}

    await db.flush()
    return {
        "id": str(conv.id),
        "title": conv.title,
        "model": conv.model,
        "mode": conv.mode,
        "temperature": conv.temperature,
        "thinking_mode": conv.thinking_mode,
        "grounding": conv.grounding or {},
    }


@router.post("/conversations/{conversation_id}/messages/batch")
async def batch_save_messages(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Mehrere Nachrichten synchron speichern (fuer Web-Suche etc.)."""
    result = await db.execute(
        select(LlmConversation).where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    messages_data = body.get("messages", [])
    saved = []
    for m in messages_data:
        msg = LlmMessage(
            conversation_id=conv.id,
            role=m.get("role", "user"),
            content=m.get("content", ""),
            tokens=m.get("tokens"),
            cost_usd=m.get("cost_usd"),
            citations=m.get("citations", []),
        )
        db.add(msg)
        await db.flush()
        saved.append({"id": str(msg.id), "role": msg.role})

    if not conv.title and messages_data:
        first_user = next((m for m in messages_data if m.get("role") == "user"), None)
        if first_user:
            conv.title = (first_user["content"][:80] + "...") if len(first_user["content"]) > 80 else first_user["content"]

    return {"ok": True, "saved": saved}


def _is_gemini_deep_research(m: str) -> bool:
    return m.startswith("gemini/deep-research") or m in (
        "deep-research-preview-04-2026",
        "deep-research-max-preview-04-2026",
    )


def _build_research_briefing(
    user_content: str,
    history: list[dict],
    pinned_block: str,
) -> str:
    """Research-Briefing für Deep Research: Verlauf + angepinnte Dokumente.

    Deep Research (Gemini Interactions API) ist zustandslos — vorher ging nur
    die nackte Frage raus und Rückfragen verloren jeden Bezug. Das Briefing
    liefert den Konversationskontext und die angepinnten Dokumente mit, damit
    Folge-Recherchen auf dem Gesprächsstand aufsetzen.
    """
    parts: list[str] = []
    if pinned_block:
        parts.append(
            "## Angehängte Dokumente (Kontext der Konversation)\n\n"
            + pinned_block[:60_000]
        )
    if history:
        lines = []
        for m in history[-12:]:
            role = "Frage" if m["role"] == "user" else "Antwort"
            content = m["content"]
            if len(content) > 2500:
                content = content[:2500] + " […]"
            lines.append(f"**{role}:** {content}")
        parts.append("## Bisheriger Gesprächsverlauf\n\n" + "\n\n".join(lines))
    parts.append("## Rechercheauftrag\n\n" + user_content)
    if len(parts) == 1:
        return user_content
    return (
        "Kontext für diese Recherche (Konversation mit Vorwissen):\n\n"
        + "\n\n".join(parts)
    )


# <think>-Tags aus Antworten separieren (Qwen/Perplexity liefern Reasoning teils inline).
_THINK_RE = None


def _split_think_tags(text: str) -> tuple[str, str]:
    """Trennt ``<think>…</think>``-Blöcke vom sichtbaren Antwort-Text."""
    import re

    global _THINK_RE
    if _THINK_RE is None:
        _THINK_RE = re.compile(r"<think>(.*?)</think>\s*", re.DOTALL)
    thinking_parts = _THINK_RE.findall(text)
    cleaned = _THINK_RE.sub("", text).strip()
    return cleaned, "\n".join(t.strip() for t in thinking_parts)


@router.post("/conversations/{conversation_id}/messages")
async def send_message(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Deep-Research-Nachricht senden und Antwort als SSE streamen.

    Seit der Modus-Vereinheitlichung bedient dieser Pfad nur noch Deep Research
    (öffentliche Recherche-Modelle). Zwei Backends:
    - Gemini Deep Research: Interactions API (eigener Pfad, ``generate_gemini_research``).
    - Übrige Recherche-Modelle (z. B. Perplexity): Hermes-Runtime, Preset ``chat``
      (keine MCP-Tools, aber Session-Kompression, Verlauf und angepinnte Dokumente).

    Der interaktive Agent (InnoPilot) läuft über den eigenen ``/agent``-Pfad.
    """
    from app.services.conversation_context import (
        build_conversation_history,
        build_pinned_context_block,
        load_pinned_items,
        persist_context_sources,
    )

    result = await db.execute(
        select(LlmConversation)
        .options(selectinload(LlmConversation.messages))
        .where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    user_content = body.get("content", "")
    user_attachments = body.get("attachments", [])
    context_sources = body.get("context_sources", [])

    user_msg = LlmMessage(
        conversation_id=conv.id,
        role="user",
        content=user_content,
        attachments=user_attachments,
    )
    db.add(user_msg)
    await db.flush()

    # Chat-Lernkanal auch im Plain-Chat: "merk dir ..."-Absicht erfassen ->
    # Regel-Vorschlag (HITL).
    try:
        from app.services.learning import extract_teach_intent, record_chat_teach

        lesson = extract_teach_intent(user_content)
        if lesson:
            await record_chat_teach(db, content=lesson, conversation_id=str(conv.id))
    except Exception:  # noqa: BLE001 - best-effort, darf den Chat nie blockieren
        logger.warning("Chat-Teach-Erfassung (Plain-Chat) fehlgeschlagen")

    if not conv.title and len(conv.messages) <= 1:
        conv.title = user_content[:80] + ("..." if len(user_content) > 80 else "")

    # Modellwechsel-Fix: Das im Request mitgesendete Modell gilt sofort für
    # diese Antwort (und wird an der Konversation persistiert).
    if body.get("model"):
        conv.model = str(body["model"])
    if body.get("temperature") is not None:
        try:
            conv.temperature = float(body["temperature"])
        except (TypeError, ValueError):
            pass

    # Dokumente an die Konversation pinnen (einmalige Extraktion) und den
    # gesamten angepinnten Korpus für diesen Turn laden — Dokumente bleiben
    # damit über die ganze Konversation sichtbar, nicht nur im Upload-Request.
    await persist_context_sources(db, conv.id, context_sources)
    pinned_items = await load_pinned_items(db, conv.id)
    pinned_block = build_pinned_context_block(pinned_items)

    # Verlauf als echtes Message-Array (tokenbudgetiert, grosszügig) — die
    # Hermes-Session-Kompression übernimmt bei sehr langen Konversationen.
    history = build_conversation_history(
        [m for m in conv.messages if m.id != user_msg.id]
    )
    # Für die Hermes-Runtime steht der angepinnte Dokument-Korpus am Anfang
    # des Verlaufs (stabiler Präfix → prompt-cache-freundlich).
    hermes_history = history
    if pinned_block:
        hermes_history = [{
            "role": "user",
            "content": (
                "Folgende Dokumente sind in dieser Konversation angepinnt und "
                "bleiben dauerhaft verfügbar. Beziehe dich bei Antworten darauf:\n\n"
                + pinned_block
            ),
        }, *history]

    await db.commit()

    conv_id_str = str(conv.id)
    model = conv.model
    temperature = conv.temperature

    async def generate_gemini_research():
        """Gemini Deep Research via Interactions API (mit Kontext-Briefing)."""
        from app.services.gemini_research import stream_research

        full_response = ""
        full_thinking = ""
        gemini_model = model.replace("gemini/", "") if model.startswith("gemini/") else None
        briefing = _build_research_briefing(user_content, history, pinned_block)

        try:
            async for event in stream_research(briefing, model=gemini_model):
                if event["type"] == "thought":
                    full_thinking += event["content"] + "\n"
                    yield {"event": "thinking", "data": json.dumps({"content": event["content"]})}
                elif event["type"] == "text":
                    full_response += event["content"]
                    yield {"event": "chunk", "data": json.dumps({"content": event["content"]})}
                elif event["type"] == "status":
                    yield {"event": "status", "data": json.dumps({"content": event["content"]})}
                elif event["type"] == "error":
                    yield {"event": "error", "data": json.dumps({"error": event["content"]})}
                    return
                elif event["type"] == "done":
                    if event.get("content") and not full_response:
                        full_response = event["content"]
        except Exception as e:
            logger.exception("Gemini Deep Research Fehler")
            yield {"event": "error", "data": json.dumps({"error": "Deep Research fehlgeschlagen"})}
            return

        async with async_session() as save_db:
            assistant_msg = LlmMessage(
                conversation_id=uuid.UUID(conv_id_str),
                role="assistant",
                content=full_response,
                tokens=0,
                cost_usd=None,
            )
            save_db.add(assistant_msg)
            await save_db.commit()

        yield {
            "event": "done",
            "data": json.dumps({
                "message_id": str(assistant_msg.id),
                "tokens": 0,
                "reasoning_tokens": 0,
                "cost_usd": None,
                "content": full_response,
                "thinking": full_thinking.strip() if full_thinking else None,
            }),
        }

    if _is_gemini_deep_research(model):
        return EventSourceResponse(generate_gemini_research())

    async def generate_hermes_chat():
        """Deep Research über die Hermes-Runtime (Preset ``chat``, keine Tools).

        Die synchronen Hermes-Callbacks (Text/Reasoning) werden threadsicher
        in eine ``asyncio.Queue`` gebrückt und als dieselben SSE-Events wie
        bisher gestreamt (``thinking``/``chunk``/``done``/``error``) — das
        Frontend bleibt unverändert. Bricht der Client ab, wird der Agent
        via ``interrupt()`` kooperativ gestoppt.
        """
        from app.services.hermes_worker import build_chat_agent, ensure_runtime_ready

        if not await ensure_runtime_ready():
            yield {"event": "error", "data": json.dumps({"error": "LLM-Runtime nicht verfügbar"})}
            return

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def _emit(evt_type: str, payload: str):
            loop.call_soon_threadsafe(queue.put_nowait, (evt_type, payload))

        def on_text(text: str):
            if text:
                _emit("chunk", text)

        def on_reasoning(text: str):
            if text:
                _emit("thinking", text)

        try:
            agent = await asyncio.to_thread(
                build_chat_agent,
                model,
                preset="chat",
                temperature=temperature,
                on_text=on_text,
                on_reasoning=on_reasoning,
                # Eigener Session-Namespace pro Preset: die Plain-Chat-Session
                # (ohne Tools) darf die Agent-Session derselben Konversation
                # nicht überschreiben, falls der Modus gewechselt wird.
                session_id=f"chatplain-{conv_id_str}",
            )
        except Exception:
            logger.exception("Chat-Agent-Init fehlgeschlagen (Modell %s)", model)
            yield {"event": "error", "data": json.dumps({"error": "LLM-Initialisierung fehlgeschlagen"})}
            return

        def _run_sync() -> str:
            result = agent.run_conversation(
                user_content,
                system_message=_DEEP_RESEARCH_SYSTEM_HINT,
                conversation_history=list(hermes_history),
            )
            if isinstance(result, dict):
                return str(result.get("final_response") or "")
            return str(result or "")

        bot_task = asyncio.create_task(asyncio.to_thread(_run_sync))
        bot_task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

        full_response = ""
        full_thinking = ""
        try:
            while not bot_task.done():
                try:
                    evt_type, payload = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ts": int(time.time())})}
                    continue
                if evt_type == "chunk":
                    full_response += payload
                    yield {"event": "chunk", "data": json.dumps({"content": payload})}
                elif evt_type == "thinking":
                    full_thinking += payload
                    yield {"event": "thinking", "data": json.dumps({"content": payload})}

            while not queue.empty():
                evt_type, payload = queue.get_nowait()
                if evt_type == "chunk":
                    full_response += payload
                    yield {"event": "chunk", "data": json.dumps({"content": payload})}
                elif evt_type == "thinking":
                    full_thinking += payload
                    yield {"event": "thinking", "data": json.dumps({"content": payload})}

            final = bot_task.result()
        except asyncio.CancelledError:
            # Client hat abgebrochen: Hermes kooperativ stoppen, nichts speichern.
            try:
                agent.interrupt("Vom Benutzer abgebrochen.")
            except Exception:  # noqa: BLE001
                pass
            raise
        except Exception:
            logger.exception("Hermes-Chat fehlgeschlagen (Modell %s)", model)
            yield {"event": "error", "data": json.dumps({"error": "LLM-Antwort fehlgeschlagen"})}
            return

        if final and not full_response:
            # Kein Streaming (z. B. Modell ohne Delta-Support): Komplettantwort senden.
            clean, extra_think = _split_think_tags(final)
            if extra_think and not full_thinking:
                full_thinking = extra_think
            full_response = clean
            yield {"event": "chunk", "data": json.dumps({"content": clean})}
        else:
            clean, extra_think = _split_think_tags(full_response or final)
            if extra_think and not full_thinking:
                full_thinking = extra_think
            full_response = clean

        total_tokens_used = int(getattr(agent, "session_total_tokens", 0) or 0)
        cost_usd = 0.0
        if not model.startswith("ollama/") and model not in ("hermes", "nanobot", ""):
            try:
                cost_usd = litellm.cost_calculator.completion_cost(
                    model=model,
                    prompt="\n".join(m["content"] for m in hermes_history) + user_content,
                    completion=full_response,
                )
            except Exception:  # noqa: BLE001 - Kostenberechnung ist best-effort
                pass

        async with async_session() as save_db:
            assistant_msg = LlmMessage(
                conversation_id=uuid.UUID(conv_id_str),
                role="assistant",
                content=full_response,
                model=model,
                tokens=total_tokens_used,
                cost_usd=cost_usd if cost_usd > 0 else None,
            )
            save_db.add(assistant_msg)

            update_result = await save_db.execute(
                select(LlmConversation).where(LlmConversation.id == uuid.UUID(conv_id_str))
            )
            conv_update = update_result.scalar_one_or_none()
            if conv_update:
                conv_update.total_tokens = (conv_update.total_tokens or 0) + total_tokens_used
                conv_update.total_cost_usd = float(conv_update.total_cost_usd or 0) + cost_usd
            await save_db.commit()

        yield {
            "event": "done",
            "data": json.dumps({
                "message_id": str(assistant_msg.id),
                "tokens": total_tokens_used,
                "reasoning_tokens": 0,
                "cost_usd": round(cost_usd, 6) if cost_usd > 0 else None,
                "content": full_response,
                "thinking": full_thinking.strip() if full_thinking else None,
                "model": model,
            }),
        }

    return EventSourceResponse(generate_hermes_chat())


@router.post("/messages/{message_id}/create-task")
async def create_task_from_message(
    message_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Erstellt eine neue Aufgabe basierend auf einer Chat-Nachricht."""
    result = await db.execute(select(LlmMessage).where(LlmMessage.id == message_id))
    message = result.scalar_one_or_none()
    if not message:
        raise HTTPException(status_code=404, detail="Nachricht nicht gefunden")

    project_id = body.get("project_id")
    board_column_id = body.get("board_column_id")

    if not project_id or not board_column_id:
        proj_result = await db.execute(
            select(Project).where(Project.status == "active").order_by(Project.created_at).limit(1)
        )
        project = proj_result.scalar_one_or_none()
        if not project:
            raise HTTPException(status_code=400, detail="Kein aktives Projekt vorhanden")
        if not project_id:
            project_id = project.id

        col_result = await db.execute(
            select(BoardColumn)
            .where(BoardColumn.project_id == project_id)
            .order_by(BoardColumn.position)
            .limit(1)
        )
        column = col_result.scalar_one_or_none()
        if not column:
            raise HTTPException(status_code=400, detail="Keine Board-Spalte vorhanden")
        if not board_column_id:
            board_column_id = column.id

    max_pos_result = await db.execute(
        select(Task.board_position)
        .where(Task.board_column_id == board_column_id)
        .order_by(Task.board_position.desc())
        .limit(1)
    )
    max_pos = max_pos_result.scalar_one_or_none() or 0.0

    title = body.get("title") or message.content[:80]
    if len(message.content) > 80 and not body.get("title"):
        title += "..."

    raw_description = body.get("description", message.content)
    html_description = md_lib.markdown(
        raw_description,
        extensions=["tables", "fenced_code", "nl2br", "sane_lists"],
    )

    task = Task(
        title=title,
        description=html_description,
        project_id=project_id,
        board_column_id=board_column_id,
        board_position=max_pos + 1.0,
        assignee=body.get("assignee", "me"),
        due_date=body.get("due_date"),
    )
    db.add(task)
    await db.flush()

    return {"task_id": str(task.id), "title": task.title, "project_id": str(project_id)}


MAX_AGENT_TIMEOUT = 600


# Der Sandbox-Server stellt diesen Text jedem gescheiterten Lauf voran
# (src/mcp-sandbox/server.py). Ein vereinbarter Marker, keine Fehlerdeutung.
_CODE_FEHLER_MARKER = "Ausführung fehlgeschlagen"


def denkmodus_hinweis(denkmodus: str, codefehler: int) -> str:
    """Hinweis, wenn ein Lauf ohne Denkmodus an der Code-Ausführung scheiterte.

    Der Anlass ist messbar: Am 02.09.2026 schrieb das lokale Modell ohne Denkmodus
    achtzehnmal hintereinander Python, das nicht einmal geparst werden konnte --
    meist Syntaxfehler. Derselbe Auftrag mit Denkmodus «kurz» brauchte drei
    Werkzeugaufrufe und lieferte ein nachprüfbar richtiges Ergebnis.

    Ausgelöst wird am beobachteten Fehlschlag, nicht an der Frage: Ob es sich um
    eine Auswertung handelt, aus dem Wortlaut zu erschliessen, wäre geraten -- ein
    gescheiterter Sandbox-Lauf ist eine Tatsache.
    """
    if denkmodus != "aus" or codefehler < 1:
        return ""
    return (
        "\n\n---\n\n"
        f"*Hinweis: Der Denkmodus ist ausgeschaltet, und {codefehler} Code-Ausführung"
        f"{'en' if codefehler != 1 else ''} "
        f"{'sind' if codefehler != 1 else 'ist'} dabei gescheitert. Für Auswertungen im "
        "Datenraum ist der Denkmodus «kurz» erfahrungsgemäss nicht Komfort, sondern "
        "Voraussetzung — ohne ihn schreibt das lokale Modell häufig Code, der nicht "
        "läuft, und die Zahlen darüber sind entsprechend unsicher.*"
    )


def zeitlimit_grund(sekunden: int, codefehler: int) -> str:
    """Meldung beim Zeitlimit -- mit dem Grund, wenn er bekannt ist.

    Am 02.09.2026 lief ein Auftrag in die 600 Sekunden, weil sich das Modell an
    einem verschriebenen Spaltennamen festgebissen hatte: achtzehn Sandbox-Läufe,
    fünfzehn davon fehlgeschlagen. Die Meldung «Zeitlimit überschritten» legte
    nahe, die Aufgabe sei zu gross gewesen -- sie war es nicht, die Antwort lag
    nach dem zweiten Aufruf vor. Wer die Zahl der Fehlläufe sieht, weiss sofort,
    dass Wiederholen nichts bringt und die Frage enger gestellt gehört.
    """
    meldung = f"InnoPilot hat das Zeitlimit überschritten ({sekunden}s)"
    if codefehler >= 3:
        meldung += (
            f" -- {codefehler} Code-Ausführungen sind dabei gescheitert. Der Agent hat "
            "sich festgefahren, statt an der Aufgabengrösse zu scheitern. Die Frage "
            "enger stellen oder in zwei Schritte teilen."
        )
    return meldung


class Ablaufspeicher:
    """Sammelt den Ablauf eines Agentenlaufs mit getrennten Budgets.

    Ein gemeinsamer Deckel bevorzugt, was zuerst eintrifft, und das sind die
    Denkschritte. Beim Lauf vom 02.09.2026 füllten 140 davon den Deckel von 200,
    bevor der erste Werkzeugaufruf kam; der ``execute_code``, dessen Abfrage die
    Zahlen der Antwort erzeugt hatte, fehlte danach im Protokoll. Werkzeuge sind
    selten und tragen die Beweislast -- abgeschnitten wird nur das Denken.
    """

    def __init__(self, max_denken: int = 150, max_werkzeug: int = 300):
        self.ereignisse: list[dict] = []
        self._max_denken = max_denken
        self._max_werkzeug = max_werkzeug
        self._denken = 0
        self._werkzeug = 0

    def anhaengen(self, ereignis: dict) -> None:
        if ereignis.get("type") == "thinking":
            if self._denken >= self._max_denken:
                return
            self._denken += 1
        else:
            if self._werkzeug >= self._max_werkzeug:
                return
            self._werkzeug += 1
        self.ereignisse.append(ereignis)

# ── Agent-Event-Buffer (Background-Decoupling) ──────────────────
# Jeder laufende/kürzlich beendete Agent-Run hat eine Event-Liste.
# Neue Subscriber bekommen alle Events ab einem Offset.

_agent_events: dict[str, list[dict]] = {}
_agent_conditions: dict[str, asyncio.Condition] = {}
_agent_running: dict[str, bool] = {}
_AGENT_EVENT_TTL = 600  # Events 10min nach Abschluss aufbewahren

# Job-IDs, fuer die der Nutzer einen Stopp angefordert hat. Der Agent-Thread
# prueft dies kooperativ in seinen Callbacks (naechste Tool-/Text-Grenze) und
# bricht dann ab -- ein echter Stopp, nicht nur ein abgeklemmter Client-Stream.
_agent_cancel: set[str] = set()

# Aktiver Job pro Konversation (conv_id -> job_id). Erzwingt "ein Lauf pro
# Konversation": ein neuer Lauf stoppt zuerst den alten (verhindert Hermes-
# Session-Kollisionen bei session_id=f"chat-{conv_id}" und doppelte Laeufe).
_conv_active_job: dict[str, str] = {}

# Sanfte Parallelitaets-Begrenzung: max. 3 gleichzeitige Hermes-Laeufe. Mehr
# warten sauber in der Queue, statt ThreadPool/Ollama unkontrolliert zu fluten.
_MAX_CONCURRENT_AGENTS = 3
_agent_semaphore = asyncio.Semaphore(_MAX_CONCURRENT_AGENTS)

# clarify-Timeout: ohne Antwort trifft der Agent nach 180s eine Annahme und
# laeuft weiter -- der Thread blockiert nicht mehr minutenlang, wenn der Nutzer
# weggeklickt hat.
CLARIFY_TIMEOUT = 180


class _AgentCancelled(Exception):
    """Wird in den Hermes-Callbacks ausgelöst, wenn der Nutzer gestoppt hat."""

# Offene clarify-Rückfragen (HITL): clarify_id -> {event, answer, job_id}.
# Der Agent-Thread blockiert auf ``event`` bis der Nutzer via Endpoint antwortet.
_clarify_pending: dict[str, dict] = {}


async def _push_agent_event(job_id: str, event: dict):
    """Event in den Buffer schreiben und wartende Subscriber benachrichtigen."""
    if job_id not in _agent_events:
        _agent_events[job_id] = []
    _agent_events[job_id].append(event)
    cond = _agent_conditions.get(job_id)
    if cond:
        async with cond:
            cond.notify_all()


async def _cleanup_agent_events(job_id: str):
    """Events nach TTL aufräumen."""
    await asyncio.sleep(_AGENT_EVENT_TTL)
    _agent_events.pop(job_id, None)
    _agent_conditions.pop(job_id, None)
    _agent_running.pop(job_id, None)


def _load_agent_skills() -> str:
    """Kompakter Skill-Index (Name + Beschreibung) statt Volltext-Injektion.

    Hermes-nativ lädt der Agent den vollständigen Skill bei Bedarf selbst via
    ``skill_view`` (Progressive Disclosure). Hier liefern wir nur das Inhalts-
    verzeichnis, damit der Prompt schlank bleibt und der Agent weiss, welche
    Skills es gibt.
    """
    from app.services.hermes_config import discover_skills

    skills = discover_skills()
    if not skills:
        return "(Keine Skills hinterlegt.)"

    lines = []
    for s in skills:
        tools = f" [Tools: {', '.join(s['requires_toolsets'])}]" if s["requires_toolsets"] else ""
        lines.append(f"- **{s['name']}**: {s['description']}{tools}")
    lines.append("")
    lines.append("Lade den vollständigen Skill bei Bedarf mit skill_view(name='<name>'), bevor du eine Fachaufgabe ausführst.")
    return "\n".join(lines)


MCP_SERVER_DESCRIPTIONS: dict[str, dict[str, str]] = {
    "graph": {
        "label": "Microsoft 365",
        "description": "E-Mail, Kalender, Teams-Nachrichten, OneDrive, Dateisuche, Planner",
        "tools": (
            "search_drive(query) — Dateien auf OneDrive suchen; "
            "download_file(item_id) — Datei herunterladen und Text extrahieren (PDF); "
            "list_drive_items(path) — Ordnerinhalt auflisten; "
            "get_email(message_id) — E-Mail lesen; "
            "search_emails(query) — E-Mails durchsuchen; "
            "list_events(start, end) — Kalendereinträge; "
            "list_chats() — Teams-Chats; "
            "list_planner_tasks() — Planner-Aufgaben"
        ),
    },
    "taskpilot": {
        "label": "Aufgaben",
        "description": "Tasks erstellen, aktualisieren, zuweisen, Projekte verwalten",
        "tools": (
            "list_tasks(project_id, status) — Aufgaben auflisten; "
            "create_task(title, project_id) — Aufgabe erstellen; "
            "update_task(task_id, status) — Aufgabe ändern"
        ),
    },
    "pipedrive": {
        "label": "CRM (Pipedrive)",
        "description": "Deals, Kontakte, Aktivitäten, Notizen",
        "tools": (
            "list_deals() — Deals auflisten; "
            "get_deal(id) — Deal-Details; "
            "list_persons() — Kontakte; "
            "list_activities() — Aktivitäten"
        ),
    },
    "toggl": {
        "label": "Zeiterfassung (Toggl)",
        "description": "Zeiteinträge verwalten",
        "tools": (
            "list_time_entries(start, end) — Zeiteinträge auflisten; "
            "list_projects() — Projekte; "
            "get_project_summary(project_id) — Zusammenfassung"
        ),
    },
    "bexio": {
        "label": "Buchhaltung (Bexio)",
        "description": "Einzelabfragen zu Rechnungen, Journal, Kontenplan, Bankkonten",
        "tools": (
            "Für Auswertungen (Umsatz, Summen, Ranglisten, Verläufe) NICHT diese Tools "
            "nehmen, sondern den Datenraum — hier gibt es nur Einzelfälle. "
            "search_contact(name | email) — Kontakt suchen, mindestens eines von beiden; "
            "get_contact(contact_id) — Kontaktdetails; "
            "list_invoices(limit, offset) — Rechnungen einer Seite, ohne Kundenfilter; "
            "search_invoices(contact_id, status, from_date, to_date) — Rechnungen filtern; "
            "get_invoice(invoice_id) — Rechnungsdetails; "
            "get_journal(from_date, to_date) — Buchungsjournal; "
            "list_accounts() — Kontenplan; "
            "list_bank_accounts() — Bankkonten; "
            "get_business_years() — Geschäftsjahre"
        ),
    },
    "datenraum": {
        "label": "Datenraum (lokale Tabellen)",
        "description": "Die ganze Buchhaltung, dazu Belege, Zeiten und CRM als lokale Tabellen — die Quelle für alle Auswertungen",
        "tools": (
            "ERSTE ANLAUFSTELLE für jede Frage nach Zahlen: Umsatz, Ausgaben, offene "
            "Posten, Kunden, Lieferanten, Stunden, Projekte, Deals. Die Fachsysteme "
            "werden im Takt lokal gespiegelt; ein Werkzeugaufruf ersetzt Dutzende "
            "Einzelabfragen und die Zahlen sind vollständig statt auf die erste Seite "
            "gekürzt. "
            "datenraum_katalog() — welche Tabellen es gibt, mit Spalten, Bedeutung, "
            "Stand und fertigen Abfragen; "
            "datenraum_auffrischen(quelle) — neu laden, nur bei taggenauem Bedarf. "
            "Ausgewertet wird danach mit execute_code über /daten/<tabelle>.parquet. "
            "Für AUSGABEN und Kosten ist bexio_journal zuständig (WHERE ist_aufwand, "
            "gruppiert nach soll_konto), nicht bexio_kreditoren — letztere enthält nur "
            "die als Lieferantenrechnung erfassten rund 20 Prozent. "
            "Für eine BESTIMMTE Kundschaft immer über kundenschluessel verbinden statt "
            "über den Namen zu filtern — Kürzel wie AGG oder MBA kommen in der "
            "Buchhaltung nicht vor und ergeben lautlos 0 CHF."
        ),
    },
    "sandbox": {
        "label": "Code-Sandbox",
        "description": "Python in einem isolierten Container — rechnen, auswerten, visualisieren",
        "tools": (
            "execute_code(code) — Python ausführen; duckdb, pandas, matplotlib verfügbar. "
            "Der Datenraum liegt unter /daten/ bereit. Dateien nach /workspace/ "
            "erscheinen automatisch im Chat (Diagramme, Tabellen, HTML); "
            "list_packages() — verfügbare Pakete"
        ),
    },
    "invoiceinsight": {
        "label": "Kreditoren-Analyse",
        "description": "KPIs, Zahlungen, Anomalien, Cashflow-Prognose",
        "tools": (
            "Für Summen und Auswertungen NICHT hierher, sondern in den Datenraum: die "
            "Belege liegen vollständig unter /daten/invoiceinsight_rechnungen.parquet, "
            "der verbuchte Aufwand unter /daten/bexio_journal.parquet, die offenen "
            "Lieferantenrechnungen unter /daten/bexio_kreditoren.parquet. Die Werkzeuge "
            "hier liefern fertige Kennzahlen und Einzelfälle, keine Bestände. "
            "get_kpis() — Kennzahlen; "
            "get_upcoming_payments() — Anstehende Zahlungen; "
            "get_cost_distribution() — Kostenverteilung; "
            "get_cashflow_forecast() — Cashflow-Prognose; "
            "get_invoice_details(id) — Rechnungsdetails mit PDF-Pfad"
        ),
    },
    "signa": {
        "label": "Recherche (SIGNA)",
        "description": "ISI-Datenbank, wissenschaftliche Quellen",
        "tools": (
            "semantic_search_signals(query) — Signale semantisch nach Thema/Bedeutung suchen; "
            "search_signals(query) — Signale nach Stichwort durchsuchen; "
            "get_briefing(id) — Briefing lesen"
        ),
    },
}


def _get_configured_mcp_servers() -> dict:
    """Liest die MCP-Server-Liste aus der Hermes-Config (~/.hermes/config.yaml)."""
    import yaml
    from app.services.hermes_config import get_hermes_home

    config_path = get_hermes_home() / "config.yaml"
    try:
        with open(config_path, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        return config.get("mcp_servers", {})
    except Exception:
        return {}


async def _build_task_briefing(task_id) -> str:
    """Task-Briefing für Konversationen mit verknüpfter Task (``task_id``).

    Nutzt den vollständigen Auftragskontext des Workers (Titel, Beschreibung,
    Checkliste, Anhänge, Tags, Referenzen), damit «Mit Agent besprechen» direkt
    mit dem ganzen Task-Wissen startet. Best-effort: leerer String bei Fehlern.
    """
    if not task_id:
        return ""
    try:
        from sqlalchemy.orm import selectinload as _selectinload

        from app.services.hermes_worker import _format_task_context

        async with async_session() as db:
            task = (
                await db.execute(
                    select(Task)
                    .options(
                        _selectinload(Task.checklist_items),
                        _selectinload(Task.attachments),
                        _selectinload(Task.tags),
                    )
                    .where(Task.id == task_id)
                )
            ).scalar_one_or_none()
        if not task:
            return ""
        return (
            "\n## Verknüpfte Aufgabe (Kontext dieser Konversation)\n\n"
            "Diese Konversation ist mit folgender InnoSmith OS-Aufgabe verknüpft. "
            "Beziehe dich bei deinen Antworten darauf:\n\n"
            f"{_format_task_context(task)}\n"
        )
    except Exception:  # noqa: BLE001 - best-effort, darf den Chat nie blockieren
        logger.warning("Task-Briefing für Konversation konnte nicht geladen werden")
        return ""


async def _build_agent_prompt(
    user_content: str,
    task_id=None,
) -> str:
    """Baut einen schlanken Prompt — Tool-Definitionen kommen nativ vom Hermes-Agent via MCP.

    Der Konversationsverlauf und angepinnte Dokumente werden NICHT mehr in den
    Prompt-String dupliziert: sie laufen als echtes Message-Array über
    ``run_conversation(conversation_history=...)`` (siehe ``send_agent_message``).
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    skills_text = _load_agent_skills()

    # Lern-Schicht (Paritaet mit der E-Mail-Triage): freigegebene Leitregeln im
    # Chat-Kontext + gelernte Lektionen aus aehnlichen frueheren Faellen.
    from app.services.hermes_worker import _build_recall_block, _build_rules_block

    rules_block = await _build_rules_block("chat")
    recall_block = await _build_recall_block(
        {}, job_type=None, query=user_content[:400],
    )
    task_briefing = await _build_task_briefing(task_id)

    now_zurich = datetime.now(ZoneInfo("Europe/Zurich"))
    date_context = now_zurich.strftime("%A, %d. %B %Y, %H:%M Uhr")
    # Explizite Datums-Anker, damit relative Angaben ("nächste Woche") eindeutig sind.
    from datetime import timedelta as _td
    _days_to_mon = (7 - now_zurich.weekday()) % 7 or 7  # nächster Montag (heute zählt nicht)
    next_monday = (now_zurich + _td(days=_days_to_mon)).date()
    this_monday = (now_zurich - _td(days=now_zurich.weekday())).date()

    # Jeden Wochentag mit effektivem Datum vorrechnen (dynamisch), damit das Modell
    # nicht selbst addieren muss und sich nicht verzählt.
    _wd = ["Mo", "Di", "Mi", "Do", "Fr"]

    def _week_line(monday):
        return ", ".join(
            f"{_wd[i]} {(monday + _td(days=i)).strftime('%d.%m.')}" for i in range(5)
        )

    date_anchors = (
        f"Heute ist {now_zurich.strftime('%A')}, {now_zurich.date().isoformat()}. "
        f"'Diese Woche' (Mo–Fr): {_week_line(this_monday)}. "
        f"'Nächste Woche' (Mo–Fr, beginnt am nächsten Montag): {_week_line(next_monday)}."
    )

    return f"""Du bist InnoPilot, der KI-Agent von Anthony Smith (InnoSmith GmbH, Schweiz).
Du hast direkten Zugriff auf Firmendaten über deine MCP-Tools (siehst du in deiner Tool-Liste).
Nutze deine Tools aktiv. Behaupte niemals, du hättest keinen Zugriff.

## Aktuell

- Datum/Uhrzeit: {date_context} (Europe/Zurich)
- {date_anchors}
- User: Anthony Smith (du sprichst direkt mit ihm)

## Regeln

- Bei Fragen zu Firmendaten: Sofort passende Tools aufrufen
- Dateien: search_files → download_file
- ZAHLEN AUS FACHSYSTEMEN (Umsatz, Ausgaben, offene Posten, Kunden, Stunden, Projekte, Deals): Immer über den Datenraum. Er spiegelt die Buchhaltung, die Belegauswertung, Toggl und Pipedrive lokal als Tabellen. Zuerst `datenraum_katalog()`, dann EIN `execute_code` mit duckdb über `/daten/<tabelle>.parquet`. Das ist vollständig und in einer Runde erledigt — Einzelabfragen der Fachsysteme liefern nur die erste Seite und brauchen ein Dutzend Runden.
  Den Stand aus dem Katalog in der Antwort nennen. Nie ganze Tabellen ausgeben — nur das Ergebnis.
- EINE BESTIMMTE KUNDSCHAFT (Kürzel wie AGG, MBA, BFH, GSW): NIE mit ILIKE auf den Kundennamen filtern. Die drei Systeme benennen dieselbe Kundschaft verschieden — „AGG" heisst in der Buchhaltung „Bau- und Verkehrsdirektion des Kantons Bern (BVD) Amt für Grundstücke und Gebäude". Eine Namenssuche findet dort NULL Zeilen und meldet keinen Fehler, sondern 0 CHF. Immer über `/daten/kundenschluessel.parquet` gehen:
  `duckdb.sql("SELECT k.name, count(*) AS rechnungen, round(sum(r.netto),2) AS netto FROM '/daten/bexio_rechnungen.parquet' r JOIN '/daten/kundenschluessel.parquet' k ON k.system='bexio' AND k.fremd_id=r.kunden_id WHERE r.ist_umsatz AND k.schluessel='agg' GROUP BY 1")`
  Den Schlüssel zuerst nachschlagen (`SELECT DISTINCT schluessel, name, fremd_name FROM '/daten/kundenschluessel.parquet' WHERE fremd_name ILIKE '%AGG%' OR name ILIKE '%AGG%'`). Findet er die Kundschaft nicht, ist genau das zu sagen — nicht auf einen Namensvergleich ausweichen. Der Katalog erklärt es unter `umsatz_lesart`.
- UMSATZ ist, was fakturiert wurde: `bexio_rechnungen` mit `WHERE ist_umsatz`. Ein gewonnener Pipedrive-Deal ist eine Absicht, erfasste Toggl-Zeit ist Aufwand. Für AGG stehen 181'000 (CRM), 227'789 (Buchhaltung) und 59'160 (Zeit) nebeneinander — drei richtige Zahlen auf drei Fragen. Nie über zwei davon summieren.
- AUSGABEN und KOSTEN: `/daten/bexio_journal.parquet` mit `WHERE ist_aufwand`, gruppiert nach `soll_konto`. Das ist das vollständige Bild. `bexio_kreditoren` enthält nur die als Lieferantenrechnung erfassten rund 20 Prozent — mit Karte bezahlte Abos fehlen dort und stehen im Journal gegen Konto 2120. Der Katalog erklärt das unter `ausgaben_lesart`.
- JAHRESVERGLEICHE: vorher `/daten/bexio_geschaeftsjahre.parquet` prüfen. Ein laufendes Jahr (`ist_abgeschlossen = false`) ist ein Teiljahr; es gegen ein volles Jahr zu stellen zeigt einen Einbruch, wo bloss Monate fehlen. Das gehört in die Antwort.
- Buchhaltung im Einzelfall (eine bestimmte Rechnung, ein Konto, das Journal): get_invoice, get_journal, list_accounts, search_invoices
- Mehrstufige Aufgaben: Schritt für Schritt, Tool-Ergebnisse auswerten
- Öffentliche/aktuelle Recherche im Internet (News, Studien, Markt, Personen, Firmen): Nutze IMMER `web_search` (und `web_extract` für Detailseiten). Das ist die agentische Web-Recherche.
- WICHTIG — SIGNA ist NICHT das Internet: `semantic_search_signals`/`search_signals` durchsuchen NUR die interne strategische Signal-Datenbank (ISI). Nutze SIGNA ausschliesslich, wenn explizit nach SIGNA-Signalen/Briefings gefragt wird — NICHT für allgemeine Web-Recherche. Bei „recherchiere aktuelle Entwicklungen im Internet" → `web_search`, nicht SIGNA.
- CRM-Suche (Pipedrive): `search_crm` mit EINEM kurzen Begriff (Name, Firma, E-Mail) aufrufen, nicht mit ganzen Themensätzen. item_types im Singular (deal,person,organization).
- Angepinnte Dokumente: Dokumente, die Anthony in dieser Konversation angehängt hat, stehen vollständig im Verlauf (als «angepinnt» markiert). Nutze ihren Inhalt direkt — lade sie NICHT erneut mit download_file.
- Frühere Gespräche: Wenn Anthony sich auf etwas Früheres bezieht ("wie letzte Woche besprochen"), durchsuche den Verlauf mit session_search, bevor du nachfragst.
- Dauerhaftes Wissen: Lernst du eine stabile Präferenz oder Tatsache über Anthony/Arbeitsweise, halte sie knapp mit dem memory-Tool fest.
- Rückfragen bei Mehrdeutigkeit: Ist der Auftrag unklar oder gibt es mehrere sinnvolle Wege, stelle mit dem clarify-Tool eine kurze, strukturierte Rückfrage statt zu raten.
- Grosse Recherche-/Dokument-Aufträge: Zerlege sie bei Bedarf mit delegate_task in Subaufgaben. Externe Ausgaben (E-Mails, Dokumente an Kunden) bleiben IMMER HITL — du lieferst einen Entwurf zur Freigabe, versendest nichts eigenständig.
- Neue Skills: Erstelle Skills mit skill_manage nur als Vorschlag (propose-only). Beschreibe Anthony kurz den Nutzen und überlasse ihm die Freigabe/Aktivierung, statt eigenmächtig viele Skills anzulegen.
- Sprache: Deutsch (Schweizer Hochdeutsch, ss statt ß, korrekte Umlaute ä/ö/ü)
- Zeitzone: IMMER Europe/Zurich — alle Kalenderzeiten sind in dieser Zeitzone
- Kalender: Du verwaltest Anthonys Outlook-Kalender direkt. Bei Terminwünschen IMMER zuerst mit list_calendar_events oder find_free_slots prüfen ob der Slot frei ist, dann mit create_calendar_event buchen. Verweise NICHT auf externe Buchungstools — du bist das Buchungstool.

## Verfügbare Skills (bei Bedarf mit skill_view laden)

{skills_text}
{rules_block}{recall_block}{task_briefing}
## Anfrage

{user_content}"""


@router.get("/agent-tools")
async def get_agent_tools(user: User = Depends(require_role("owner"))):
    """Gibt die konfigurierten MCP-Server mit Beschreibungen zurück."""
    servers = _get_configured_mcp_servers()
    result = []
    for key in servers:
        meta = MCP_SERVER_DESCRIPTIONS.get(key, {})
        result.append({
            "key": key,
            "label": meta.get("label", key.capitalize()),
            "description": meta.get("description", ""),
        })
    return {"servers": result}


class _TorGeschlossen(HTTPException):
    """Der Text darf so nicht hinaus -- und es gibt nichts zu entscheiden."""

    def __init__(self, grund: str):
        super().__init__(status_code=503, detail=grund)


class _TorBrauchtFreigabe(HTTPException):
    """Restbestände gefunden. Der Mensch sitzt davor, also entscheidet er.

    409 statt 400: Der Zustand der Anfrage ist nicht falsch, er ist noch nicht
    freigegeben. Dieselbe Anfrage mit ``anon_ack: true`` geht durch.
    """

    def __init__(self, restbestaende: list[str], vorschau: str, modell: str):
        super().__init__(
            status_code=409,
            detail={
                "code": "anon_review",
                "model": modell,
                "residuals": restbestaende,
                "preview": vorschau[:4000],
                "message": (
                    "Im maskierten Text stehen noch Bruchstücke echter Werte. "
                    "Bitte prüfen, bevor der Text das Haus verlässt."
                ),
            },
        )


async def _protokolliere_ausgang(
    *, user_id: uuid.UUID, modell: str, restbestaende: list[str], freigegeben: bool
) -> None:
    """Hält fest, **dass** Text hinausging -- nie, welcher.

    Der Umstand ist die prüfbare Tatsache: welches Modell, maskiert, wie viele
    Bruchstücke gemeldet, ob ein Mensch sie freigegeben hat. Der Text selbst
    gehört nicht in ein Protokoll, dessen ganzer Zweck es ist, aufbewahrt zu
    werden.
    """
    try:
        from app.models.models import AuditLog

        async with async_session() as adb:
            adb.add(AuditLog(
                user_id=user_id,
                action="chat.cloud_ausgang",
                resource="llm_conversation",
                details={
                    "model": modell,
                    "anonymized": True,
                    "residual_count": len(restbestaende),
                    "human_approved": freigegeben,
                },
            ))
            await adb.commit()
    except Exception:  # noqa: BLE001 - ein Protokollfehler darf nichts anhalten
        logger.warning("Audit-Eintrag für den Cloud-Ausgang fehlgeschlagen")


async def _tor_nach_draussen(
    *,
    modell: str,
    prompt: str,
    verlauf: list[dict],
    freigegeben: bool,
) -> tuple[str, list[dict], str | None, list[str]]:
    """Das Tor vor der Cloud. Liefert ``(prompt, verlauf, sitzung, restbestaende)``.

    Bei einem lokalen Modell reicht es alles unverändert durch -- das Modell
    rechnet auf derselben Maschine, die Maskierung wäre Aufwand ohne Schutz.

    Bei einem auswärtigen Modell geht **alles** durch die Maskierung: Frage,
    Verlauf und angeheftete Dokumente. Sie stehen im Verlauf, und wer nur die
    Frage maskiert, schickt das Dossier trotzdem hinaus.

    ``freigegeben`` ist die Antwort des Menschen auf eine vorangegangene
    Rückfrage. Die zweite Maskierung erzeugt eine neue Sitzung statt die alte
    fortzuschreiben -- die Erkennung ist deterministisch, der Befund also
    derselbe, und ein Freigabe-Token mit eigener Lebensdauer wäre eine zweite
    Ablaufmechanik neben der des Mapping-Stores.
    """
    from app.services import schleuse

    if schleuse.ist_lokal(modell):
        return prompt, verlauf, None, []

    teile = [prompt, *[str(m.get("content") or "") for m in verlauf]]
    durchlass = await schleuse.pruefe_ausgang(
        text=_TRENNER.join(teile), modell=modell, bei_restbestaenden="melden"
    )

    if durchlass.lokal:
        # Kein Weg nach draussen. Zeigen kann man nur, was es gibt -- hier gibt
        # es keinen maskierten Text, also auch nichts zu entscheiden.
        raise _TorGeschlossen(
            durchlass.grund or "Die Anonymisierung ist nicht verfügbar."
        )

    maskierte_teile = durchlass.text.split(_TRENNER)
    if len(maskierte_teile) != len(teile):
        # Die Maskierung hat den Trenner verschluckt. Welcher Abschnitt jetzt
        # welcher ist, wäre geraten -- und ein falsch zugeordneter Verlauf ist
        # schlimmer als eine abgelehnte Anfrage.
        logger.error(
            "Maskierung hat die Gesprächsstruktur zerlegt (%d statt %d Teile)",
            len(maskierte_teile), len(teile),
        )
        raise _TorGeschlossen(
            "Die Maskierung hat die Gesprächsstruktur zerlegt -- der Text bleibt im Haus."
        )

    reste = list(durchlass.restbestaende)
    if reste and not freigegeben:
        raise _TorBrauchtFreigabe(reste, maskierte_teile[0], modell)

    neuer_verlauf = [
        {**m, "content": text}
        for m, text in zip(verlauf, maskierte_teile[1:], strict=True)
    ]
    return maskierte_teile[0], neuer_verlauf, durchlass.sitzung, reste


@router.post("/conversations/{conversation_id}/agent")
async def send_agent_message(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
    db: AsyncSession = Depends(get_db),
):
    """Agent-Nachricht absenden — startet Background-Task, gibt job_id zurück.

    Der Agent läuft unabhängig vom Client. Events werden über
    GET /conversations/{id}/agent-stream?job_id=... gestreamt und
    können nach Reconnect ab beliebigem Offset fortgesetzt werden.
    """
    from datetime import datetime, timezone as tz

    logger.info("[agent] Anfrage für Konversation %s", conversation_id)

    result = await db.execute(
        select(LlmConversation)
        .options(selectinload(LlmConversation.messages))
        .where(LlmConversation.id == conversation_id)
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Konversation nicht gefunden")

    user_content = body.get("content", "")
    selected_model = body.get("model", "hermes")
    context_sources = body.get("context_sources", [])
    logger.info("[agent] Nachricht (%.80s…), conv=%s", user_content, conversation_id)

    from app.services import denkstufen

    denkmodus = denkstufen.normalisiere(body.get("thinking_mode") or conv.thinking_mode)
    conv.thinking_mode = denkmodus

    # Grounding-Politik: Lokale Modelle = voller Zugriff. Cloud-Modelle =
    # Default-Deny; nur explizit freigegebene MCP-Server + optional Memory.
    from app.services.hermes_worker import (
        CLOUD_TOOL_LIMIT,
        _is_local_model,
        count_tools,
        resolve_cloud_toolsets,
    )

    requested_servers = body.get("enabled_servers") or []
    include_memory = bool(body.get("include_memory", False))

    if _is_local_model(selected_model):
        grounding = {"enabled_servers": list(requested_servers), "include_memory": include_memory}
    else:
        valid_servers = resolve_cloud_toolsets(requested_servers)
        tool_count = count_tools(valid_servers) if valid_servers else 0
        if tool_count > CLOUD_TOOL_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Zu viele Tools für ein Cloud-Modell ({tool_count} > {CLOUD_TOOL_LIMIT}). "
                    "Bitte weniger MCP-Server aktivieren."
                ),
            )
        grounding = {"enabled_servers": valid_servers, "include_memory": include_memory}

    conv.grounding = grounding
    # Gewaehltes Modell pro Konversation merken (statt Platzhalter 'hermes'),
    # damit die UI es beim Wiederoeffnen/Reload korrekt wiederherstellen kann.
    conv.model = selected_model

    # Dokumente an die Konversation pinnen (einmalige Extraktion) und den
    # gesamten angepinnten Korpus laden — identische Kontext-Pipeline wie im
    # Plain-Chat: Dokumente bleiben über die ganze Konversation sichtbar.
    from app.services.conversation_context import (
        build_conversation_history,
        build_pinned_context_block,
        load_pinned_items,
        persist_context_sources,
    )

    await persist_context_sources(db, conv.id, context_sources)
    pinned_items = await load_pinned_items(db, conv.id)
    pinned_block = build_pinned_context_block(pinned_items)

    # Der Verlauf enthält die neue Frage noch nicht — sie steht als '## Anfrage'
    # im Prompt. Er geht als echtes Message-Array an die Hermes-Runtime
    # (conversation_history) statt als gekürzter Textblock.
    sorted_messages = sorted(conv.messages, key=lambda m: m.created_at)
    conversation_history = build_conversation_history(sorted_messages)
    if pinned_block:
        conversation_history = [{
            "role": "user",
            "content": (
                "Folgende Dokumente sind in dieser Konversation angepinnt und "
                "bleiben dauerhaft verfügbar. Nutze ihren Inhalt direkt:\n\n"
                + pinned_block
            ),
        }, *conversation_history]

    full_prompt = await _build_agent_prompt(user_content, task_id=conv.task_id)

    # Das Tor vor der Cloud — **vor** dem Speichern der Nachricht. Andernfalls
    # stünde bei einer Rückfrage oder Absage eine Frage ohne Antwort im Verlauf,
    # und der Nutzer müsste sie von Hand aufräumen, um es erneut zu versuchen.
    full_prompt, conversation_history, anon_sitzung, restbestaende = await _tor_nach_draussen(
        modell=selected_model,
        prompt=full_prompt,
        verlauf=conversation_history,
        freigegeben=bool(body.get("anon_ack")),
    )
    if anon_sitzung:
        await _protokolliere_ausgang(
            user_id=user.id,
            modell=selected_model,
            restbestaende=restbestaende,
            freigegeben=bool(body.get("anon_ack")),
        )

    user_msg = LlmMessage(
        conversation_id=conv.id,
        role="user",
        content=user_content,
        attachments=body.get("attachments", []),
    )
    db.add(user_msg)
    await db.flush()

    # Chat-Lernkanal (Saeule 4): "merk dir ..."-Absicht erfassen -> Regel-Vorschlag (HITL).
    try:
        from app.services.learning import extract_teach_intent, record_chat_teach

        lesson = extract_teach_intent(user_content)
        if lesson:
            await record_chat_teach(db, content=lesson, conversation_id=str(conv.id))
    except Exception:  # noqa: BLE001 - best-effort, darf den Chat nie blockieren
        logger.warning("Chat-Teach-Erfassung fehlgeschlagen")

    if not conv.title and len(conv.messages) <= 1:
        conv.title = user_content[:80] + ("..." if len(user_content) > 80 else "")

    agent_job = AgentJob(
        user_id=user.id,
        job_type="chat_agent",
        status="running",
        llm_model=selected_model,
        metadata_json={
            "conversation_id": str(conv.id),
            "prompt_preview": user_content[:200],
            "anonymized": bool(anon_sitzung),
            "thinking_mode": denkmodus,
        },
        started_at=datetime.now(tz.utc),
    )
    db.add(agent_job)
    await db.flush()
    agent_job_id = agent_job.id

    await db.commit()
    conv_id_str = str(conv.id)
    job_id_str = str(agent_job_id)

    # Ein Lauf pro Konversation: laeuft in dieser Konversation bereits ein Job,
    # zuerst kooperativ stoppen (verhindert Hermes-Session-Kollisionen und
    # doppelte Laeufe, die den ThreadPool/Ollama blockieren).
    prev_job = _conv_active_job.get(conv_id_str)
    if prev_job and prev_job != job_id_str and _agent_running.get(prev_job):
        _agent_cancel.add(prev_job)
        for _cid, _p in list(_clarify_pending.items()):
            if _p.get("job_id") == prev_job:
                _p["answer"] = "Abgebrochen (neuer Lauf gestartet)."
                _p["event"].set()

    _agent_events[job_id_str] = []
    _agent_conditions[job_id_str] = asyncio.Condition()
    _agent_running[job_id_str] = True
    _conv_active_job[conv_id_str] = job_id_str

    asyncio.create_task(
        _run_agent_background(
            job_id_str,
            conv_id_str,
            full_prompt,
            selected_model,
            enabled_servers=grounding["enabled_servers"],
            include_memory=grounding["include_memory"],
            conversation_history=conversation_history,
            denkmodus=denkmodus,
            anon_sitzung=anon_sitzung,
        )
    )

    return {
        "job_id": job_id_str,
        "conversation_id": conv_id_str,
        "status": "running",
        "anonymized": bool(anon_sitzung),
        "residuals": restbestaende,
        # Die Sitzungskennung, nicht die Zuordnung selbst. Damit kann die
        # Oberflaeche ueber /api/content/mapping-keys/{id}/diff nachschlagen,
        # was ersetzt wurde, und es als Vermerk in den Verlauf stellen. Die
        # Klartextwerte reisen weiterhin nur auf ausdrueckliche Anfrage.
        "anon_session": anon_sitzung,
    }


def _suchanfrage(args) -> str:
    """Holt die Suchanfrage aus den Werkzeugargumenten. Leer, wenn nicht lesbar.

    Hermes reicht die Argumente mal als Woerterbuch, mal als JSON-Zeichenkette
    durch -- je nachdem, ob das Modell sie schon geparst geliefert hat. Beide
    Formen kommen im Betrieb vor, darum beide hier.

    Faellt nie durch: Ein Fehler beim Lesen der Anfrage darf weder den
    Suchverlauf noch den Agenten anhalten. Dann steht eben nur der
    Werkzeugname da.
    """
    try:
        if isinstance(args, dict):
            return str(args.get("query") or "")
        if isinstance(args, str) and args.strip().startswith("{"):
            return str((json.loads(args) or {}).get("query") or "")
    except Exception:  # noqa: BLE001 - Anzeige darf den Agenten nie stoeren
        return ""
    return ""


async def _run_agent_background(
    job_id: str,
    conv_id: str,
    prompt: str,
    model: str,
    *,
    enabled_servers: list[str] | None = None,
    include_memory: bool = False,
    conversation_history: list[dict] | None = None,
    denkmodus: str = "lang",
    anon_sitzung: str | None = None,
):
    """Wrapper um den eigentlichen Lauf: Parallelitaets-Begrenzung + robuster Cleanup.

    - ``asyncio.Semaphore`` begrenzt gleichzeitige Laeufe (Warte-Status-Event).
    - ``finally`` garantiert, dass ``_agent_running`` zurueckgesetzt, das Cancel-Set
      bereinigt, der Konversations-Slot freigegeben und das Event-Cleanup geplant
      wird -- auch bei ``CancelledError`` (Backend-Reload/-Shutdown). Damit haengt
      kein Client mehr in einem Endlos-Reconnect.
    """
    if _agent_semaphore.locked():
        await _push_agent_event(job_id, {"event": "status", "data": json.dumps(
            {"content": "Wartet auf freien Agent-Slot..."})})
    async with _agent_semaphore:
        # Nach dem Warten pruefen, ob der Job zwischenzeitlich gestoppt wurde.
        if job_id in _agent_cancel:
            _agent_running[job_id] = False
            _agent_cancel.discard(job_id)
            if _conv_active_job.get(conv_id) == job_id:
                _conv_active_job.pop(conv_id, None)
            await _push_agent_event(job_id, {"event": "stopped", "data": json.dumps({"content": "Vom Benutzer gestoppt"})})
            asyncio.create_task(_cleanup_agent_events(job_id))
            return
        try:
            await _run_agent_background_impl(
                job_id,
                conv_id,
                prompt,
                model,
                enabled_servers=enabled_servers,
                include_memory=include_memory,
                conversation_history=conversation_history,
                denkmodus=denkmodus,
                anon_sitzung=anon_sitzung,
            )
        except asyncio.CancelledError:
            logger.info("[agent-bg] Background-Task abgebrochen, job=%s", job_id)
            try:
                await _push_agent_event(job_id, {"event": "error", "data": json.dumps({"error": "Agent-Lauf abgebrochen"})})
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            _agent_running[job_id] = False
            _agent_cancel.discard(job_id)
            if _conv_active_job.get(conv_id) == job_id:
                _conv_active_job.pop(conv_id, None)
            asyncio.create_task(_cleanup_agent_events(job_id))


async def _run_agent_background_impl(
    job_id: str,
    conv_id: str,
    prompt: str,
    model: str,
    *,
    enabled_servers: list[str] | None = None,
    include_memory: bool = False,
    conversation_history: list[dict] | None = None,
    denkmodus: str = "lang",
    anon_sitzung: str | None = None,
):
    """Führt den Hermes-Agent (InnoPilot) als Background-Task aus.

    Hermes ist synchron: ``AIAgent.run_conversation`` läuft in einem Thread
    (``asyncio.to_thread``). Die synchronen Callbacks (Text/Reasoning/Tools)
    feuern aus dem Worker-Thread und werden via ``loop.call_soon_threadsafe``
    threadsicher in eine ``asyncio.Queue`` gebrückt. So bleibt die volle
    Transparenz erhalten: man sieht InnoPilot denken (``thinking``), Tools
    aufrufen (``tool_start``/``tool_event``) und streamen (``chunk``).
    """
    from datetime import datetime, timezone as tz
    from app.services.hermes_worker import build_chat_agent, ensure_runtime_ready

    t_start = time.time()
    loop = asyncio.get_running_loop()

    async def _update_agent_job(
        status: str,
        output: str | None = None,
        error_message: str | None = None,
        tools_used: list[str] | None = None,
        trace: list[dict] | None = None,
    ):
        async with async_session() as sdb:
            res = await sdb.execute(
                select(AgentJob).where(AgentJob.id == uuid.UUID(job_id))
            )
            job = res.scalar_one_or_none()
            if not job:
                return
            job.status = status
            if output is not None:
                job.output = output[:2000]
            if error_message is not None:
                job.error_message = error_message
            if status in ("completed", "failed"):
                job.completed_at = datetime.now(tz.utc)
            if tools_used or trace:
                meta = dict(job.metadata_json or {})
                if tools_used:
                    meta["tools_used"] = tools_used
                # Trace im selben Format wie der Worker, damit der bestehende
                # Trace-Endpoint (metadata_json['trace']) auch Chat-Jobs zeigt.
                if trace:
                    meta["trace"] = trace
                job.metadata_json = meta
            await sdb.commit()

    queue: asyncio.Queue = asyncio.Queue()

    def _emit(evt_type: str, payload):
        """Threadsicher ein Event in die Queue legen (aus dem Agent-Thread)."""
        loop.call_soon_threadsafe(queue.put_nowait, (evt_type, payload))

    def _check_cancel():
        """Kooperativer Abbruch: an Tool-/Text-Grenzen den Stopp-Wunsch prüfen."""
        if job_id in _agent_cancel:
            raise _AgentCancelled()

    # Synchrone Hermes-Callbacks -> Queue-Brücke
    def on_text(text: str):
        _check_cancel()
        if text:
            _emit("chunk", text)

    # Trace-Akkumulator: gleiches Format wie der Worker (thinking/tool_start/
    # tool_complete), damit der bestehende Trace-Endpoint Chat-Jobs anzeigt.
    _ablauf = Ablaufspeicher()
    _trace = _ablauf.ereignisse

    def _trace_append(event: dict):
        _ablauf.anhaengen(event)

    # Fehlgeschlagene Sandbox-Läufe zählen. Sie sind der einzige belastbare
    # Anhaltspunkt dafür, dass der abgeschaltete Denkmodus die Auswertung getragen
    # hat -- an der Frage selbst wäre das nur zu raten.
    _codefehler = [0]

    # Der Gedankengang als Ganzes -- der Trace hält ihn nur in Stücken zu je
    # 2000 Zeichen und deckelt bei 150 Ereignissen. Für die Anzeige nach einem
    # Neuladen braucht es den ungekürzten Text.
    _denken: list[str] = []
    _DENKEN_GRENZE = 200_000

    def on_reasoning(text: str):
        _check_cancel()
        if text:
            _trace_append({"type": "thinking", "text": str(text)[:2000]})
            if sum(len(t) for t in _denken) < _DENKEN_GRENZE:
                _denken.append(str(text))
            _emit("thinking", text)

    _tools_used: list[str] = []
    # Sandbox-Artefakte dieses Laufs: Scope + Dateinamen (Reihenfolge/dedupe),
    # damit die erzeugten Dateien inline gerendert werden koennen. Bei geforctem
    # workspace_key landet alles im persistenten conv-<id>-Scope.
    _artifact_scope: dict[str, str | None] = {"scope": None}
    _artifact_names: list[str] = []

    def on_tool_start(tc_id, name, args):
        _check_cancel()
        # Sandbox-Ausfuehrung an die Konversation binden: erzwingt den
        # persistenten conv-<conv_id>-Workspace (reload-sicher, ueber den
        # bestehenden Artefakt-Endpoint bedienbar) — unabhaengig davon, ob das
        # Modell selbst einen workspace_key setzt.
        if str(name) == _SANDBOX_EXEC_TOOL and isinstance(args, dict):
            args["workspace_key"] = conv_id
        if name and name not in _tools_used:
            _tools_used.append(str(name))
        event = {"type": "tool_start", "name": str(name)}
        # Bei Skill-Aufrufen den geladenen Skill-Namen miterfassen -- analog zum
        # Worker (_on_tool_start), damit die Skill-Nutzungs-Analytics auch
        # explizite skill_view-Loads aus dem Chat zaehlen kann.
        if str(name) in ("skill_view", "skill_manage"):
            skill = None
            try:
                if isinstance(args, dict):
                    skill = args.get("name") or args.get("skill")
                elif isinstance(args, str) and args.strip().startswith("{"):
                    skill = (json.loads(args) or {}).get("name")
            except Exception:  # noqa: BLE001 - Trace darf nie scheitern
                skill = None
            if skill:
                event["skill"] = str(skill)
        # Bei der Websuche die Anfrage mitzeigen -- im Moment des Geschehens,
        # nicht erst hinterher im Suchverlauf. «Ich habe im Netz gesucht» ist
        # keine Auskunft; «ich habe nach *X* gesucht» ist eine, die man pruefen
        # kann, waehrend sie noch zu aendern waere. Die Anfrage geht dabei
        # unveraendert hinaus wie sie hinausging -- gezeigt wird, was das Haus
        # verlassen hat, nicht was sich schoener laese.
        beschriftung = str(name)
        if str(name) == "web_search":
            frage = _suchanfrage(args)
            if frage:
                event["query"] = frage
                beschriftung = f"{name}: «{frage}»"
        # Bei Sandbox-Läufen den Code mitschreiben. Ohne ihn ist eine Zahl in der
        # Antwort nicht nachprüfbar: man sieht, dass gerechnet wurde, aber nicht
        # was. Genau daran scheiterte die Klärung der Pipedrive-Auswertung vom
        # 02.09.2026 -- die Summen stimmten, die Aufschlüsselung nicht, und die
        # Abfrage, die beides erzeugt hatte, war nirgends festgehalten.
        elif str(name) == _SANDBOX_EXEC_TOOL and isinstance(args, dict):
            code = args.get("code")
            if code:
                event["code"] = str(code)[:4000]
        _trace_append(event)
        _emit("tool_start", beschriftung)

    def _web_search_provider() -> str:
        """Tatsächliches Such-Backend der Hermes-nativen Websuche (z. B. ddgs).

        Für den Datenschutz-Audit-Trail soll nachvollziehbar sein, WOHIN die
        Query ging -- nicht nur, dass Hermes gesucht hat. Fallback: 'hermes'.
        """
        try:
            from tools.web_tools import _get_search_backend

            return _get_search_backend() or "hermes"
        except Exception:  # noqa: BLE001 - Audit-Log darf den Agenten nie stören
            return "hermes"

    async def _log_web_search(query: str, result_preview: str):
        """Hermes-native Websuche in ``web_searches`` historisieren (Audit-Parität).

        Ersetzt das Logging des abgelösten Tavily-Modus: jede agentische Suche
        bleibt damit im Suchverlauf nachvollziehbar. Best-effort.
        """
        try:
            from app.models.models import WebSearch

            async with async_session() as wdb:
                wdb.add(WebSearch(
                    query=query[:500],
                    provider=_web_search_provider(),
                    results=[{"content": result_preview[:2000]}] if result_preview else [],
                    result_count=1 if result_preview else 0,
                    triggered_by="agent",
                    conversation_id=uuid.UUID(conv_id),
                    credits_used=0,
                ))
                await wdb.commit()
        except Exception:  # noqa: BLE001 - Audit-Log darf den Agenten nie stören
            logger.warning("web_search-Audit-Log fehlgeschlagen")

    def on_tool_complete(tc_id, name, args, result):
        # Die Ausgabe eines Sandbox-Laufs ist die Quelle jeder Zahl in der Antwort
        # und braucht mehr Platz als 500 Zeichen: eine Rangliste über zehn Kunden
        # ist danach abgeschnitten, und ob die Antwort sie wiedergibt oder ergänzt
        # hat, lässt sich nicht mehr feststellen.
        grenze = 4000 if str(name) == _SANDBOX_EXEC_TOOL else 500
        _trace_append({"type": "tool_complete", "name": str(name), "result": str(result)[:grenze]})
        if str(name) == _SANDBOX_EXEC_TOOL and _CODE_FEHLER_MARKER in str(result or ""):
            _codefehler[0] += 1
        # Sandbox-Artefakte einsammeln: der Marker <!--tp-exec:scope:names-->
        # aus dem Tool-Ergebnis liefert Scope + Dateinamen fuer das Inline-Rendering.
        if str(name) == _SANDBOX_EXEC_TOOL:
            m = _EXEC_MARKER_RE.search(str(result or ""))
            if m:
                _artifact_scope["scope"] = m.group(1)
                for n in m.group(2).split("|"):
                    n = n.strip()
                    if n and n not in _artifact_names:
                        _artifact_names.append(n)
        # Audit-Parität: Hermes-native web_search-Aufrufe historisieren.
        # Exakter Abgleich -- ein Substring-Match hatte frueher auch
        # mcp_taskpilot_web_search erfasst und Duplikate erzeugt.
        if str(name) == "web_search":
            query = _suchanfrage(args)
            if query:
                asyncio.run_coroutine_threadsafe(
                    _log_web_search(query, str(result or "")), loop
                )
        _emit("tool_event", json.dumps(
            {"tool": str(name), "result": str(result)[:500]}, ensure_ascii=False
        ))
        _check_cancel()

    def clarify_callback(question: str, choices) -> str:
        """HITL-Rückfrage: blockiert den Agent-Thread bis der Nutzer antwortet.

        Läuft im Hermes-Worker-Thread. Wir emittieren ein clarify-SSE-Event und
        warten auf die Antwort (gesetzt über den /agent/clarify-Endpoint). Bei
        Timeout gibt der Callback einen neutralen Hinweis zurück, damit der Agent
        eigenständig eine sinnvolle Annahme treffen und fortfahren kann.
        """
        clarify_id = uuid.uuid4().hex
        ev = threading.Event()
        _clarify_pending[clarify_id] = {"event": ev, "answer": None, "job_id": job_id}
        try:
            choice_list = [str(c) for c in choices] if isinstance(choices, (list, tuple)) else []
            _emit("clarify", json.dumps({
                "clarify_id": clarify_id,
                "question": str(question),
                "choices": choice_list,
            }, ensure_ascii=False))
            answered = ev.wait(timeout=CLARIFY_TIMEOUT)
            if not answered:
                return "Keine Antwort des Nutzers erhalten. Triff eine sinnvolle Annahme und fahre fort."
            return _clarify_pending.get(clarify_id, {}).get("answer") or "(leere Antwort)"
        finally:
            _clarify_pending.pop(clarify_id, None)

    await _push_agent_event(job_id, {"event": "status", "data": json.dumps({"content": "InnoPilot wird initialisiert..."})})

    if not await ensure_runtime_ready():
        logger.error("[agent-bg] Hermes-Runtime nicht verfügbar")
        await _update_agent_job("failed", error_message="Hermes-Runtime nicht verfügbar")
        await _push_agent_event(job_id, {"event": "error", "data": json.dumps({"error": "InnoPilot nicht verfügbar — prüfe ~/.hermes/config.yaml"})})
        _agent_running[job_id] = False
        asyncio.create_task(_cleanup_agent_events(job_id))
        return

    try:
        agent = await asyncio.to_thread(
            build_chat_agent,
            model,
            enabled_servers=enabled_servers,
            include_memory=include_memory,
            on_text=on_text,
            on_reasoning=on_reasoning,
            on_tool_start=on_tool_start,
            on_tool_complete=on_tool_complete,
            clarify_callback=clarify_callback,
            session_id=f"chat-{conv_id}",
        )
    except Exception:
        logger.exception("[agent-bg] Agent-Init fehlgeschlagen")
        await _update_agent_job("failed", error_message="Agent-Initialisierung fehlgeschlagen")
        await _push_agent_event(job_id, {"event": "error", "data": json.dumps({"error": "InnoPilot konnte nicht initialisiert werden"})})
        _agent_running[job_id] = False
        asyncio.create_task(_cleanup_agent_events(job_id))
        return

    # Denkmodus: Hermes bekommt die anbieterrichtigen Parameter. Die Abbildung
    # steht in app/services/denkstufen.py, damit die Oberfläche drei Stufen
    # kennen darf und nichts über Ollama, Anthropic oder OpenAI wissen muss.
    try:
        from app.services import denkstufen

        overrides = denkstufen.request_overrides(denkmodus, model)
        if overrides:
            bestand = dict(getattr(agent, "request_overrides", None) or {})
            bestand.update(overrides)
            agent.request_overrides = bestand
    except Exception:  # noqa: BLE001 - ein Denkschalter darf keinen Lauf verhindern
        logger.warning("Denkmodus %s liess sich nicht setzen", denkmodus)

    active_model = getattr(agent, "model", "?")
    await _push_agent_event(job_id, {"event": "status", "data": json.dumps(
        {"content": f"InnoPilot bereit (Modell: {active_model}) — Aufgabe wird verarbeitet..."})})
    logger.info("[agent-bg] Run gestartet, conv=%s, model=%s, prompt_len=%d",
                conv_id, active_model, len(prompt))

    def _run_sync() -> str:
        result = agent.run_conversation(
            prompt,
            conversation_history=list(conversation_history or []),
        )
        if isinstance(result, dict):
            return str(result.get("final_response") or "")
        return str(result or "")

    bot_task = asyncio.create_task(asyncio.to_thread(_run_sync))
    # Bei vorzeitigem Return (Stopp/Timeout) eine evtl. Thread-Exception abgreifen,
    # damit kein "Task exception was never retrieved" geloggt wird.
    bot_task.add_done_callback(lambda t: None if t.cancelled() else t.exception())

    # Läuft der Text maskiert, kann er nicht Bruchstück für Bruchstück
    # zurückgebildet werden: Eine Deckadresse fällt leicht über die Grenze
    # zweier Chunks, und die Rückbildung fände sie dann nie. Gesendet wird
    # darum periodisch der **ganze** zurückgebildete Text (``chunk_replace``
    # statt anhängendem ``chunk``) -- so, wie es das GSW-Cockpit macht.
    _maskiert = bool(anon_sitzung)
    _roh = {"antwort": "", "denken": ""}
    _letzte_rueckbildung = {"antwort": 0.0, "denken": 0.0}
    _RUECKBILDUNG_TAKT = 0.8

    async def _zeige_zurueckgebildet(feld: str, ereignis: str, erzwinge: bool = False):
        jetzt = time.monotonic()
        if not erzwinge and jetzt - _letzte_rueckbildung[feld] < _RUECKBILDUNG_TAKT:
            return
        _letzte_rueckbildung[feld] = jetzt
        try:
            from app.services import anon_politik

            klar, _reste = await anon_politik.bilde_zurueck(_roh[feld], anon_sitzung or "")
        except Exception:  # noqa: BLE001 - Zwischenstand, der Endstand zählt
            return
        await _push_agent_event(job_id, {"event": ereignis, "data": json.dumps({"content": klar})})

    async def _drain(evt_type: str, evt_data):
        if evt_type == "chunk":
            if _maskiert:
                _roh["antwort"] += str(evt_data)
                await _zeige_zurueckgebildet("antwort", "chunk_replace")
                return
            await _push_agent_event(job_id, {"event": "chunk", "data": json.dumps({"content": evt_data})})
        elif evt_type == "thinking":
            if _maskiert:
                _roh["denken"] += str(evt_data)
                await _zeige_zurueckgebildet("denken", "thinking_replace")
                return
            await _push_agent_event(job_id, {"event": "thinking", "data": json.dumps({"content": evt_data})})
        elif evt_type == "tool_start":
            await _push_agent_event(job_id, {"event": "tool_start", "data": json.dumps({"tools": evt_data})})
        elif evt_type == "tool_event":
            await _push_agent_event(job_id, {"event": "tool_event", "data": evt_data})
        elif evt_type == "clarify":
            await _push_agent_event(job_id, {"event": "clarify", "data": evt_data})
        elif evt_type == "status":
            await _push_agent_event(job_id, {"event": "status", "data": json.dumps({"content": evt_data})})

    try:
        while not bot_task.done():
            # Stopp-Wunsch des Nutzers: Client sofort entkoppeln, Thread läuft
            # kooperativ aus (Callback-Abbruch an nächster Grenze).
            if job_id in _agent_cancel:
                logger.info("[agent-bg] Stopp durch Nutzer, job=%s", job_id)
                # Echter Cross-Thread-Abbruch: gibt Ollama/GPU + ThreadPool-Slot
                # frei, statt den Hermes-Thread unbemerkt weiterlaufen zu lassen.
                try:
                    agent.interrupt("Vom Benutzer abgebrochen.")
                except Exception:  # noqa: BLE001 - Abbruch darf nie scheitern
                    pass
                bot_task.cancel()
                await _update_agent_job("failed", error_message="Vom Benutzer gestoppt", tools_used=list(_tools_used), trace=list(_trace))
                await _push_agent_event(job_id, {"event": "stopped", "data": json.dumps({"content": "Vom Benutzer gestoppt"})})
                _agent_running[job_id] = False
                _agent_cancel.discard(job_id)
                # clarify ggf. entsperren, damit der Thread nicht hängen bleibt
                for cid, p in list(_clarify_pending.items()):
                    if p.get("job_id") == job_id:
                        p["answer"] = "Abgebrochen."
                        p["event"].set()
                asyncio.create_task(_cleanup_agent_events(job_id))
                return
            try:
                evt_type, evt_data = await asyncio.wait_for(queue.get(), timeout=2.0)
                await _drain(evt_type, evt_data)
            except asyncio.TimeoutError:
                elapsed = time.time() - t_start
                if elapsed > MAX_AGENT_TIMEOUT:
                    # Ollama/GPU + ThreadPool-Slot wirklich freigeben.
                    try:
                        agent.interrupt("Zeitlimit überschritten.")
                    except Exception:  # noqa: BLE001 - Abbruch darf nie scheitern
                        pass
                    bot_task.cancel()
                    logger.warning("[agent-bg] Timeout nach %.0fs, job=%s", elapsed, job_id)
                    # Woran die Zeit verging, gehört in die Meldung. «Zeitlimit
                    # überschritten» allein lässt offen, ob die Aufgabe zu gross war
                    # oder ob sich der Agent an einer Kleinigkeit festgebissen hat --
                    # und das sind zwei völlig verschiedene Konsequenzen.
                    grund = zeitlimit_grund(MAX_AGENT_TIMEOUT, _codefehler[0])
                    await _update_agent_job("failed", error_message=f"Timeout nach {MAX_AGENT_TIMEOUT}s", tools_used=list(_tools_used), trace=list(_trace))
                    await _push_agent_event(job_id, {"event": "error", "data": json.dumps({"error": grund})})
                    _agent_running[job_id] = False
                    asyncio.create_task(_cleanup_agent_events(job_id))
                    return

        while not queue.empty():
            evt_type, evt_data = queue.get_nowait()
            await _drain(evt_type, evt_data)

        content = bot_task.result()
        rueckstaende: list[str] = []
        if _maskiert:
            # Der Endstand, nicht der Zwischenstand: Hier zählt die
            # Rückstandsliste, weil ein stehengebliebener Ersatzname genauso
            # plausibel aussieht wie ein echter und niemand Anlass zu zweifeln hat.
            from app.services import anon_politik

            content, rueckstaende = await anon_politik.bilde_zurueck(
                content, anon_sitzung or ""
            )
            await _zeige_zurueckgebildet("denken", "thinking_replace", erzwinge=True)
        # Erzeugte Sandbox-Artefakte inline verfuegbar machen: Marker anhaengen,
        # damit das Frontend den ArtifactViewer (Bilder/HTML spielbar, Vollbild)
        # rendert — sowohl live (done-Event) als auch nach Reload (gespeicherter
        # content).
        # Der Hinweis steht vor dem Artefakt-Marker, damit er im Text landet und
        # nicht zwischen den Marker und dessen Auswertung im Frontend gerät.
        content = f"{content}{denkmodus_hinweis(denkmodus, _codefehler[0])}"
        artifact_marker = _artifacts_marker(_artifact_scope["scope"], _artifact_names)
        if artifact_marker:
            content = f"{content}{artifact_marker}"
        tools_used = list(_tools_used)
        elapsed = time.time() - t_start
        logger.info("[agent-bg] Fertig in %.1fs, Antwort=%d Zeichen, Tools=%s, job=%s",
                    elapsed, len(content), tools_used, job_id)

    except Exception:
        logger.exception("[agent-bg] Fehler in job=%s", job_id)
        await _update_agent_job("failed", error_message="Agent-Ausführung fehlgeschlagen", tools_used=list(_tools_used), trace=list(_trace))
        await _push_agent_event(job_id, {"event": "error", "data": json.dumps({"error": "InnoPilot-Ausführung fehlgeschlagen"})})
        _agent_running[job_id] = False
        asyncio.create_task(_cleanup_agent_events(job_id))
        return

    # Token-Tracking: Hermes akkumuliert die tatsächliche Usage des Laufs
    # (alle Iterationen inkl. Tool-Turns) auf dem frisch gebauten Agenten.
    total_tokens_used = int(getattr(agent, "session_total_tokens", 0) or 0)

    gedankengang = "".join(_denken).strip()
    if _maskiert and gedankengang:
        from app.services import anon_politik

        gedankengang, _ = await anon_politik.bilde_zurueck(gedankengang, anon_sitzung or "")

    async with async_session() as save_db:
        assistant_msg = LlmMessage(
            conversation_id=uuid.UUID(conv_id),
            role="assistant",
            content=content,
            model=model if model not in ("hermes", "nanobot", "") else None,
            tokens=total_tokens_used or None,
            thinking=gedankengang or None,
            residuals=rueckstaende,
        )
        save_db.add(assistant_msg)
        if total_tokens_used:
            conv_res = await save_db.execute(
                select(LlmConversation).where(LlmConversation.id == uuid.UUID(conv_id))
            )
            conv_update = conv_res.scalar_one_or_none()
            if conv_update:
                conv_update.total_tokens = (conv_update.total_tokens or 0) + total_tokens_used
        await save_db.commit()

    await _update_agent_job("completed", output=content, tools_used=tools_used, trace=list(_trace))

    await _push_agent_event(job_id, {"event": "done", "data": json.dumps({
        "message_id": str(assistant_msg.id),
        "tokens": total_tokens_used,
        "content": content,
        "thinking": gedankengang or None,
        "residuals": rueckstaende,
        "anonymized": _maskiert,
        "tools_used": tools_used,
        "elapsed_s": round(time.time() - t_start, 1),
    })})

    _agent_running[job_id] = False
    asyncio.create_task(_cleanup_agent_events(job_id))


@router.post("/conversations/{conversation_id}/agent/clarify")
async def answer_agent_clarify(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
):
    """Antwort auf eine clarify-Rückfrage des Agenten entgegennehmen.

    Setzt die Antwort und entsperrt den blockierten Agent-Thread (siehe
    ``clarify_callback`` in ``_run_agent_background``).
    """
    clarify_id = body.get("clarify_id")
    answer = body.get("answer", "")
    pending = _clarify_pending.get(clarify_id) if clarify_id else None
    if not pending:
        raise HTTPException(status_code=404, detail="Rückfrage nicht gefunden oder bereits abgelaufen")
    pending["answer"] = str(answer)
    pending["event"].set()
    return {"ok": True}


@router.post("/conversations/{conversation_id}/agent/cancel")
async def cancel_agent_run(
    conversation_id: uuid.UUID,
    body: dict,
    user: User = Depends(require_role("owner")),
):
    """Laufenden Chat-Agent-Job stoppen (echter, kooperativer Abbruch).

    Setzt den Stopp-Wunsch; der Background-Run entkoppelt den Client sofort und
    der Hermes-Thread bricht an der nächsten Tool-/Text-Grenze ab. Im Gegensatz
    zum reinen Schliessen des Streams läuft der Job danach nicht unbemerkt weiter.
    """
    job_id = body.get("job_id")
    if not job_id:
        raise HTTPException(status_code=400, detail="job_id fehlt")
    _agent_cancel.add(str(job_id))
    # Falls der Agent gerade auf eine clarify-Antwort wartet: entsperren.
    for cid, p in list(_clarify_pending.items()):
        if p.get("job_id") == str(job_id):
            p["answer"] = "Abgebrochen."
            p["event"].set()
    return {"ok": True}


async def _terminal_event_from_db(job_id: str) -> dict:
    """Baut aus dem finalen Job-Status ein terminales SSE-Event.

    Wird gesendet, wenn der Stream keinen terminalen Event mehr im Buffer hat,
    der Job aber nicht mehr laeuft (z. B. Reconnect nach Ablauf des Buffers oder
    Backend-Reload). So kann der Client immer sauber aufraeumen, statt in einen
    Endlos-Reconnect zu laufen.
    """
    job = None
    try:
        async with async_session() as sdb:
            res = await sdb.execute(select(AgentJob).where(AgentJob.id == uuid.UUID(job_id)))
            job = res.scalar_one_or_none()
    except Exception:  # noqa: BLE001
        job = None
    if job is None:
        return {"event": "error", "data": json.dumps(
            {"error": "Agent-Lauf nicht mehr verfügbar", "_synthetic": True})}
    if job.status == "completed":
        meta = job.metadata_json or {}
        return {"event": "done", "data": json.dumps({
            "message_id": None,
            "content": job.output or "",
            "tools_used": meta.get("tools_used"),
            "_synthetic": True,
        }, ensure_ascii=False)}
    return {"event": "error", "data": json.dumps({
        "error": job.error_message or "Agent-Lauf beendet",
        "_synthetic": True,
    }, ensure_ascii=False)}


@router.get("/conversations/{conversation_id}/agent-stream")
async def stream_agent_events(
    conversation_id: uuid.UUID,
    job_id: str = Query(..., description="Agent-Job-ID aus POST-Antwort"),
    offset: int = Query(0, ge=0, description="Event-Offset für Reconnect"),
    user: User = Depends(require_role("owner")),
):
    """SSE-Stream der Agent-Events. Reconnect-fähig über offset-Parameter.

    Der Client verbindet sich nach dem POST hierhin und erhält alle Events
    ab dem angegebenen Offset. Bei Verbindungsverlust einfach mit dem
    letzten empfangenen Offset erneut verbinden.
    """
    if job_id not in _agent_events:
        raise HTTPException(status_code=404, detail="Agent-Job nicht gefunden oder bereits abgelaufen")

    async def generate():
        idx = offset
        while True:
            events = _agent_events.get(job_id, [])

            while idx < len(events):
                evt = events[idx]
                evt_with_idx = dict(evt)
                data = json.loads(evt.get("data", "{}"))
                data["_idx"] = idx
                evt_with_idx["data"] = json.dumps(data, ensure_ascii=False)
                yield evt_with_idx
                idx += 1

                if evt.get("event") in ("done", "error"):
                    return

            if not _agent_running.get(job_id, False):
                # Kein stiller Close: finalen Status aus der DB lesen und ein
                # synthetisches terminales Event senden, damit der Client immer
                # sauber terminiert (verhindert Endlos-Reconnect mit stale State).
                term = await _terminal_event_from_db(job_id)
                data = json.loads(term.get("data", "{}"))
                data["_idx"] = idx
                term = dict(term)
                term["data"] = json.dumps(data, ensure_ascii=False)
                yield term
                return

            cond = _agent_conditions.get(job_id)
            if cond:
                try:
                    async with cond:
                        await asyncio.wait_for(cond.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": json.dumps({"ts": int(time.time())})}

    return EventSourceResponse(generate())

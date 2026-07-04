"""Semantischer Such-Index: E-Mails + OneDrive-Dokumente in ``semantic_documents``.

Ein periodischer, best-effort Ingest lädt neue/geänderte E-Mails und Dokumente
via Microsoft Graph, extrahiert Text, zerlegt ihn in überlappende Chunks und legt
je Chunk ein lokales Embedding (Qwen3-Embedding-4B, 2560d, halfvec) ab. Der Index
ist die Basis der user-facing semantischen Suche (UI + Hermes-Agent).

Prinzipien:
- **Nur Textdokumente** (Whitelist) -- Bilder/Audio/Video/Archive kosten 0 Speicher.
- **Idempotent** über ``(source_type, source_id, chunk_index)``; geänderte Dokumente
  werden vor dem Neu-Indexieren komplett ersetzt (Chunk-Zahl kann schrumpfen).
- **Datenschutz-souverän**: Embeddings lokal via Ollama, kein Cloud-Egress.
- **Best-effort**: Fehler stoppen weder Scheduler noch Backend.

Getrennt vom 0.6B-Agent-Index (Style-Store/Episoden): eigenes, stärkeres Modell
via ``search_embed_model``/``search_embed_dim``.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.services.context_resolver import ALLOWED_TEXT_EXTENSIONS, _extract_text
from app.services.embeddings import embed_text, to_pgvector
from app.services.learning import html_to_text, strip_quoted_history

logger = logging.getLogger("taskpilot.semantic_index")

# Text-tragende Endungen, die wir indexieren (deckungsgleich mit der
# Extraktions-Pipeline in context_resolver._extract_text). Bilder/Audio/Video/
# Archive fehlen bewusst -> werden nie heruntergeladen/eingebettet.
_DOC_EXTENSIONS = ALLOWED_TEXT_EXTENSIONS | {".pdf", ".docx", ".xlsx"}

# E-Mail-Ordner, die in den Index aufgenommen werden.
_MAIL_FOLDERS = ("inbox", "sentitems", "archive")

_MIN_CHUNK_CHARS = 20

# Scheduler-Handle
_task: asyncio.Task | None = None
_stop = asyncio.Event()


async def _build_graph_client():
    """Baut einen GraphClient aus den Settings (oder None). Lokal, ohne Worker-Import."""
    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        return None
    import sys as _sys

    _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
    from graph_client import GraphClient, GraphConfig  # noqa: E402

    return GraphClient(GraphConfig(
        tenant_id=s.graph_tenant_id,
        client_id=s.graph_client_id,
        client_secret=s.graph_client_secret,
        user_email=s.graph_user_email,
    ))


def _ext_of(name: str) -> str:
    _, _, ext = (name or "").rpartition(".")
    return f".{ext.lower()}" if ext else ""


def is_indexable_file(name: str) -> bool:
    """True, wenn die Datei anhand ihrer Endung als Text indexiert werden soll."""
    return _ext_of(name) in _DOC_EXTENSIONS


def chunk_text(body: str, size: int, overlap: int) -> list[str]:
    """Zerlegt Text in überlappende Chunks (~``size`` Zeichen, ``overlap`` Überlappung).

    Bevorzugt Absatz-/Zeilengrenzen innerhalb des Fensters, um Sätze nicht mitten
    im Wort zu zerschneiden. Rein, deterministisch, gut testbar.
    """
    clean = (body or "").strip()
    if not clean:
        return []
    if size <= 0:
        return [clean]
    overlap = max(0, min(overlap, size - 1))

    chunks: list[str] = []
    start = 0
    n = len(clean)
    while start < n:
        end = min(start + size, n)
        if end < n:
            # Weiches Ende an einer Grenze suchen (letzter Umbruch/Punkt im Fenster).
            window = clean[start:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(". "))
            if cut > size * 0.5:
                end = start + cut + 1
        piece = clean[start:end].strip()
        if len(piece) >= _MIN_CHUNK_CHARS:
            chunks.append(piece)
        if end >= n:
            break
        start = max(end - overlap, start + 1)
    return chunks


async def _upsert_chunks(
    db: AsyncSession,
    *,
    source_type: str,
    source_id: str,
    title: str | None,
    url: str | None,
    mime: str | None,
    metadata: dict,
    chunks: list[str],
    source_modified_at: datetime | None,
) -> int:
    """Ersetzt alle Chunks einer Quelle atomar durch die neuen. Gibt Chunk-Zahl zurück.

    Löscht zuerst bestehende Zeilen (damit eine geschrumpfte Chunk-Zahl keine
    Leichen hinterlässt), embeddet dann jeden Chunk und fügt ihn ein. Ein Chunk
    ohne Embedding (Ollama offline) wird trotzdem gespeichert (tsvector-Keyword
    bleibt nutzbar), damit der Index nicht bei LLM-Ausfall bricht.
    """
    cfg = get_settings()
    await db.execute(
        text("DELETE FROM semantic_documents WHERE source_type = :st AND source_id = :sid"),
        {"st": source_type, "sid": source_id},
    )
    written = 0
    for idx, chunk in enumerate(chunks):
        vec = await embed_text(chunk, model=cfg.search_embed_model, dim=cfg.search_embed_dim)
        emb_literal = to_pgvector(vec) if vec else None
        await db.execute(
            text(
                """
                INSERT INTO semantic_documents
                    (source_type, source_id, chunk_index, title, content_text,
                     url, mime, metadata, embedding, source_modified_at)
                VALUES
                    (:st, :sid, :idx, :title, :body, :url, :mime,
                     CAST(:meta AS jsonb), CAST(:emb AS halfvec),
                     CAST(:modified AS timestamptz))
                ON CONFLICT (source_type, source_id, chunk_index) DO UPDATE SET
                    title = EXCLUDED.title,
                    content_text = EXCLUDED.content_text,
                    url = EXCLUDED.url,
                    mime = EXCLUDED.mime,
                    metadata = EXCLUDED.metadata,
                    embedding = EXCLUDED.embedding,
                    source_modified_at = EXCLUDED.source_modified_at,
                    indexed_at = now()
                """
            ),
            {
                "st": source_type,
                "sid": source_id,
                "idx": idx,
                "title": (title or "")[:500] or None,
                "body": chunk,
                "url": url,
                "mime": mime,
                "meta": _json(metadata),
                "emb": emb_literal,
                # asyncpg erwartet für timestamptz ein echtes datetime-Objekt (auch mit
                # CAST) -- ein ISO-String löst DataError aus. Direkt durchreichen.
                "modified": source_modified_at,
            },
        )
        written += 1
    return written


def _json(obj: dict) -> str:
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return "{}"


def _parse_graph_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def _existing_modified(db: AsyncSession, source_type: str, source_id: str) -> datetime | None:
    row = await db.execute(
        text(
            "SELECT max(source_modified_at) FROM semantic_documents "
            "WHERE source_type = :st AND source_id = :sid"
        ),
        {"st": source_type, "sid": source_id},
    )
    return row.scalar()


async def _index_email(db: AsyncSession, client, message_id: str) -> int:
    """Indexiert eine einzelne E-Mail (voller Body). Skip, wenn bereits vorhanden."""
    existing = await _existing_modified(db, "email", message_id)
    if existing is not None:
        return 0  # E-Mails sind unveränderlich -> bereits indexiert
    msg = await client.get_email(message_id)
    subject = msg.get("subject") or "(kein Betreff)"
    raw = (msg.get("body") or {}).get("content") or msg.get("bodyPreview") or ""
    body = html_to_text(raw) if raw else ""
    body = (strip_quoted_history(body) or body).strip()
    full = f"{subject}\n\n{body}".strip()
    cfg = get_settings()
    chunks = chunk_text(full, cfg.search_chunk_chars, cfg.search_chunk_overlap)
    chunks = chunks[: cfg.search_index_max_chunks_per_doc]
    if not chunks:
        return 0
    sender = ((msg.get("from") or {}).get("emailAddress") or {}).get("address")
    meta = {
        "from": sender,
        "receivedDateTime": msg.get("receivedDateTime"),
        "conversationId": msg.get("conversationId"),
    }
    return await _upsert_chunks(
        db,
        source_type="email",
        source_id=message_id,
        title=subject,
        url=msg.get("webLink"),
        mime="message/rfc822",
        metadata=meta,
        chunks=chunks,
        source_modified_at=_parse_graph_dt(msg.get("receivedDateTime")),
    )


async def _index_document(db: AsyncSession, client, item: dict) -> int:
    """Indexiert ein OneDrive-Dokument. Skip, wenn unverändert (lastModifiedDateTime)."""
    cfg = get_settings()
    item_id = item.get("id")
    name = item.get("name") or ""
    if not item_id or not is_indexable_file(name):
        return 0
    size = item.get("size") or 0
    if size > cfg.search_index_max_file_mb * 1024 * 1024:
        return 0
    modified = _parse_graph_dt(item.get("lastModifiedDateTime"))
    existing = await _existing_modified(db, "onedrive", item_id)
    if existing is not None and modified is not None and existing >= modified:
        return 0  # unverändert
    try:
        data = await client.download_drive_item(item_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Download fehlgeschlagen (%s): %s", name, exc)
        return 0
    mime = (item.get("file") or {}).get("mimeType", "")
    body = _extract_text(data, name, mime)
    if not body or not body.strip():
        return 0
    chunks = chunk_text(body, cfg.search_chunk_chars, cfg.search_chunk_overlap)
    chunks = chunks[: cfg.search_index_max_chunks_per_doc]
    if not chunks:
        return 0
    parent = (item.get("parentReference") or {}).get("path")
    meta = {"size": size, "path": parent}
    return await _upsert_chunks(
        db,
        source_type="onedrive",
        source_id=item_id,
        title=name,
        url=item.get("webUrl"),
        mime=mime or None,
        metadata=meta,
        chunks=chunks,
        source_modified_at=modified,
    )


async def sync_semantic_index(
    *,
    mail_top: int | None = None,
    index_docs: bool = True,
    index_mails: bool = True,
) -> dict:
    """Ein Ingest-Lauf: neue E-Mails + geänderte Dokumente indexieren. Best-effort.

    Gibt eine kleine Statistik zurück (``emails``/``documents``/``chunks``). Über
    ``index_mails``/``index_docs`` lassen sich die beiden Quellen einzeln fahren
    (nützlich für einen entkoppelten Backfill). ``mail_top`` überschreibt die
    Anzahl neuester E-Mails je Ordner (für einen Voll-Backfill hochsetzen).
    """
    cfg = get_settings()
    stats = {"emails": 0, "documents": 0, "chunks": 0}
    if not cfg.search_index_enabled:
        return stats
    client = await _build_graph_client()
    if client is None:
        logger.info("Semantic-Index-Sync übersprungen: Graph nicht konfiguriert")
        return stats

    limit = mail_top or cfg.search_index_mail_top
    try:
        # 1) E-Mails (neueste je Ordner)
        if index_mails:
            seen_ids: set[str] = set()
            for folder in _MAIL_FOLDERS:
                if _stop.is_set():
                    break
                try:
                    data = await client.list_emails(folder=folder, top=limit)
                except Exception as exc:  # noqa: BLE001
                    logger.info("Ordner %s nicht lesbar: %s", folder, exc)
                    continue
                msgs = data.get("value", [])
                logger.info("Backfill E-Mails: Ordner '%s' -> %d Kandidaten", folder, len(msgs))
                for i, msg in enumerate(msgs, 1):
                    mid = msg.get("id")
                    if not mid or mid in seen_ids:
                        continue
                    seen_ids.add(mid)
                    async with async_session() as db:
                        try:
                            n = await _index_email(db, client, mid)
                            await db.commit()
                        except Exception as exc:  # noqa: BLE001
                            await db.rollback()
                            logger.warning("E-Mail %s nicht indexierbar: %s: %s",
                                           mid, type(exc).__name__, exc)
                            n = 0
                    if n:
                        stats["emails"] += 1
                        stats["chunks"] += n
                    if i % 50 == 0:
                        logger.info(
                            "  … %s: %d/%d verarbeitet (neu indexiert: %d, Chunks: %d)",
                            folder, i, len(msgs), stats["emails"], stats["chunks"],
                        )

        # 2) Dokumente (rekursiver Drive-Walk, Whitelist + Change-Detection)
        if index_docs and not _stop.is_set():
            try:
                files = await client.walk_drive_files()
            except Exception as exc:  # noqa: BLE001
                logger.info("Drive-Walk fehlgeschlagen: %s", exc)
                files = []
            indexable = [f for f in files if is_indexable_file(f.get("name") or "")]
            logger.info(
                "Backfill Dokumente: %d Dateien gesamt, davon %d indexierbar",
                len(files), len(indexable),
            )
            for i, item in enumerate(indexable, 1):
                if _stop.is_set():
                    break
                async with async_session() as db:
                    try:
                        n = await _index_document(db, client, item)
                        await db.commit()
                    except Exception as exc:  # noqa: BLE001
                        await db.rollback()
                        logger.warning("Dokument %s nicht indexierbar: %s: %s",
                                       item.get("name"), type(exc).__name__, exc)
                        n = 0
                if n:
                    stats["documents"] += 1
                    stats["chunks"] += n
                if i % 25 == 0:
                    logger.info(
                        "  … Dokumente %d/%d (neu/aktualisiert: %d, Chunks: %d)",
                        i, len(indexable), stats["documents"], stats["chunks"],
                    )

        logger.info(
            "Semantic-Index-Sync fertig: %d E-Mails, %d Dokumente, %d Chunks",
            stats["emails"], stats["documents"], stats["chunks"],
        )
        return stats
    except Exception:  # noqa: BLE001 - best-effort, darf Scheduler nie stoppen
        logger.exception("Semantic-Index-Sync fehlgeschlagen")
        return stats
    finally:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


async def _loop() -> None:
    cfg = get_settings()
    interval = cfg.search_index_interval_seconds
    # Kleiner Startversatz, damit der Boot nicht durch den Walk blockiert.
    try:
        await asyncio.wait_for(_stop.wait(), timeout=60)
        return
    except asyncio.TimeoutError:
        pass
    while not _stop.is_set():
        await sync_semantic_index()
        try:
            await asyncio.wait_for(_stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            continue


async def start_semantic_index() -> None:
    """Startet den periodischen Ingest (Lifespan-Hook)."""
    global _task
    cfg = get_settings()
    if not cfg.search_index_enabled or not cfg.integrations_active:
        logger.info("Semantic-Index-Scheduler inaktiv (enabled=%s, integrations=%s)",
                    cfg.search_index_enabled, cfg.integrations_active)
        return
    if _task and not _task.done():
        return
    _stop.clear()
    _task = asyncio.create_task(_loop())
    logger.info("Semantic-Index-Scheduler gestartet (Intervall %ds)",
                cfg.search_index_interval_seconds)


async def stop_semantic_index() -> None:
    """Stoppt den Ingest sauber (Lifespan-Hook)."""
    _stop.set()
    if _task:
        try:
            await asyncio.wait_for(_task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _task.cancel()
        except Exception:  # noqa: BLE001
            pass

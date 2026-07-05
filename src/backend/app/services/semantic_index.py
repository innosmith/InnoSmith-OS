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
import re
from datetime import datetime, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models import User
from app.services.context_resolver import _extract_text
from app.services.embeddings import embed_text, to_pgvector
from app.services.learning import html_to_text, strip_quoted_history

logger = logging.getLogger("taskpilot.semantic_index")

# OneDrive-Pfade (relativ zu Drive-Root), die standardmaessig NICHT indexiert werden.
# Der KnowledgeFlow-Korpus (Bundesgerichts-Entscheide) ist ein themenfremder Fremd-
# korpus, der den persoenlichen Such-Index sonst dominiert (>50 % der Chunks) und
# ueber reine Vektor-Naehe irrelevante Treffer liefert. Ueber die Owner-Settings
# (``search_excluded_paths``) frei konfigurierbar; diese Konstante ist nur der
# Default, wenn noch nichts konfiguriert wurde.
_DEFAULT_EXCLUDED_PATHS: tuple[str, ...] = (
    "/Shared/KnowledgeFlow/bundesgericht-steuerrecht",
)

# Praefix, mit dem Graph den Drive-Root in ``parentReference.path`` ausweist.
_DRIVE_ROOT_PREFIX = "/drive/root:"

# Endungen für den semantischen SUCH-Index -- bewusst ENG gehalten: nur echte
# Nutzdokumente. Code-/Config-/generische Struktur-Dateien (.py, .js, .ts, .json,
# .xml, .yaml, .html, ...) liegen oft in vielen Projektkopien vor (z. B.
# dossier_context.json, __init__.py) und wären reines Rauschen in der Dokumentsuche.
# Der Agent-Kontext (context_resolver.ALLOWED_TEXT_EXTENSIONS) bleibt breit -- dort
# sind Code/Configs bewusst gewollt. Bilder/Audio/Video/Archive fehlen ebenfalls.
_DOC_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".md", ".txt", ".csv"}

_MIN_CHUNK_CHARS = 20

# NUL (0x00) ist in PostgreSQL text/tsvector HART verboten
# (CharacterNotInRepertoireError) -- manche extrahierten .md-Dateien (z. B.
# UTF-16-/korrupte Exporte) enthalten NUL-Bytes und wuerden sonst den kompletten
# Dokument-Insert sprengen. Weitere C0-Steuerzeichen (ausser Tab/LF/CR) tragen
# keine Suchinformation und werden ebenfalls entfernt.
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_text(s: str | None) -> str:
    """Entfernt NUL- und andere C0-Steuerzeichen (ausser \\t\\n\\r). "" bei None."""
    if not s:
        return ""
    return _CONTROL_CHARS_RE.sub("", s)

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


def _normalize_drive_path(path: str | None) -> str:
    """Reduziert einen Graph-Pfad auf den Teil relativ zum Drive-Root, lowercase.

    ``/drive/root:/Shared/KnowledgeFlow/…`` -> ``/shared/knowledgeflow/…``. So lassen
    sich Item-Pfade robust mit den (relativ notierten) Ausschluss-Praefixen vergleichen.
    """
    p = (path or "").strip()
    idx = p.find(_DRIVE_ROOT_PREFIX)
    if idx >= 0:
        p = p[idx + len(_DRIVE_ROOT_PREFIX):]
    if not p.startswith("/"):
        p = "/" + p
    return p.lower()


def _path_is_excluded(item_path: str | None, excluded: list[str]) -> bool:
    """True, wenn der Item-Pfad unter einem der Ausschluss-Praefixe liegt."""
    if not excluded:
        return False
    norm = _normalize_drive_path(item_path)
    for prefix in excluded:
        pref = _normalize_drive_path(prefix)
        if pref and (norm == pref or norm.startswith(pref.rstrip("/") + "/") or norm.startswith(pref)):
            return True
    return False


async def _load_excluded_paths(db: AsyncSession) -> list[str]:
    """Laedt die Ausschlussliste aus den Owner-Settings (Default, falls nie gesetzt).

    Fehlt der Key ``search_excluded_paths`` vollstaendig, gelten die Defaults; ist er
    vorhanden (auch als leere Liste), wird die explizite Owner-Wahl respektiert.
    """
    row = await db.execute(select(User.settings).where(User.role == "owner").limit(1))
    settings = row.scalar_one_or_none() or {}
    if "search_excluded_paths" not in settings:
        return list(_DEFAULT_EXCLUDED_PATHS)
    val = settings.get("search_excluded_paths")
    if not isinstance(val, list):
        return list(_DEFAULT_EXCLUDED_PATHS)
    return [str(p) for p in val if str(p).strip()]


async def purge_excluded_documents(db: AsyncSession, excluded: list[str]) -> int:
    """Loescht bereits indexierte OneDrive-Zeilen, die unter einem Ausschlusspfad liegen.

    Sorgt dafuer, dass nach Aktivierung eines Ausschlusses keine Leichen im Index
    verbleiben. Match ueber ``metadata->>'path'`` (der gespeicherte Elternpfad).
    Gibt die Anzahl geloeschter Chunk-Zeilen zurueck.
    """
    total = 0
    for prefix in excluded:
        pref = _normalize_drive_path(prefix)
        if not pref:
            continue
        # Praefix-Match gegen den normalisierten Drive-Pfad. ``metadata->>'path'``
        # beginnt mit ``/drive/root:`` -- wir vergleichen den Teil danach.
        res = await db.execute(
            text(
                """
                DELETE FROM semantic_documents
                WHERE source_type = 'onedrive'
                  AND lower(
                        substring(metadata->>'path' from position(:root in metadata->>'path') + :rootlen)
                      ) LIKE :like
                """
            ),
            {
                "root": _DRIVE_ROOT_PREFIX,
                "rootlen": len(_DRIVE_ROOT_PREFIX),
                "like": pref.rstrip("/") + "%",
            },
        )
        total += res.rowcount or 0
    return total


def chunk_text(body: str, size: int, overlap: int) -> list[str]:
    """Zerlegt Text in überlappende Chunks (~``size`` Zeichen, ``overlap`` Überlappung).

    Bevorzugt Absatz-/Zeilengrenzen innerhalb des Fensters, um Sätze nicht mitten
    im Wort zu zerschneiden. Rein, deterministisch, gut testbar.
    """
    clean = _sanitize_text(body).strip()
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
                "title": (_sanitize_text(title)[:500] or None),
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


async def _index_document(
    db: AsyncSession, client, item: dict, excluded: list[str] | None = None
) -> int:
    """Indexiert ein OneDrive-Dokument. Skip, wenn unverändert (lastModifiedDateTime)."""
    cfg = get_settings()
    item_id = item.get("id")
    name = item.get("name") or ""
    if not item_id or not is_indexable_file(name):
        return 0
    # Defense-in-depth: ausgeschlossene Pfade auch hier hart abweisen (der Walk
    # filtert bereits vorher -- dies schuetzt bei Direktaufruf/Race).
    if excluded and _path_is_excluded((item.get("parentReference") or {}).get("path"), excluded):
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

    # Ausschlussliste (Owner-Settings) laden und bestehende Leichen bereinigen --
    # so verschwinden ausgeschlossene Pfade auch aus einem bereits gefuellten Index.
    async with async_session() as db:
        excluded = await _load_excluded_paths(db)
        if excluded:
            try:
                purged = await purge_excluded_documents(db, excluded)
                await db.commit()
                if purged:
                    logger.info("Ausschluss-Purge: %d Zeilen entfernt (%s)", purged, excluded)
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                logger.warning("Ausschluss-Purge fehlgeschlagen: %s", exc)

    # Seitengroesse/Cap fuer die E-Mail-Pagination. ``mail_top`` (One-Off) begrenzt je
    # Ordner; im Daemon-Betrieb ist der Cap 0 (unbegrenzt = Voll-Archiv).
    page_size = cfg.search_index_mail_page_size
    per_folder_cap = mail_top if mail_top is not None else cfg.search_index_mail_max_per_folder
    try:
        # 1) E-Mails: ALLE Ordner (ausser Junk/Geloescht), vollstaendig paginiert.
        if index_mails:
            seen_ids: set[str] = set()
            try:
                folders = await client.iter_all_mail_folders()
            except Exception as exc:  # noqa: BLE001
                logger.info("Ordner-Enumeration fehlgeschlagen: %s", exc)
                folders = []
            logger.info("Backfill E-Mails: %d Ordner (ohne Junk/Geloescht)", len(folders))
            for folder in folders:
                if _stop.is_set():
                    break
                fid = folder.get("id")
                fname = folder.get("displayName") or fid
                if not fid:
                    continue
                processed = 0
                try:
                    async for msg in client.iter_folder_messages(
                        fid, page_size=page_size, max_total=per_folder_cap or 0
                    ):
                        if _stop.is_set():
                            break
                        processed += 1
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
                        if processed % 200 == 0:
                            logger.info(
                                "  … Ordner '%s': %d gesichtet (neu: %d, Chunks: %d)",
                                fname, processed, stats["emails"], stats["chunks"],
                            )
                except Exception as exc:  # noqa: BLE001
                    logger.info("Ordner '%s' nicht (vollstaendig) lesbar: %s", fname, exc)
                logger.info("  Ordner '%s' fertig: %d gesichtet", fname, processed)

        # 2) Dokumente (rekursiver Drive-Walk, Whitelist + Change-Detection)
        if index_docs and not _stop.is_set():
            try:
                files = await client.walk_drive_files()
            except Exception as exc:  # noqa: BLE001
                logger.info("Drive-Walk fehlgeschlagen: %s", exc)
                files = []
            indexable = [f for f in files if is_indexable_file(f.get("name") or "")]
            # Ausgeschlossene Pfade VOR dem Download entfernen (spart ggf. Tausende Downloads).
            before_excl = len(indexable)
            indexable = [
                f for f in indexable
                if not _path_is_excluded((f.get("parentReference") or {}).get("path"), excluded)
            ]
            skipped_excluded = before_excl - len(indexable)
            # In-Run-Dedup: Dieselbe Datei liegt im OneDrive oft physisch mehrfach
            # (verschiedene driveItem-IDs, gleicher Name + Grösse in mehreren Ordnern,
            # privat + geteilt). Ohne Dedup würde jede Kopie als eigenes Dokument
            # eingebettet -> Rauschen im Index. Wir indexieren pro (Name, Grösse) nur
            # die erste Kopie dieses Laufs.
            seen_files: set[tuple[str, int]] = set()
            deduped: list[dict] = []
            for f in indexable:
                key = ((f.get("name") or "").strip().casefold(), int(f.get("size") or 0))
                if key in seen_files:
                    continue
                seen_files.add(key)
                deduped.append(f)
            skipped_dupes = len(indexable) - len(deduped)
            indexable = deduped
            logger.info(
                "Backfill Dokumente: %d gesamt, %d indexierbar, %d ausgeschlossen, %d Duplikate uebersprungen",
                len(files), len(indexable), skipped_excluded, skipped_dupes,
            )
            for i, item in enumerate(indexable, 1):
                if _stop.is_set():
                    break
                async with async_session() as db:
                    try:
                        n = await _index_document(db, client, item, excluded)
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
    """Startet den periodischen Ingest (Lifespan-Hook).

    Standardmaessig deaktiviert (``search_index_in_process=False``): die Indexierung
    laeuft als dedizierter Daemon-Container (``app.scripts.index_daemon``). Nur wenn
    das Flag explizit gesetzt ist, uebernimmt der Backend-Prozess selbst.
    """
    global _task
    cfg = get_settings()
    if not cfg.search_index_in_process:
        logger.info("Semantic-Index-Scheduler in-process AUS (Daemon uebernimmt)")
        return
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

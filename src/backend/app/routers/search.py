import asyncio
import logging
import re
import sys
import time
import uuid
from datetime import date
from pathlib import Path

from cachetools import TTLCache
from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_role
from app.config import get_settings as _get_settings
from app.database import get_db
from app.models import Project, Tag, Task, User
from ai9.semantic_search import hybrid_search

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "pipedrive"))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "email-graph"))

from app.routers.pipedrive import _extract_pic_url

logger = logging.getLogger("taskpilot.search")

_search_cache: TTLCache = TTLCache(maxsize=200, ttl=300)

# Anzahl OneDrive-Treffer in der globalen Instant-Suche. Die Microsoft Search API
# liefert weit mehr (Default 25/Seite, via Paging Hunderte) -- dies ist bewusst ein
# UI-Fenster fuer das Dropdown, analog zur Task-Grenze (20).
_ONEDRIVE_SEARCH_TOP = 20

router = APIRouter(prefix="/api/search", tags=["search"])


class SearchTaskHit(BaseModel):
    id: uuid.UUID
    title: str
    project_id: uuid.UUID
    project_name: str
    assignee: str
    is_completed: bool
    due_date: date | None


class SearchProjectHit(BaseModel):
    id: uuid.UUID
    name: str
    color: str
    status: str


class SearchTagHit(BaseModel):
    id: uuid.UUID
    name: str
    color: str


class CrmSearchHit(BaseModel):
    id: int | str
    name: str
    type: str
    detail: str | None = None
    email: str | None = None
    pic_url: str | None = None


class TogglHit(BaseModel):
    id: int
    name: str
    type: str  # "client" | "project"
    workspace_id: int | None = None


class BexioHit(BaseModel):
    id: int
    name: str
    type: str  # "contact" | "order" | "project"
    email: str | None = None


class SearchResults(BaseModel):
    tasks: list[SearchTaskHit]
    projects: list[SearchProjectHit]
    tags: list[SearchTagHit]
    crm: list[CrmSearchHit]
    toggl: list[TogglHit]
    bexio: list[BexioHit]
    signa: list["SignaHit"]


class SignaHit(BaseModel):
    id: int
    title: str
    type: str  # "rss" | "youtube" | "web"
    score: float | None = None
    source: str | None = None


class FileHit(BaseModel):
    id: str
    name: str
    size: int | None = None
    last_modified: str | None = None
    web_url: str | None = None
    is_folder: bool = False
    path: str | None = None
    snippet: str | None = None
    thumbnail_url: str | None = None
    mime_type: str | None = None


SearchResults.model_rebuild()


async def _search_pipedrive(user: User, term: str) -> list[CrmSearchHit]:
    """Pipedrive-Suche mit Timeout, Cache und Fallback (blockiert nie die lokale Suche)."""
    cache_key = term.strip().lower()
    cached_result = _search_cache.get(cache_key)
    if cached_result is not None:
        return cached_result

    try:
        from pipedrive_client import PipedriveClient, PipedriveConfig  # noqa: E402
        from app.routers.pipedrive import _person_cache

        settings = user.settings or {}
        token = settings.get("pipedrive_api_token") or ""
        domain = settings.get("pipedrive_domain") or "innosmith"

        if not token:
            from app.config import get_settings
            app_cfg = get_settings()
            token = app_cfg.pipedrive_api_token
            domain = app_cfg.pipedrive_domain or domain

        if not token:
            return []

        client = PipedriveClient(PipedriveConfig(api_token=token, company_domain=domain))
        raw = await asyncio.wait_for(
            client.search_items(term, "deal,person,organization", 8),
            timeout=5.0,
        )
        results: list[CrmSearchHit] = []
        for item in raw:
            item_data = item.get("item", item)
            item_type = item.get("item_type") or item_data.get("type", "")
            name = item_data.get("title") or item_data.get("name") or ""
            detail = None
            email = None
            pic_url = None
            if item_type == "person":
                org = item_data.get("organization", {})
                detail = org.get("name") if isinstance(org, dict) else None
                emails = item_data.get("emails", []) or item_data.get("primary_email", "")
                if isinstance(emails, list) and emails:
                    email = emails[0] if isinstance(emails[0], str) else emails[0].get("value", "")
                elif isinstance(emails, str):
                    email = emails
                if email:
                    cached = _person_cache.get(email.strip().lower())
                    if cached is not None and cached:
                        pic_url = cached.pic_url
                if not pic_url:
                    person_id = item_data.get("id")
                    if person_id:
                        try:
                            full_person = await client.get_person_v1(person_id)
                            pic_url = _extract_pic_url(full_person)
                        except Exception:
                            pass
            elif item_type == "deal":
                detail = item_data.get("person_name") or item_data.get("org_name")
            elif item_type == "organization":
                detail = item_data.get("address")
            results.append(CrmSearchHit(
                id=item_data.get("id", 0),
                name=name,
                type=item_type,
                detail=detail,
                email=email,
                pic_url=pic_url,
            ))
        _search_cache[cache_key] = results
        return results
    except Exception as exc:
        logger.debug("Pipedrive-Suche fehlgeschlagen (wird ignoriert): %s", exc)
        return []


async def _search_toggl(user: User, term: str) -> list[TogglHit]:
    """Toggl-Suche: Clients + Projekte (lokal gefiltert)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "toggl"))
        from toggl_client import TogglClient, TogglConfig

        settings = user.settings or {}
        token = settings.get("toggl_api_token") or ""
        ws_id = int(settings.get("toggl_workspace_id") or 0)
        if not token:
            from app.config import get_settings
            cfg = get_settings()
            token = cfg.toggl_api_token
            ws_id = ws_id or cfg.toggl_workspace_id
        if not token or not ws_id:
            return []

        client = TogglClient(TogglConfig(api_token=token, workspace_id=ws_id))
        clients_task = client.search_clients(term, ws_id)
        projects_task = client.search_projects(term, ws_id)
        clients_raw, projects_raw = await asyncio.wait_for(
            asyncio.gather(clients_task, projects_task),
            timeout=5.0,
        )
        hits: list[TogglHit] = []
        for c in (clients_raw or [])[:5]:
            hits.append(TogglHit(id=c["id"], name=c.get("name", ""), type="client", workspace_id=ws_id))
        for p in (projects_raw or [])[:5]:
            hits.append(TogglHit(id=p["id"], name=p.get("name", ""), type="project", workspace_id=ws_id))
        return hits
    except Exception as exc:
        logger.debug("Toggl-Suche fehlgeschlagen (wird ignoriert): %s", exc)
        return []


async def _search_bexio(user: User, term: str) -> list[BexioHit]:
    """Bexio-Suche: Kontakte nach Name."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "bexio"))
        from bexio_client import BexioClient, BexioConfig

        settings = user.settings or {}
        token = settings.get("bexio_api_token") or ""
        if not token:
            from app.config import get_settings
            cfg = get_settings()
            token = cfg.bexio_api_token
        if not token:
            return []

        client = BexioClient(BexioConfig(api_token=token))
        contacts_raw = await asyncio.wait_for(
            client.search_contact_by_name(term),
            timeout=5.0,
        )
        hits: list[BexioHit] = []
        for c in (contacts_raw or [])[:8]:
            name = c.get("name_1", "")
            if c.get("name_2"):
                name = f"{name} {c['name_2']}"
            hits.append(BexioHit(
                id=c["id"],
                name=name,
                type="contact",
                email=c.get("mail"),
            ))
        return hits
    except Exception as exc:
        logger.debug("Bexio-Suche fehlgeschlagen (wird ignoriert): %s", exc)
        return []


async def _search_signa(term: str) -> list[SignaHit]:
    """SIGNA-Signale nach Titel durchsuchen."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "signa"))
        from signa_client import SignaClient, SignaConfig

        from app.config import get_settings
        s = get_settings()
        cfg = SignaConfig(
            host=s.isi_host, database=s.isi_db,
            user=s.isi_user, password=s.isi_secret, port=s.isi_port,
        )
        if not cfg.is_configured:
            return []

        client = SignaClient(cfg)
        try:
            rows = await asyncio.wait_for(
                client.search_signals(term, min_score=0, limit=8),
                timeout=5.0,
            )
            return [
                SignaHit(
                    id=r["id"],
                    title=r.get("title", ""),
                    type=r.get("type", "rss"),
                    score=float(r["total_score"]) if r.get("total_score") is not None else None,
                    source=r.get("source_name"),
                )
                for r in rows
            ]
        finally:
            await client.close()
    except Exception as exc:
        logger.debug("SIGNA-Suche fehlgeschlagen (wird ignoriert): %s", exc)
        return []


def _strip_hit_highlights(summary: str | None) -> str | None:
    """Wandelt das Search-API-Snippet in sauberen Plaintext.

    Microsoft markiert Treffer mit ``<c0>…</c0>`` (Hit-Highlight) und trennt
    Snippet-Fragmente mit ``<ddd/>`` (Ellipsis). Wir entfernen die Highlights und
    ersetzen ``<ddd/>`` durch ein echtes Auslassungszeichen; Mehrfach-Whitespace
    wird kollabiert. (Kein HTML-Rendering im Frontend -> reiner Text.)
    """
    if not summary:
        return None
    s = summary.replace("<ddd/>", " … ")
    s = re.sub(r"</?c\d+>", "", s)
    s = re.sub(r"\s+", " ", s).strip(" …")
    return s or None


# Prozessweiter Cache der Tenant-Region (Microsoft Search API, app-only Pflicht).
# WICHTIG fuer Determinismus: Wir cachen ausschliesslich ERFOLGE. Ein transienter
# Timeout bei der Aufloesung (z. B. direkt nach einem Neustart unter Last) darf NICHT
# dazu fuehren, dass fuer die gesamte Prozess-Lebensdauer keine Snippets mehr kommen.
# Nach einem Fehlversuch wird daher (throttled) erneut aufgeloest, bis es klappt.
_region_value: str | None = None      # aufgeloeste Region, sobald bekannt (sticky)
_region_last_try: float = 0.0         # monotonic-Timestamp des letzten Versuchs
_REGION_RETRY_SECONDS = 60.0          # Mindestabstand zwischen Fehlversuchen


async def _resolve_region(client, settings) -> str | None:
    """Liefert die Search-API-Region -- deterministisch und selbstheilend.

    Reihenfolge: explizite Config (``TP_GRAPH_SEARCH_REGION``) > bereits aufgelöster
    Cache-Wert > Neuauflösung. Fehlversuche werden bewusst NICHT gecacht (nur
    zeitlich gedrosselt), damit die Vorschau nach einem transienten Fehler von
    selbst zurückkommt statt bis zum nächsten Neustart auszufallen.
    """
    global _region_value, _region_last_try
    if settings.graph_search_region:
        return settings.graph_search_region
    if _region_value:
        return _region_value
    now = time.monotonic()
    if now - _region_last_try < _REGION_RETRY_SECONDS:
        return None  # kürzlich fehlgeschlagen -> kurz nicht erneut hämmern
    _region_last_try = now
    try:
        region = await asyncio.wait_for(client.get_search_region(), timeout=15.0)
    except Exception:  # noqa: BLE001
        region = None
    if region:
        _region_value = region  # nur Erfolge sind sticky
    return region


def _file_dedupe_key(name: str, size: int | None) -> str:
    """Dedup-Schlüssel für OneDrive-Treffer.

    Dieselbe logische Datei existiert im OneDrive oft physisch mehrfach (verschiedene
    ``driveItem``-IDs in unterschiedlichen Ordnern, privat + geteilt). Dedup auf der
    ID greift daher NICHT -- wir normalisieren stattdessen auf ``name`` (+ ``size`` als
    Tie-Breaker gegen echte Namensgleichheit unterschiedlicher Dateien).
    """
    norm = (name or "").strip().casefold()
    return f"{norm}|{size if size is not None else ''}"


def _dedupe_file_hits(hits: list[FileHit]) -> list[FileHit]:
    """Dedupliziert OneDrive-Treffer nach normalisiertem Namen (+ Grösse).

    Behält pro Schlüssel den ersten (relevanz-höchsten) Treffer, ergänzt aber das
    längste verfügbare Snippet als beste Vorschau. Reihenfolge bleibt erhalten.
    """
    by_key: dict[str, FileHit] = {}
    order: list[str] = []
    for h in hits:
        key = _file_dedupe_key(h.name, h.size)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = h
            order.append(key)
            continue
        if len(h.snippet or "") > len(existing.snippet or ""):
            existing.snippet = h.snippet
    return [by_key[k] for k in order]


async def _search_onedrive(term: str) -> list[FileHit]:
    """OneDrive-Dokumente durchsuchen -- mit Inhalts-Vorschau (Microsoft Search API).

    Bevorzugt die Search API (``POST /search/query``, EntityType ``driveItem``):
    Sie liefert app-only ein ``summary``-Snippet mit Treffer-Highlight. Dafür ist
    im App-only-Modus eine ``region`` Pflicht -- wir ermitteln sie automatisch aus
    ``siteCollection.dataLocationCode`` (oder ``TP_GRAPH_SEARCH_REGION``) und cachen
    sie. Ist die Region nicht ermittelbar, fällt die Suche auf die reine Namens-/
    Metadaten-Route (``drive/root/search``) ohne Snippet zurück. Thumbnails werden
    im reaktiven Instant-Pfad bewusst NICHT geladen (sonst N Graph-Calls je
    Tastendruck) -- der Snippet ist die eigentliche Vorschau. Ergebnisse werden nach
    normalisiertem Namen dedupliziert (dieselbe Datei liegt oft mehrfach im Drive).
    """
    try:
        from graph_client import GraphClient, GraphConfig  # noqa: E402
        s = _get_settings()
        if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
            return []
        client = GraphClient(GraphConfig(
            tenant_id=s.graph_tenant_id,
            client_id=s.graph_client_id,
            client_secret=s.graph_client_secret,
            user_email=s.graph_user_email,
        ))
        try:
            region = await _resolve_region(client, s)
            if region:
                hits = await asyncio.wait_for(
                    client.search_query(
                        term, entity_types=["driveItem"], region=region,
                        top=_ONEDRIVE_SEARCH_TOP,
                    ),
                    timeout=8.0,
                )
                results: list[FileHit] = []
                for h in hits:
                    res = h.get("resource") or {}
                    if not res.get("id"):
                        continue
                    results.append(FileHit(
                        id=res.get("id", ""),
                        name=res.get("name", ""),
                        size=res.get("size"),
                        last_modified=res.get("lastModifiedDateTime"),
                        web_url=res.get("webUrl"),
                        is_folder=bool(res.get("folder")),
                        path=(res.get("parentReference") or {}).get("path", ""),
                        snippet=_strip_hit_highlights(h.get("summary")),
                        mime_type=(res.get("file") or {}).get("mimeType"),
                    ))
                return _dedupe_file_hits(results)

            # Fallback ohne Region: Namens-/Metadaten-Suche (kein Snippet).
            items = await asyncio.wait_for(
                client.search_drive(term, top=_ONEDRIVE_SEARCH_TOP), timeout=8.0
            )
            fallback = [
                FileHit(
                    id=item.get("id", ""),
                    name=item.get("name", ""),
                    size=item.get("size"),
                    last_modified=item.get("lastModifiedDateTime"),
                    web_url=item.get("webUrl"),
                    is_folder=bool(item.get("folder")),
                    path=(item.get("parentReference") or {}).get("path", ""),
                    mime_type=(item.get("file") or {}).get("mimeType"),
                )
                for item in items
            ]
            return _dedupe_file_hits(fallback)
        finally:
            await client.close()
    except Exception as exc:
        logger.debug("OneDrive-Suche fehlgeschlagen (wird ignoriert): %s", exc)
        return []


# Live-E-Mail-Fenster (Graph $search, ganzes Postfach). Grosszügig gewählt, damit
# echte Treffer vollständig ankommen -- keine künstliche Repräsentations-Quote.
_EMAIL_SEARCH_TOP = 25


async def _search_emails_live(term: str) -> list[dict]:
    """E-Mails live über das GANZE Postfach durchsuchen (Graph ``$search``).

    Symmetrisch zu ``_search_onedrive``: garantiert vollständige E-Mail-Abdeckung
    unabhängig vom (nur teilweise backfilled) lokalen Index. Liefert normalisierte
    Kandidaten-Dicts (siehe ``_merge_documents``); ``id`` = Graph-Message-ID, damit
    Live- und Index-Treffer derselben Mail verschmelzen. Best-effort mit Timeout.
    """
    try:
        from graph_client import GraphClient, GraphConfig  # noqa: E402
        s = _get_settings()
        if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
            return []
        client = GraphClient(GraphConfig(
            tenant_id=s.graph_tenant_id,
            client_id=s.graph_client_id,
            client_secret=s.graph_client_secret,
            user_email=s.graph_user_email,
        ))
        try:
            msgs = await asyncio.wait_for(
                client.search_emails(term, top=_EMAIL_SEARCH_TOP), timeout=8.0
            )
        finally:
            await client.close()
        out: list[dict] = []
        for m in msgs:
            mid = m.get("id")
            if not mid:
                continue
            frm = (m.get("from") or {}).get("emailAddress") or {}
            sender = frm.get("name") or frm.get("address")
            preview = (m.get("bodyPreview") or "").strip()
            snippet = f"{sender}: {preview}" if sender and preview else (preview or sender)
            out.append({
                "source_type": "email",
                "id": mid,
                "title": m.get("subject") or "(kein Betreff)",
                "url": m.get("webLink"),
                "mime_type": "message/rfc822",
                "snippet": snippet or None,
                # Graph $search ist keyword-basiert -> verlaessliches Relevanzsignal.
                "matched_keyword": True,
            })
        return out
    except Exception as exc:
        logger.debug("Live-E-Mail-Suche fehlgeschlagen (wird ignoriert): %s", exc)
        return []


@router.get("", response_model=SearchResults)
async def search(
    q: str = Query(..., min_length=1),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("owner")),
) -> SearchResults:
    logger.info("Search-Request: q=%r, user=%s", q, user.email)
    pattern = f"%{q}%"

    db_task = db.execute(
        select(Task, Project.name.label("project_name"))
        .join(Project, Task.project_id == Project.id)
        .where(or_(Task.title.ilike(pattern), Task.description.ilike(pattern)))
        .order_by(Task.updated_at.desc())
        .limit(20)
    )
    db_project = db.execute(
        select(Project).where(Project.name.ilike(pattern)).order_by(Project.name).limit(10)
    )
    db_tag = db.execute(
        select(Tag).where(Tag.name.ilike(pattern)).order_by(Tag.name).limit(10)
    )
    crm_task = _search_pipedrive(user, q)
    toggl_task = _search_toggl(user, q)
    bexio_task = _search_bexio(user, q)
    signa_task = _search_signa(q)

    task_result, project_result, tag_result, crm_results, toggl_results, bexio_results, signa_results = await asyncio.gather(
        db_task, db_project, db_tag, crm_task, toggl_task, bexio_task, signa_task,
    )

    tasks = [
        SearchTaskHit(
            id=t.id, title=t.title, project_id=t.project_id,
            project_name=pname, assignee=t.assignee,
            is_completed=t.is_completed, due_date=t.due_date,
        )
        for t, pname in task_result.all()
    ]

    projects = [
        SearchProjectHit(id=p.id, name=p.name, color=p.color, status=p.status)
        for p in project_result.scalars().all()
    ]

    tags = [
        SearchTagHit(id=t.id, name=t.name, color=t.color)
        for t in tag_result.scalars().all()
    ]

    return SearchResults(tasks=tasks, projects=projects, tags=tags, crm=crm_results, toggl=toggl_results, bexio=bexio_results, signa=signa_results)


class SemanticHit(BaseModel):
    source_type: str
    source_id: str
    title: str | None = None
    url: str | None = None
    mime: str | None = None
    snippet: str | None = None
    chunk_index: int | None = None
    score: float | None = None
    similarity: float | None = None
    matched_keyword: bool = False


class SemanticSearchResults(BaseModel):
    query: str
    mode: str
    results: list[SemanticHit]


@router.get("/semantic", response_model=SemanticSearchResults)
async def semantic_search(
    q: str = Query(..., min_length=2),
    mode: str = Query("hybrid", pattern="^(hybrid|semantic|exact)$"),
    sources: str | None = Query(
        None, description="Komma-Liste der Quelltypen: email,onedrive,upload,transcript"
    ),
    limit: int = Query(50, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("owner")),
) -> SemanticSearchResults:
    """Semantische/hybride Volltextsuche über den lokalen Dokument- und E-Mail-Index.

    Getrennt von der globalen Instant-Suche (``GET /api/search``): Dieser Endpoint
    bedient den bewusst-semantischen Pfad (Enter im Suchdialog, Inbox-Suche,
    Agenten-RAG). ``mode`` steuert Hybrid (Default), rein semantisch oder exakt.
    """
    logger.info("Semantic-Search: q=%r mode=%s sources=%s user=%s", q, mode, sources, user.email)
    src_list = [s.strip() for s in sources.split(",") if s.strip()] if sources else None
    hits = await hybrid_search(db, q, sources=src_list, k=limit, mode=mode, user_id=str(user.id))
    return SemanticSearchResults(
        query=q, mode=mode, results=[SemanticHit(**h) for h in hits]
    )


class DocHit(BaseModel):
    source_type: str  # 'onedrive' | 'email' | 'upload' | 'transcript'
    id: str
    title: str | None = None
    url: str | None = None
    mime_type: str | None = None
    snippet: str | None = None
    score: float | None = None
    matched_keyword: bool = False


class DocumentSearchResults(BaseModel):
    query: str
    results: list[DocHit]


# RRF-Konstante für die Doc-Fusion (identisch zur semantischen Fusion, siehe
# semantic_search._RRF_K): dämpft den Einfluss sehr hoher Einzelränge.
_DOC_RRF_K = 60

def _doc_merge_key(source_type: str, title: str | None, source_id: str) -> str:
    """Merge-Schlüssel über beide Doc-Quellen (Live + Index).

    Datei-artige Quellen (``onedrive``/``upload``) werden über den normalisierten
    Titel/Dateinamen zusammengeführt -- so kollabiert dieselbe Datei aus Live-Suche
    und Index (bzw. mehrere physische Kopien) zu einer Zeile. E-Mails/Transkripte
    haben keinen dedup-fähigen Dateinamen und werden über ``source_id`` gehalten.
    """
    if source_type in ("onedrive", "upload"):
        norm = (title or "").strip().casefold()
        if norm:
            return f"file:{norm}"
    return f"{source_type}:{source_id}"


def _file_candidates(hits: list[FileHit]) -> list[dict]:
    """OneDrive-Live-Treffer → normalisierte Merge-Kandidaten (Ordner ausgeschlossen)."""
    return [
        {
            "source_type": "onedrive",
            "id": f.id,
            "title": f.name,
            "url": f.web_url,
            "mime_type": f.mime_type,
            "snippet": f.snippet,
            # Microsoft Search API ist keyword-basiert -> verlaessliches Relevanzsignal.
            "matched_keyword": True,
        }
        for f in hits
        if not f.is_folder
    ]


def _index_candidates(items: list[dict]) -> list[dict]:
    """Hybrid-Index-Treffer → normalisierte Merge-Kandidaten (``mime``→``mime_type``)."""
    return [
        {
            "source_type": it["source_type"],
            "id": it["source_id"],
            "title": it.get("title"),
            "url": it.get("url"),
            "mime_type": it.get("mime"),
            "snippet": it.get("snippet"),
            "matched_keyword": it.get("matched_keyword", False),
        }
        for it in items
    ]


def _merge_documents(*ranklists: list[dict], k: int = _DOC_RRF_K) -> list[DocHit]:
    """Fusioniert beliebig viele (bereits sortierte) Kandidaten-Ranglisten via RRF.

    Jede Liste (Live-Mail, Live-OneDrive, Index pro Quelltyp) trägt unabhängig
    ``1/(k + rang)`` zum Score bei -- Präsenz entsteht also aus dem Retrieval, die
    Fusion bestimmt allein die Reihenfolge und kann keine Quelle verdrängen.

    Treffer derselben Entität (siehe ``_doc_merge_key``: Dateien über den Namen,
    E-Mails/Transkripte über die ID) verschmelzen zu einer Zeile. Feld-Regeln:
    Identität (``url``/``mime_type``) übernimmt der erstplatzierte Treffer -- da Live-
    Listen zuerst übergeben werden, gewinnt deren klickbarer Link; als Vorschau
    gewinnt das längste (informativste) Snippet.
    """
    scores: dict[str, float] = {}
    merged: dict[str, dict] = {}
    order: list[str] = []

    for cands in ranklists:
        for rank, it in enumerate(cands):
            key = _doc_merge_key(it["source_type"], it.get("title"), it["id"])
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            existing = merged.get(key)
            if existing is None:
                merged[key] = {
                    "source_type": it["source_type"],
                    "id": it["id"],
                    "title": it.get("title"),
                    "url": it.get("url"),
                    "mime_type": it.get("mime_type"),
                    "snippet": it.get("snippet"),
                    "matched_keyword": bool(it.get("matched_keyword")),
                }
                order.append(key)
                continue
            if len(it.get("snippet") or "") > len(existing.get("snippet") or ""):
                existing["snippet"] = it.get("snippet")
            if not existing.get("url"):
                existing["url"] = it.get("url")
            if not existing.get("mime_type"):
                existing["mime_type"] = it.get("mime_type")
            # Keyword-Deckung ist sticky: sobald EINE Quelle den Treffer per Keyword
            # gefunden hat, gilt er als klarer Treffer.
            if it.get("matched_keyword"):
                existing["matched_keyword"] = True

    # Keyword-First: klare (keyword-gedeckte) Treffer immer vor rein-semantische;
    # innerhalb jeder Gruppe entscheidet der RRF-Score. Deterministisch, entfernt nichts.
    # Bei rein-konzeptuellen Queries ohne Keyword-Treffer bleibt die semantische
    # Reihenfolge erhalten (alle matched_keyword=False -> reiner Score-Sort).
    ranked = sorted(order, key=lambda key: (merged[key]["matched_keyword"], scores[key]), reverse=True)
    out: list[DocHit] = []
    for key in ranked:
        d = merged[key]
        d["score"] = round(scores[key], 6)
        out.append(DocHit(**d))
    return out


@router.get("/documents", response_model=DocumentSearchResults)
async def search_documents(
    q: str = Query(..., min_length=2),
    limit: int = Query(50, ge=1, le=200),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("owner")),
) -> DocumentSearchResults:
    """Symmetrische Dokument-/E-Mail-Suche: pro Quelle live + Index, dann RRF-Fusion.

    Leitprinzip: **Präsenz durch Retrieval, Reihenfolge durch Fusion.** Jede Quelle
    wird eigenständig abgefragt -- live über die volle API (OneDrive-Drive,
    E-Mail-Postfach) UND über den lokalen Hybrid-Index (je Quelltyp getrennt). Weil
    keine Quelle beim Abruf mit einer anderen um Plätze konkurriert, kann die (stark
    gewachsene) OneDrive-Menge die E-Mails nicht mehr verdrängen. ``_merge_documents``
    fusioniert alle Ranglisten via RRF und verschmilzt Dubletten (Datei über Namen,
    E-Mail über Message-ID). Best-effort: fällt eine Quelle aus, bleiben die anderen.
    """
    logger.info("Document-Search: q=%r user=%s", q, user.email)
    # Fünf unabhängige Ranglisten, parallel. Live zuerst (klickbare Links/Snippets
    # gewinnen beim Merge), Index je Quelltyp getrennt (keine Verdrängung beim Abruf).
    live_drive, live_mail, idx_drive, idx_mail, idx_other = await asyncio.gather(
        _search_onedrive(q),
        _search_emails_live(q),
        hybrid_search(db, q, mode="hybrid", sources=["onedrive"], k=limit, user_id=str(user.id)),
        hybrid_search(db, q, mode="hybrid", sources=["email"], k=limit, user_id=str(user.id)),
        hybrid_search(db, q, mode="hybrid", sources=["upload", "transcript"], k=limit, user_id=str(user.id)),
        return_exceptions=True,
    )
    labelled = {
        "Live-OneDrive": live_drive,
        "Live-E-Mail": live_mail,
        "Index-OneDrive": idx_drive,
        "Index-E-Mail": idx_mail,
        "Index-Upload/Transkript": idx_other,
    }
    for name, res in list(labelled.items()):
        if isinstance(res, BaseException):
            logger.debug("Document-Search-Quelle '%s' fehlgeschlagen: %s", name, res)
            labelled[name] = []
    results = _merge_documents(
        _file_candidates(labelled["Live-OneDrive"]),
        labelled["Live-E-Mail"],
        _index_candidates(labelled["Index-OneDrive"]),
        _index_candidates(labelled["Index-E-Mail"]),
        _index_candidates(labelled["Index-Upload/Transkript"]),
    )[:limit]
    return DocumentSearchResults(query=q, results=results)

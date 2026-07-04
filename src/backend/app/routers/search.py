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
from app.services.semantic_search import hybrid_search

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
    files: list["FileHit"]


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


async def _search_onedrive(term: str) -> list[FileHit]:
    """OneDrive-Dokumente durchsuchen -- mit Inhalts-Vorschau (Microsoft Search API).

    Bevorzugt die Search API (``POST /search/query``, EntityType ``driveItem``):
    Sie liefert app-only ein ``summary``-Snippet mit Treffer-Highlight. Dafür ist
    im App-only-Modus eine ``region`` Pflicht -- wir ermitteln sie automatisch aus
    ``siteCollection.dataLocationCode`` (oder ``TP_GRAPH_SEARCH_REGION``) und cachen
    sie. Ist die Region nicht ermittelbar, fällt die Suche auf die reine Namens-/
    Metadaten-Route (``drive/root/search``) ohne Snippet zurück. Thumbnails werden
    im reaktiven Instant-Pfad bewusst NICHT geladen (sonst N Graph-Calls je
    Tastendruck) -- der Snippet ist die eigentliche Vorschau.
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
                    ))
                return results

            # Fallback ohne Region: Namens-/Metadaten-Suche (kein Snippet).
            items = await asyncio.wait_for(
                client.search_drive(term, top=_ONEDRIVE_SEARCH_TOP), timeout=8.0
            )
            return [
                FileHit(
                    id=item.get("id", ""),
                    name=item.get("name", ""),
                    size=item.get("size"),
                    last_modified=item.get("lastModifiedDateTime"),
                    web_url=item.get("webUrl"),
                    is_folder=bool(item.get("folder")),
                    path=(item.get("parentReference") or {}).get("path", ""),
                )
                for item in items
            ]
        finally:
            await client.close()
    except Exception as exc:
        logger.debug("OneDrive-Suche fehlgeschlagen (wird ignoriert): %s", exc)
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
    files_task = _search_onedrive(q)

    task_result, project_result, tag_result, crm_results, toggl_results, bexio_results, signa_results, file_results = await asyncio.gather(
        db_task, db_project, db_tag, crm_task, toggl_task, bexio_task, signa_task, files_task,
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

    return SearchResults(tasks=tasks, projects=projects, tags=tags, crm=crm_results, toggl=toggl_results, bexio=bexio_results, signa=signa_results, files=file_results)


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
    hits = await hybrid_search(db, q, sources=src_list, k=limit, mode=mode)
    return SemanticSearchResults(
        query=q, mode=mode, results=[SemanticHit(**h) for h in hits]
    )

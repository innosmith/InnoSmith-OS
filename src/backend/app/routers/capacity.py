"""FastAPI Router für Kapazitätsplanung (Runn.io-Klon).

Verwaltet:
- Kapazitätsprojekte (eigene Entität, entkoppelt von TaskPilot-Projekten)
- Wochen-Zuweisungen (Allocations) mit Serien-Unterstützung
- Freie Tage (Ferien, Feiertage, Krankheit)
- Aggregationen (Wochen-Summary, Forecast-Revenue)
- Plan-vs-Ist-Vergleich (via Toggl Track, 24h-Cache)
"""

import logging
import sys
import uuid
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from cachetools import TTLCache
from fastapi import APIRouter, Body, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_role
from app.database import async_session
from app.models import CapacityAllocation, CapacityProject, CapacityTimeOff, Project, User
from app.services.finance_settings import get_forecast_settings_from_settings

_SRC = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_SRC / "toggl"))
sys.path.insert(0, str(_SRC / "capacity"))
from capacity_report import (  # noqa: E402
    Allocation,
    actual_minutes_by_toggl_project,
    planned_minutes,
)
from toggl_client import TogglClient, TogglConfig  # noqa: E402

logger = logging.getLogger("taskpilot.capacity")

router = APIRouter(prefix="/api/capacity", tags=["capacity"])

_toggl_cache: TTLCache = TTLCache(maxsize=20, ttl=86400)


# ── Pydantic Schemas ─────────────────────────────────────────────────────────


class CapacityProjectCreate(BaseModel):
    name: str
    color: str = "#3B82F6"
    icon_url: str | None = None
    icon_emoji: str | None = None
    client_name: str | None = None
    hourly_rate: float | None = None
    is_billable: bool = True
    status: str = "bestätigt"
    project_id: str | None = None
    toggl_project_id: int | None = None
    toggl_client_id: int | None = None
    toggl_billable_filter: str | None = None
    pipedrive_deal_id: int | None = None
    notes: str | None = None


class CapacityProjectUpdate(BaseModel):
    name: str | None = None
    color: str | None = None
    icon_url: str | None = None
    icon_emoji: str | None = None
    client_name: str | None = None
    hourly_rate: float | None = None
    is_billable: bool | None = None
    status: str | None = None
    project_id: str | None = None
    toggl_project_id: int | None = None
    toggl_client_id: int | None = None
    toggl_billable_filter: str | None = None
    pipedrive_deal_id: int | None = None
    sort_order: int | None = None
    notes: str | None = None


class CapacityProjectOut(BaseModel):
    id: str
    name: str
    color: str
    icon_url: str | None
    icon_emoji: str | None
    client_name: str | None
    hourly_rate: float | None
    is_billable: bool
    status: str
    project_id: str | None
    toggl_project_id: int | None
    toggl_client_id: int | None
    toggl_billable_filter: str | None
    pipedrive_deal_id: int | None
    sort_order: int
    notes: str | None
    created_at: str
    updated_at: str
    alloc_count: int = 0  # Gesamtzahl Zuweisungen (über alle Zeiträume)


class AllocationCreate(BaseModel):
    capacity_project_id: str
    week_start: str  # ISO date (Montag bei type=week, Mo-Sa bei type=day)
    minutes: int
    allocation_type: str = "week"  # "week" oder "day"
    notes: str | None = None


class AllocationRepeat(BaseModel):
    capacity_project_id: str
    week_start: str
    end_date: str
    minutes: int
    allocation_type: str = "week"  # "week" oder "day"
    interval_weeks: int = 1
    notes: str | None = None


class AllocationUpdate(BaseModel):
    minutes: int | None = None
    allocation_type: str | None = None
    notes: str | None = None
    week_start: str | None = None


class AllocationOut(BaseModel):
    id: str
    capacity_project_id: str
    week_start: str
    minutes: int
    allocation_type: str
    series_id: str | None
    notes: str | None
    created_at: str
    updated_at: str


class BulkAction(BaseModel):
    action: str  # delete, delete_from, shift, clone, update, update_from
    series_id: str | None = None
    ids: list[str] | None = None
    from_week: str | None = None
    weeks: int | None = None
    minutes: int | None = None
    target_project_id: str | None = None


class TimeOffCreate(BaseModel):
    date: str
    type: str = "ferien"
    label: str | None = None
    hours: float = 8.0


class TimeOffOut(BaseModel):
    id: str
    date: str
    type: str
    label: str | None
    hours: float
    created_at: str


class WeeklySummaryItem(BaseModel):
    week_start: str
    available_minutes: int
    planned_minutes: int
    tentative_minutes: int
    utilization_pct: float


class ForecastMonth(BaseModel):
    month: str
    revenue: float
    hours: float


class ReorderItem(BaseModel):
    id: str
    sort_order: int


# ── Helpers ──────────────────────────────────────────────────────────────────


def _monday_of(d: date) -> date:
    """Gibt den Montag der ISO-Woche zurück."""
    return d - timedelta(days=d.weekday())


def _validate_monday(date_str: str) -> date:
    """Parst ein ISO-Datum und prüft ob es ein Montag ist."""
    d = date.fromisoformat(date_str)
    if d.weekday() != 0:
        raise HTTPException(status_code=422, detail="week_start muss ein Montag sein")
    return d


def _validate_allocation_date(date_str: str, allocation_type: str) -> date:
    """Validiert das Datum je nach Typ: Montag für 'week', Mo-Sa für 'day'."""
    if allocation_type not in ("week", "day"):
        raise HTTPException(status_code=422, detail="allocation_type muss 'week' oder 'day' sein")
    d = date.fromisoformat(date_str)
    if allocation_type == "week":
        if d.weekday() != 0:
            raise HTTPException(status_code=422, detail="week_start muss ein Montag sein (Typ 'week')")
    else:
        if d.weekday() == 6:  # Sonntag
            raise HTTPException(status_code=422, detail="Tagesplanung ist Mo-Sa möglich (kein Sonntag)")
    return d


def _get_toggl_client(user: User) -> TogglClient | None:
    settings = user.settings or {}
    token = settings.get("toggl_api_token") or ""
    ws_id = settings.get("toggl_workspace_id") or 0
    if not token:
        from app.config import get_settings
        app_cfg = get_settings()
        token = app_cfg.toggl_api_token
        ws_id = ws_id or app_cfg.toggl_workspace_id
    if not token:
        return None
    return TogglClient(TogglConfig(api_token=token, workspace_id=int(ws_id or 0)))


# ── Kapazitätsprojekte ───────────────────────────────────────────────────────


@router.get("/projects", response_model=list[CapacityProjectOut])
async def list_capacity_projects(
    status: str | None = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        stmt = select(CapacityProject).order_by(CapacityProject.sort_order, CapacityProject.name)
        if status:
            stmt = stmt.where(CapacityProject.status == status)
        result = await session.execute(stmt)
        projects = result.scalars().all()

        # Gesamtzahl Zuweisungen je Projekt (über alle Zeiträume), damit das
        # Frontend "noch nie geplant = neu" von "keine Kapazität im Fenster"
        # unterscheiden kann.
        count_stmt = select(
            CapacityAllocation.capacity_project_id,
            func.count(CapacityAllocation.id),
        ).group_by(CapacityAllocation.capacity_project_id)
        count_result = await session.execute(count_stmt)
        counts = {row[0]: row[1] for row in count_result.all()}

        return [_project_to_out(p, counts.get(p.id, 0)) for p in projects]


@router.post("/projects", response_model=CapacityProjectOut, status_code=201)
async def create_capacity_project(
    body: CapacityProjectCreate,
    user: User = Depends(require_role("owner")),
):
    if body.toggl_project_id and body.toggl_client_id:
        raise HTTPException(status_code=422, detail="toggl_project_id und toggl_client_id dürfen nicht gleichzeitig gesetzt sein")
    if body.toggl_billable_filter and body.toggl_billable_filter not in ("non_billable", "billable"):
        raise HTTPException(status_code=422, detail="toggl_billable_filter muss 'non_billable', 'billable' oder leer sein")
    async with async_session() as session:
        proj = CapacityProject(
            name=body.name,
            color=body.color,
            icon_url=body.icon_url,
            icon_emoji=body.icon_emoji,
            client_name=body.client_name,
            hourly_rate=body.hourly_rate,
            is_billable=body.is_billable,
            status=body.status,
            project_id=uuid.UUID(body.project_id) if body.project_id else None,
            toggl_project_id=body.toggl_project_id,
            toggl_client_id=body.toggl_client_id,
            toggl_billable_filter=body.toggl_billable_filter,
            pipedrive_deal_id=body.pipedrive_deal_id,
            notes=body.notes,
        )
        session.add(proj)
        await session.commit()
        await session.refresh(proj)
        return _project_to_out(proj)


@router.patch("/projects/{project_id}", response_model=CapacityProjectOut)
async def update_capacity_project(
    project_id: str,
    body: CapacityProjectUpdate,
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        proj = await session.get(CapacityProject, uuid.UUID(project_id))
        if not proj:
            raise HTTPException(status_code=404, detail="Kapazitätsprojekt nicht gefunden")
        data = body.model_dump(exclude_unset=True)
        if "project_id" in data:
            data["project_id"] = uuid.UUID(data["project_id"]) if data["project_id"] else None
        for key, val in data.items():
            setattr(proj, key, val)
        await session.commit()
        await session.refresh(proj)
        return _project_to_out(proj)


@router.delete("/projects/{project_id}", status_code=204)
async def delete_capacity_project(
    project_id: str,
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        proj = await session.get(CapacityProject, uuid.UUID(project_id))
        if not proj:
            raise HTTPException(status_code=404, detail="Kapazitätsprojekt nicht gefunden")
        await session.delete(proj)
        await session.commit()


@router.post("/projects/reorder")
async def reorder_projects(
    items: list[ReorderItem] = Body(...),
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        for item in items:
            await session.execute(
                update(CapacityProject)
                .where(CapacityProject.id == uuid.UUID(item.id))
                .values(sort_order=item.sort_order)
            )
        await session.commit()
    return {"status": "ok"}


# ── Zuweisungen (Allocations) ────────────────────────────────────────────────


@router.get("/allocations", response_model=list[AllocationOut])
async def list_allocations(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
    include_tentative: bool = Query(default=True),
    user: User = Depends(require_role("owner")),
):
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)
    async with async_session() as session:
        stmt = (
            select(CapacityAllocation)
            .join(CapacityProject)
            .where(CapacityAllocation.week_start >= d_from)
            .where(CapacityAllocation.week_start <= d_to)
        )
        if not include_tentative:
            stmt = stmt.where(CapacityProject.status == "bestätigt")
        result = await session.execute(stmt)
        allocs = result.scalars().all()
        return [_alloc_to_out(a) for a in allocs]


@router.post("/allocations", response_model=AllocationOut, status_code=201)
async def create_allocation(
    body: AllocationCreate,
    user: User = Depends(require_role("owner")),
):
    ws = _validate_allocation_date(body.week_start, body.allocation_type)
    try:
        project_uuid = uuid.UUID(body.capacity_project_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Ungültige capacity_project_id")

    # Upsert: Eine bestehende Zuweisung für (Projekt, Datum) wird überschrieben,
    # statt an der UNIQUE-Constraint (uq_cap_alloc_project_week) zu scheitern.
    # So bleibt das (erneute) Buchen eines Einzeltages idempotent.
    stmt = (
        pg_insert(CapacityAllocation)
        .values(
            capacity_project_id=project_uuid,
            week_start=ws,
            minutes=body.minutes,
            allocation_type=body.allocation_type,
            notes=body.notes,
        )
        .on_conflict_do_update(
            index_elements=["capacity_project_id", "week_start"],
            set_={
                "minutes": body.minutes,
                "allocation_type": body.allocation_type,
                "notes": body.notes,
                "updated_at": func.now(),
            },
        )
        .returning(CapacityAllocation.id)
    )
    async with async_session() as session:
        try:
            result = await session.execute(stmt)
            alloc_id = result.scalar_one()
            await session.commit()
        except IntegrityError:
            # z. B. ungültige capacity_project_id (FK-Verletzung)
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="Zuweisung konnte nicht gespeichert werden (ungültiges Projekt oder Konflikt)",
            )
        alloc = await session.get(CapacityAllocation, alloc_id)
        return _alloc_to_out(alloc)


@router.post("/allocations/repeat", response_model=list[AllocationOut], status_code=201)
async def create_repeat_allocations(
    body: AllocationRepeat,
    user: User = Depends(require_role("owner")),
):
    """Erstellt wiederholte Zuweisungen mit gleicher series_id."""
    ws = _validate_allocation_date(body.week_start, body.allocation_type)
    end = date.fromisoformat(body.end_date)
    if end < ws:
        raise HTTPException(status_code=422, detail="Enddatum muss nach Startdatum liegen")

    series_id = uuid.uuid4()
    allocs: list[CapacityAllocation] = []
    current = ws
    interval = timedelta(weeks=body.interval_weeks) if body.allocation_type == "week" else timedelta(weeks=body.interval_weeks)
    while current <= end:
        allocs.append(CapacityAllocation(
            capacity_project_id=uuid.UUID(body.capacity_project_id),
            week_start=current,
            minutes=body.minutes,
            allocation_type=body.allocation_type,
            series_id=series_id,
            notes=body.notes,
        ))
        current += interval

    async with async_session() as session:
        session.add_all(allocs)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            raise HTTPException(
                status_code=409,
                detail="Serie überschneidet sich mit bestehenden Zuweisungen oder verweist auf ein ungültiges Projekt",
            )
        for a in allocs:
            await session.refresh(a)
        return [_alloc_to_out(a) for a in allocs]


@router.patch("/allocations/{alloc_id}", response_model=AllocationOut)
async def update_allocation(
    alloc_id: str,
    body: AllocationUpdate,
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        alloc = await session.get(CapacityAllocation, uuid.UUID(alloc_id))
        if not alloc:
            raise HTTPException(status_code=404, detail="Zuweisung nicht gefunden")
        data = body.model_dump(exclude_unset=True)
        if "week_start" in data and data["week_start"]:
            alloc_type = data.get("allocation_type") or alloc.allocation_type
            data["week_start"] = _validate_allocation_date(data["week_start"], alloc_type)
        if "allocation_type" in data and data["allocation_type"]:
            if data["allocation_type"] not in ("week", "day"):
                raise HTTPException(status_code=422, detail="allocation_type muss 'week' oder 'day' sein")
        for key, val in data.items():
            setattr(alloc, key, val)
        await session.commit()
        await session.refresh(alloc)
        return _alloc_to_out(alloc)


@router.delete("/allocations/{alloc_id}", status_code=204)
async def delete_allocation(
    alloc_id: str,
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        alloc = await session.get(CapacityAllocation, uuid.UUID(alloc_id))
        if not alloc:
            raise HTTPException(status_code=404, detail="Zuweisung nicht gefunden")
        await session.delete(alloc)
        await session.commit()


@router.post("/allocations/bulk")
async def bulk_allocations(
    body: BulkAction,
    user: User = Depends(require_role("owner")),
):
    """Bulk-Operationen auf Zuweisungen (Serie oder ID-Liste)."""
    async with async_session() as session:
        if body.action == "delete":
            if body.series_id:
                await session.execute(
                    delete(CapacityAllocation).where(
                        CapacityAllocation.series_id == uuid.UUID(body.series_id)
                    )
                )
            elif body.ids:
                uuids = [uuid.UUID(i) for i in body.ids]
                await session.execute(
                    delete(CapacityAllocation).where(CapacityAllocation.id.in_(uuids))
                )
            else:
                raise HTTPException(status_code=422, detail="series_id oder ids erforderlich")

        elif body.action == "delete_from":
            if not body.series_id or not body.from_week:
                raise HTTPException(status_code=422, detail="series_id und from_week erforderlich")
            from_date = date.fromisoformat(body.from_week)
            await session.execute(
                delete(CapacityAllocation).where(
                    CapacityAllocation.series_id == uuid.UUID(body.series_id),
                    CapacityAllocation.week_start >= from_date,
                )
            )

        elif body.action == "shift":
            if not body.series_id or body.weeks is None:
                raise HTTPException(status_code=422, detail="series_id und weeks erforderlich")
            delta = timedelta(weeks=body.weeks)
            order = CapacityAllocation.week_start.desc() if body.weeks > 0 else CapacityAllocation.week_start.asc()
            stmt = select(CapacityAllocation).where(
                CapacityAllocation.series_id == uuid.UUID(body.series_id)
            ).order_by(order)
            result = await session.execute(stmt)
            for alloc in result.scalars().all():
                alloc.week_start = alloc.week_start + delta
                await session.flush()

        elif body.action == "update":
            if not body.series_id or body.minutes is None:
                raise HTTPException(status_code=422, detail="series_id und minutes erforderlich")
            stmt = select(CapacityAllocation).where(
                CapacityAllocation.series_id == uuid.UUID(body.series_id)
            )
            result = await session.execute(stmt)
            for alloc in result.scalars().all():
                alloc.minutes = body.minutes
                alloc.updated_at = datetime.now(timezone.utc)

        elif body.action == "update_from":
            if not body.series_id or not body.from_week or body.minutes is None:
                raise HTTPException(status_code=422, detail="series_id, from_week und minutes erforderlich")
            from_date = date.fromisoformat(body.from_week)
            stmt = select(CapacityAllocation).where(
                CapacityAllocation.series_id == uuid.UUID(body.series_id),
                CapacityAllocation.week_start >= from_date,
            )
            result = await session.execute(stmt)
            for alloc in result.scalars().all():
                alloc.minutes = body.minutes
                alloc.updated_at = datetime.now(timezone.utc)

        elif body.action == "clone":
            if not body.ids or not body.target_project_id:
                raise HTTPException(status_code=422, detail="ids und target_project_id erforderlich")
            uuids = [uuid.UUID(i) for i in body.ids]
            stmt = select(CapacityAllocation).where(CapacityAllocation.id.in_(uuids))
            result = await session.execute(stmt)
            new_series = uuid.uuid4()
            for alloc in result.scalars().all():
                clone = CapacityAllocation(
                    capacity_project_id=uuid.UUID(body.target_project_id),
                    week_start=alloc.week_start,
                    minutes=alloc.minutes,
                    allocation_type=alloc.allocation_type,
                    series_id=new_series,
                    notes=alloc.notes,
                )
                session.add(clone)

        else:
            raise HTTPException(status_code=422, detail=f"Unbekannte Aktion: {body.action}")

        await session.commit()
    return {"status": "ok"}


# ── Freie Tage ───────────────────────────────────────────────────────────────


@router.get("/time-off", response_model=list[TimeOffOut])
async def list_time_off(
    year: int = Query(default=None),
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        stmt = select(CapacityTimeOff).order_by(CapacityTimeOff.date)
        if year:
            stmt = stmt.where(
                CapacityTimeOff.date >= date(year, 1, 1),
                CapacityTimeOff.date <= date(year, 12, 31),
            )
        result = await session.execute(stmt)
        items = result.scalars().all()
        return [_timeoff_to_out(t) for t in items]


@router.post("/time-off", response_model=TimeOffOut, status_code=201)
async def create_time_off(
    body: TimeOffCreate,
    user: User = Depends(require_role("owner")),
):
    d = date.fromisoformat(body.date)
    async with async_session() as session:
        existing = await session.execute(
            select(CapacityTimeOff).where(CapacityTimeOff.date == d)
        )
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Für dieses Datum existiert bereits ein Eintrag")
        entry = CapacityTimeOff(date=d, type=body.type, label=body.label, hours=body.hours)
        session.add(entry)
        await session.commit()
        await session.refresh(entry)
        return _timeoff_to_out(entry)


@router.delete("/time-off/{entry_id}", status_code=204)
async def delete_time_off(
    entry_id: str,
    user: User = Depends(require_role("owner")),
):
    async with async_session() as session:
        entry = await session.get(CapacityTimeOff, uuid.UUID(entry_id))
        if not entry:
            raise HTTPException(status_code=404, detail="Eintrag nicht gefunden")
        await session.delete(entry)
        await session.commit()


# ── Aggregation: Wochen-Summary ──────────────────────────────────────────────


@router.get("/weekly-summary", response_model=list[WeeklySummaryItem])
async def get_weekly_summary(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
    include_tentative: bool = Query(default=True),
    user: User = Depends(require_role("owner")),
):
    """Pro Woche: verfügbare Minuten, geplante Minuten, Auslastung in %."""
    d_from = _monday_of(date.fromisoformat(from_date))
    d_to = date.fromisoformat(to_date)

    async with async_session() as session:
        alloc_stmt = (
            select(CapacityAllocation)
            .join(CapacityProject)
            .where(CapacityAllocation.week_start >= d_from)
            .where(CapacityAllocation.week_start <= d_to)
        )
        if not include_tentative:
            alloc_stmt = alloc_stmt.where(CapacityProject.status == "bestätigt")
        alloc_result = await session.execute(alloc_stmt)
        allocs = alloc_result.scalars().all()

        # Tentative separat laden für die Aufschlüsselung
        tent_stmt = (
            select(CapacityAllocation)
            .join(CapacityProject)
            .where(CapacityAllocation.week_start >= d_from)
            .where(CapacityAllocation.week_start <= d_to)
            .where(CapacityProject.status == "vorläufig")
        )
        tent_result = await session.execute(tent_stmt)
        tent_allocs = tent_result.scalars().all()

        timeoff_stmt = select(CapacityTimeOff).where(
            CapacityTimeOff.date >= d_from,
            CapacityTimeOff.date <= d_to,
        )
        timeoff_result = await session.execute(timeoff_stmt)
        time_offs = timeoff_result.scalars().all()

    # Gruppieren (Tages-Allocations werden ihrer ISO-Woche zugeordnet)
    planned_by_week: dict[date, int] = defaultdict(int)
    tentative_by_week: dict[date, int] = defaultdict(int)
    timeoff_by_week: dict[date, float] = defaultdict(float)

    for a in allocs:
        week_key = a.week_start if a.allocation_type == "week" else _monday_of(a.week_start)
        planned_by_week[week_key] += a.minutes
    for a in tent_allocs:
        week_key = a.week_start if a.allocation_type == "week" else _monday_of(a.week_start)
        tentative_by_week[week_key] += a.minutes
    for t in time_offs:
        week = _monday_of(t.date)
        timeoff_by_week[week] += t.hours * 60

    # 40h/Woche = 2400 Minuten
    base_weekly_minutes = 2400
    result: list[WeeklySummaryItem] = []
    current = d_from
    while current <= d_to:
        off_minutes = timeoff_by_week.get(current, 0)
        available = max(0, base_weekly_minutes - int(off_minutes))
        planned = planned_by_week.get(current, 0)
        tentative = tentative_by_week.get(current, 0)
        util = (planned / available * 100) if available > 0 else 0
        result.append(WeeklySummaryItem(
            week_start=current.isoformat(),
            available_minutes=available,
            planned_minutes=planned,
            tentative_minutes=tentative,
            utilization_pct=round(util, 1),
        ))
        current += timedelta(weeks=1)

    return result


# ── Aggregation: Forecast Revenue ────────────────────────────────────────────


def _workdays_in_range(start: date, end: date) -> list[date]:
    """Arbeitstage (Mo–Fr) von ``start`` bis ``end`` inklusive."""
    return [
        d
        for d in (start + timedelta(days=i) for i in range((end - start).days + 1))
        if d.weekday() < 5
    ]


@router.get("/forecast-revenue", response_model=list[ForecastMonth])
async def get_forecast_revenue(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
    user: User = Depends(require_role("owner")),
):
    """Monatliche Umsatzprognose basierend auf geplanter Kapazität x Stundensatz.

    Zwei Eigenheiten, die die Prognose früher um mehr als eine Grössenordnung
    verfälscht haben:

    - **Stundensatz:** Projekte ohne eigenen ``hourly_rate`` wurden komplett
      ignoriert. Da in der Praxis nur einzelne Projekte einen individuellen Satz
      tragen, blieb fast die gesamte verrechenbare Kapazität unsichtbar. Jetzt
      greift der ``default_hourly_rate`` aus den Owner-Settings.
    - **Monatszuordnung:** Eine Wochen-Allokation wurde vollständig dem Monat
      ihres Montags zugeschlagen. Die Woche ab 27.07. landete damit ganz im
      Juli, obwohl vier von fünf Arbeitstagen im August liegen. Jetzt werden die
      Minuten arbeitstagweise auf die berührten Monate verteilt, und Wochen, die
      in das Fenster hineinreichen, werden mitgezählt.
    """
    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    fs = get_forecast_settings_from_settings(getattr(user, "settings", None))

    async with async_session() as session:
        stmt = (
            select(CapacityAllocation, CapacityProject)
            .join(CapacityProject)
            # Eine Woche, die vor ``d_from`` beginnt, kann in das Fenster
            # hineinreichen -- sonst fehlen die ersten Tage des Zeitraums.
            .where(CapacityAllocation.week_start >= d_from - timedelta(days=6))
            .where(CapacityAllocation.week_start <= d_to)
            .where(CapacityProject.status == "bestätigt")
            .where(CapacityProject.is_billable == True)  # noqa: E712
        )
        result = await session.execute(stmt)
        rows = result.all()

    monthly: dict[str, dict] = defaultdict(lambda: {"revenue": 0.0, "hours": 0.0})
    for alloc, proj in rows:
        rate = float(proj.hourly_rate) if proj.hourly_rate is not None else fs.default_hourly_rate
        if alloc.allocation_type == "week":
            span_start = alloc.week_start
            span_end = alloc.week_start + timedelta(days=6)
        else:
            span_start = span_end = alloc.week_start

        workdays = _workdays_in_range(span_start, span_end) or [span_start]
        in_window = [d for d in workdays if d_from <= d <= d_to]
        if not in_window:
            continue

        # Minuten gleichmässig auf die Arbeitstage der Allokation verteilen und
        # nur die Tage innerhalb des Fensters zählen.
        minutes_per_day = alloc.minutes / len(workdays)
        for day in in_window:
            month_key = day.strftime("%Y-%m")
            hours = minutes_per_day / 60
            monthly[month_key]["hours"] += hours
            monthly[month_key]["revenue"] += hours * rate

    return [
        ForecastMonth(month=k, revenue=round(v["revenue"], 2), hours=round(v["hours"], 2))
        for k, v in sorted(monthly.items())
    ]


# ── Plan vs. Ist (Toggl) ────────────────────────────────────────────────────


@router.get("/plan-vs-actual")
async def get_plan_vs_actual(
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
    user: User = Depends(require_role("owner")),
):
    """Vergleich geplante Kapazität vs. effektive Toggl-Stunden (24h-Cache).

    Unterstützt zwei Verknüpfungsarten:
    1. Direkt: toggl_project_id → 1:1-Vergleich
    2. Client-Aggregation: toggl_client_id + toggl_billable_filter → N Projekte aggregiert
    """
    from sqlalchemy import or_

    d_from = date.fromisoformat(from_date)
    d_to = date.fromisoformat(to_date)

    async with async_session() as session:
        # Alle Capacity-Projekte mit irgendeiner Toggl-Verknüpfung laden
        proj_stmt = select(CapacityProject).where(
            or_(
                CapacityProject.toggl_project_id.isnot(None),
                CapacityProject.toggl_client_id.isnot(None),
            )
        )
        proj_result = await session.execute(proj_stmt)
        toggl_projects = proj_result.scalars().all()

        # Allocations im Zeitraum laden (für alle Toggl-verknüpften Projekte)
        cap_ids = [p.id for p in toggl_projects]
        alloc_rows = []
        if cap_ids:
            stmt = (
                select(CapacityAllocation, CapacityProject)
                .join(CapacityProject)
                .where(CapacityAllocation.week_start >= d_from)
                .where(CapacityAllocation.week_start <= d_to)
                .where(CapacityAllocation.capacity_project_id.in_(cap_ids))
            )
            result = await session.execute(stmt)
            alloc_rows = result.all()

    # Projekte aufteilen: direkt vs. Client-Aggregation
    direct_projects = [p for p in toggl_projects if p.toggl_project_id]
    agg_projects = [p for p in toggl_projects if p.toggl_client_id and not p.toggl_project_id]

    # Plan-Daten für direkte Projekte (Key: toggl_project_id)
    plan_by_project_week: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    project_names: dict[int, str] = {}
    all_toggl_project_ids: list[int] = []

    for proj in direct_projects:
        project_names[proj.toggl_project_id] = proj.name
        all_toggl_project_ids.append(proj.toggl_project_id)

    for alloc, proj in alloc_rows:
        if proj.toggl_project_id:
            plan_by_project_week[proj.toggl_project_id][alloc.week_start.isoformat()] += alloc.minutes

    # Plan-Daten für aggregierte Projekte (Key: capacity_project_id)
    plan_by_cap_week: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for alloc, proj in alloc_rows:
        if proj.toggl_client_id and not proj.toggl_project_id:
            plan_by_cap_week[str(proj.id)][alloc.week_start.isoformat()] += alloc.minutes

    # Client-basierte Projekte: zugehörige Toggl-Projekt-IDs ermitteln
    agg_toggl_ids: list[int] = []
    agg_cap_mapping: dict[str, list[int]] = {}  # cap_id → [toggl_project_ids]

    if agg_projects:
        client = _get_toggl_client(user)
        if client:
            cache_key_tp = "toggl_projects_list"
            tp_list = _toggl_cache.get(cache_key_tp)
            if tp_list is None:
                try:
                    raw = await client.list_projects(active=None)
                    tp_list = [
                        {
                            "id": p.get("id"),
                            "client_id": p.get("client_id") or p.get("cid"),
                            "billable": p.get("billable", False),
                        }
                        for p in raw
                        if p.get("id")
                    ]
                except Exception as e:
                    logger.warning("Toggl-Projekte für Aggregation: %s", e)
                    tp_list = []

            for cap_proj in agg_projects:
                matching_ids = []
                for tp in tp_list:
                    if tp.get("client_id") != cap_proj.toggl_client_id:
                        continue
                    bf = cap_proj.toggl_billable_filter
                    if bf == "non_billable" and tp.get("billable"):
                        continue
                    if bf == "billable" and not tp.get("billable"):
                        continue
                    matching_ids.append(tp.get("id"))
                agg_cap_mapping[str(cap_proj.id)] = matching_ids
                agg_toggl_ids.extend(matching_ids)

    all_query_ids = list(set(all_toggl_project_ids + agg_toggl_ids))

    if not all_query_ids and not agg_projects:
        return {"projects": [], "toggl_data_date": None}

    # Toggl-Zeiteinträge (24h-Cache)
    cache_key = f"cap_toggl:{from_date}:{to_date}"
    toggl_data = _toggl_cache.get(cache_key)

    if toggl_data is None and all_query_ids:
        client = _get_toggl_client(user)
        if client:
            try:
                entries = await client.search_time_entries(
                    workspace_id=None,
                    start_date=from_date,
                    end_date=to_date,
                    project_ids=all_query_ids,
                )
                toggl_data = _aggregate_toggl_weekly(entries, d_from, d_to)
                _toggl_cache[cache_key] = toggl_data
            except Exception as e:
                logger.warning("Toggl-Abfrage fehlgeschlagen: %s", e)
                toggl_data = {}
        else:
            toggl_data = {}
    elif toggl_data is None:
        toggl_data = {}

    # Ergebnis: Direkte Projekte (wie bisher)
    projects_result = []
    all_toggl_ids_set = set(plan_by_project_week.keys())
    if toggl_data:
        for tid in toggl_data:
            if tid in {p.toggl_project_id for p in direct_projects}:
                all_toggl_ids_set.add(tid)

    for toggl_id in all_toggl_ids_set:
        planned_weeks = plan_by_project_week.get(toggl_id, {})
        actual_weeks = toggl_data.get(toggl_id, {}) if toggl_data else {}
        all_week_strs = sorted(set(list(planned_weeks.keys()) + list(actual_weeks.keys())))

        project_entry = {
            "toggl_project_id": toggl_id,
            "name": project_names.get(toggl_id, ""),
            "weeks": [],
        }
        for week_str in all_week_strs:
            project_entry["weeks"].append({
                "week_start": week_str,
                "planned_minutes": planned_weeks.get(week_str, 0),
                "actual_minutes": actual_weeks.get(week_str, 0),
            })
        projects_result.append(project_entry)

    # Ergebnis: Aggregierte Projekte (Client-basiert)
    for cap_proj in agg_projects:
        cap_id = str(cap_proj.id)
        matched_toggl_ids = agg_cap_mapping.get(cap_id, [])
        agg_actual: dict[str, int] = defaultdict(int)
        if toggl_data:
            for tid in matched_toggl_ids:
                for week_str, mins in toggl_data.get(tid, {}).items():
                    agg_actual[week_str] += mins

        planned_weeks = plan_by_cap_week.get(cap_id, {})
        all_week_strs = sorted(set(list(planned_weeks.keys()) + list(agg_actual.keys())))

        project_entry = {
            "toggl_project_id": None,
            "capacity_project_id": cap_id,
            "name": cap_proj.name,
            "weeks": [],
        }
        for week_str in all_week_strs:
            project_entry["weeks"].append({
                "week_start": week_str,
                "planned_minutes": planned_weeks.get(week_str, 0),
                "actual_minutes": agg_actual.get(week_str, 0),
            })
        projects_result.append(project_entry)

    return {
        "projects": projects_result,
        "toggl_data_date": date.today().isoformat() if toggl_data else None,
    }


@router.post("/refresh-toggl")
async def refresh_toggl_cache(user: User = Depends(require_role("owner"))):
    """Leert den Toggl-Cache für die Kapazitätsplanung."""
    _toggl_cache.clear()
    return {"status": "ok", "message": "Toggl-Cache geleert"}


@router.get("/monthly-actual")
async def get_monthly_actual(
    month: str | None = Query(default=None, description="YYYY-MM, default aktueller Monat"),
    prev_month: bool = Query(default=True, description="Vormonat mitliefern"),
    user: User = Depends(require_role("owner")),
):
    """Monatlicher Soll/Ist-Vergleich pro Kapazitätsprojekt (für Soll/Ist-Spalte)."""
    from calendar import monthrange

    from sqlalchemy import or_

    if month:
        parts = month.split("-")
        year, mon = int(parts[0]), int(parts[1])
    else:
        today = date.today()
        year, mon = today.year, today.month

    month_start = date(year, mon, 1)
    month_end = date(year, mon, monthrange(year, mon)[1])

    prev_year, prev_mon = (year - 1, 12) if mon == 1 else (year, mon - 1)
    prev_start = date(prev_year, prev_mon, 1)
    prev_end = date(prev_year, prev_mon, monthrange(prev_year, prev_mon)[1])

    async with async_session() as session:
        proj_stmt = select(CapacityProject).where(
            or_(
                CapacityProject.toggl_project_id.isnot(None),
                CapacityProject.toggl_client_id.isnot(None),
            )
        )
        cap_projects = (await session.execute(proj_stmt)).scalars().all()
        if not cap_projects:
            return {"month": f"{year:04d}-{mon:02d}", "projects": []}

        cap_ids = [p.id for p in cap_projects]
        query_from = _monday_of(prev_start if prev_month else month_start)
        stmt = (
            select(CapacityAllocation, CapacityProject)
            .join(CapacityProject)
            .where(CapacityAllocation.week_start >= query_from)
            .where(CapacityAllocation.week_start <= month_end)
            .where(CapacityAllocation.capacity_project_id.in_(cap_ids))
        )
        alloc_rows = (await session.execute(stmt)).all()

    # Monatsnormierung liegt in src/capacity/capacity_report.py, damit Cockpit und
    # Agent (mcp-capacity) garantiert dieselben Zahlen nennen.
    allocations = [
        Allocation(
            project_id=str(proj.id),
            week_start=alloc.week_start,
            minutes=alloc.minutes,
            allocation_type=alloc.allocation_type,
        )
        for alloc, proj in alloc_rows
    ]
    planned_cur = planned_minutes(allocations, month_start, month_end)
    planned_prev_map = (
        planned_minutes(allocations, prev_start, prev_end) if prev_month else {}
    )

    # Toggl-Projekt-IDs sammeln (direkt + aggregiert)
    direct = [p for p in cap_projects if p.toggl_project_id]
    agg = [p for p in cap_projects if p.toggl_client_id and not p.toggl_project_id]
    all_toggl_ids: list[int] = [p.toggl_project_id for p in direct]
    agg_map: dict[str, list[int]] = {}

    if agg:
        client = _get_toggl_client(user)
        if client:
            tp_list = _toggl_cache.get("toggl_projects_list")
            if tp_list is None:
                try:
                    raw = await client.list_projects(active=None)
                    tp_list = [
                        {"id": p.get("id"), "client_id": p.get("client_id") or p.get("cid"), "billable": p.get("billable", False)}
                        for p in raw if p.get("id")
                    ]
                except Exception as e:
                    logger.warning("Toggl-Projekte: %s", e)
                    tp_list = []
            for cp in agg:
                ids = [
                    tp.get("id") for tp in tp_list
                    if tp.get("client_id") == cp.toggl_client_id
                    and (not cp.toggl_billable_filter
                         or (cp.toggl_billable_filter == "non_billable" and not tp.get("billable"))
                         or (cp.toggl_billable_filter == "billable" and tp.get("billable")))
                ]
                agg_map[str(cp.id)] = ids
                all_toggl_ids.extend(ids)

    all_toggl_ids = list(set(all_toggl_ids))

    async def _fetch_monthly(m_start: date, m_end: date, label: str) -> dict[int, int]:
        """Ist-Minuten pro Toggl-Projekt via Summary API (wie Debitorenansicht)."""
        cache_key = f"cap_monthly:{label}"
        cached = _toggl_cache.get(cache_key)
        if cached is not None:
            return cached
        if not all_toggl_ids:
            return {}
        cl = _get_toggl_client(user)
        if not cl:
            return {}
        try:
            summary = await cl.get_summary_by_project(
                start_date=m_start.isoformat(),
                end_date=m_end.isoformat(),
                billable=None,
            )
            result = actual_minutes_by_toggl_project(summary, set(all_toggl_ids))
            _toggl_cache[cache_key] = result
            return result
        except Exception as e:
            logger.warning("Toggl monthly fetch: %s", e)
            return {}

    actual_cur = await _fetch_monthly(month_start, month_end, f"{year:04d}-{mon:02d}")
    actual_prev_data: dict[int, int] = {}
    if prev_month:
        actual_prev_data = await _fetch_monthly(prev_start, prev_end, f"{prev_year:04d}-{prev_mon:02d}")

    projects_out = []
    for cp in cap_projects:
        cid = str(cp.id)
        if cp.toggl_project_id:
            cur_act = actual_cur.get(cp.toggl_project_id, 0)
            prev_act = actual_prev_data.get(cp.toggl_project_id, 0)
        elif cp.toggl_client_id:
            matched = agg_map.get(cid, [])
            cur_act = sum(actual_cur.get(t, 0) for t in matched)
            prev_act = sum(actual_prev_data.get(t, 0) for t in matched)
        else:
            continue

        entry: dict = {
            "capacity_project_id": cid,
            "name": cp.name,
            "planned_minutes": planned_cur.get(cid, 0),
            "actual_minutes": cur_act,
        }
        if prev_month:
            entry["prev_month_planned"] = planned_prev_map.get(cid, 0)
            entry["prev_month_actual"] = prev_act
        projects_out.append(entry)

    return {"month": f"{year:04d}-{mon:02d}", "projects": projects_out}


@router.get("/toggl-projects")
async def list_toggl_projects(user: User = Depends(require_role("owner"))):
    """Toggl-Projekte auflisten (24h-Cache) für Import in die Kapazitätsplanung."""
    cache_key = "toggl_projects_ui"
    cached = _toggl_cache.get(cache_key)
    if cached is not None:
        return cached

    client = _get_toggl_client(user)
    if not client:
        return []

    try:
        projects = await client.list_projects(active=True)
        result = [
            {
                "id": p.get("id"),
                "name": p.get("name", ""),
                "client": p.get("client_name") or p.get("client") or "",
                "billable": p.get("billable", False),
                "color": p.get("color", "#3B82F6"),
            }
            for p in projects
            if p.get("id") and p.get("name")
        ]
        _toggl_cache[cache_key] = result
        return result
    except Exception as e:
        logger.warning("Toggl-Projekte laden fehlgeschlagen: %s", e)
        return []


@router.get("/toggl-clients")
async def list_toggl_clients(user: User = Depends(require_role("owner"))):
    """Toggl-Clients auflisten (24h-Cache) für Aggregations-Verknüpfung."""
    cache_key = "toggl_clients_list"
    cached = _toggl_cache.get(cache_key)
    if cached is not None:
        return cached

    client = _get_toggl_client(user)
    if not client:
        return []

    try:
        clients = await client.list_clients()
        result = [
            {"id": c.get("id"), "name": c.get("name", "")}
            for c in clients
            if c.get("id") and c.get("name")
        ]
        _toggl_cache[cache_key] = result
        return result
    except Exception as e:
        logger.warning("Toggl-Clients laden fehlgeschlagen: %s", e)
        return []


class AvailableProject(BaseModel):
    name: str
    source: str  # "both" | "toggl" | "taskpilot"
    toggl_project_id: int | None = None
    project_id: str | None = None
    icon_url: str | None = None
    icon_emoji: str | None = None
    color: str = "#3B82F6"
    client_name: str | None = None
    billable: bool = True


@router.get("/available-projects", response_model=list[AvailableProject])
async def list_available_projects(user: User = Depends(require_role("owner"))):
    """Zusammengeführte Liste aus Toggl- und TaskPilot-Projekten (Name-basierter Abgleich)."""
    # TaskPilot-Projekte laden
    tp_projects: dict[str, dict] = {}
    async with async_session() as session:
        stmt = select(Project).where(Project.status != "archived")
        result = await session.execute(stmt)
        for p in result.scalars().all():
            tp_projects[p.name.strip().lower()] = {
                "id": str(p.id),
                "name": p.name,
                "icon_url": p.icon_url,
                "icon_emoji": p.icon_emoji,
                "color": p.color,
            }

    # Toggl-Projekte laden (aus Cache oder API)
    cache_key = "toggl_projects_list"
    toggl_list = _toggl_cache.get(cache_key)
    if toggl_list is None:
        client = _get_toggl_client(user)
        if client:
            try:
                raw = await client.list_projects(active=True)
                toggl_list = [
                    {
                        "id": p.get("id"),
                        "name": p.get("name", ""),
                        "client": p.get("client_name") or p.get("client") or "",
                        "billable": p.get("billable", False),
                        "color": p.get("color", "#3B82F6"),
                    }
                    for p in raw
                    if p.get("id") and p.get("name")
                ]
                _toggl_cache[cache_key] = toggl_list
            except Exception as e:
                logger.warning("Toggl-Projekte für available-projects: %s", e)
                toggl_list = []
        else:
            toggl_list = []

    # Name-basierter Abgleich
    merged: dict[str, AvailableProject] = {}

    for tp_key, tp in tp_projects.items():
        merged[tp_key] = AvailableProject(
            name=tp["name"],
            source="taskpilot",
            project_id=tp["id"],
            icon_url=tp["icon_url"],
            icon_emoji=tp["icon_emoji"],
            color=tp["color"],
            client_name=None,
            billable=True,
        )

    for tg in toggl_list:
        key = tg["name"].strip().lower()
        if key in merged:
            # Match: ergänze Toggl-Daten
            existing = merged[key]
            existing.source = "both"
            existing.toggl_project_id = tg["id"]
            existing.client_name = tg["client"] or existing.client_name
            existing.billable = tg["billable"]
        else:
            merged[key] = AvailableProject(
                name=tg["name"],
                source="toggl",
                toggl_project_id=tg["id"],
                icon_url=None,
                icon_emoji=None,
                color=tg["color"],
                client_name=tg["client"] or None,
                billable=tg["billable"],
            )

    return sorted(merged.values(), key=lambda p: p.name.lower())


# ── Hilfsfunktionen ──────────────────────────────────────────────────────────


def _aggregate_toggl_weekly(
    entries: list[dict], d_from: date, d_to: date
) -> dict[int, dict[str, int]]:
    """Aggregiert Toggl-Zeiteinträge pro Projekt pro ISO-Woche (Minuten).

    Unterstützt zwei Formate:
    - Flaches Format: entry hat 'seconds' und 'start' direkt
    - Search-Format: entry hat 'time_entries' als Sub-Array mit 'seconds'/'start'
    """
    result: dict[int, dict[str, int]] = defaultdict(lambda: defaultdict(int))

    for entry in entries:
        project_id = entry.get("project_id")
        if not project_id:
            continue

        time_entries = entry.get("time_entries")
        if time_entries and isinstance(time_entries, list):
            for te in time_entries:
                seconds = te.get("seconds") or te.get("duration") or 0
                if seconds <= 0:
                    continue
                start_str = te.get("start") or te.get("at") or ""
                if not start_str:
                    continue
                try:
                    entry_date = date.fromisoformat(start_str[:10])
                except (ValueError, TypeError):
                    continue
                week_monday = _monday_of(entry_date)
                result[project_id][week_monday.isoformat()] += int(seconds / 60)
        else:
            seconds = entry.get("seconds") or entry.get("dur") or entry.get("duration") or 0
            if seconds <= 0:
                continue
            start_str = entry.get("start") or entry.get("at") or ""
            if not start_str:
                continue
            try:
                entry_date = date.fromisoformat(start_str[:10])
            except (ValueError, TypeError):
                continue
            week_monday = _monday_of(entry_date)
            result[project_id][week_monday.isoformat()] += int(seconds / 60)

    return dict(result)


def _project_to_out(p: CapacityProject, alloc_count: int = 0) -> CapacityProjectOut:
    return CapacityProjectOut(
        id=str(p.id),
        name=p.name,
        color=p.color,
        icon_url=p.icon_url,
        icon_emoji=p.icon_emoji,
        client_name=p.client_name,
        hourly_rate=float(p.hourly_rate) if p.hourly_rate else None,
        is_billable=p.is_billable,
        status=p.status,
        project_id=str(p.project_id) if p.project_id else None,
        toggl_project_id=p.toggl_project_id,
        toggl_client_id=p.toggl_client_id,
        toggl_billable_filter=p.toggl_billable_filter,
        pipedrive_deal_id=p.pipedrive_deal_id,
        sort_order=p.sort_order,
        notes=p.notes,
        created_at=p.created_at.isoformat() if p.created_at else "",
        updated_at=p.updated_at.isoformat() if p.updated_at else "",
        alloc_count=alloc_count,
    )


def _alloc_to_out(a: CapacityAllocation) -> AllocationOut:
    return AllocationOut(
        id=str(a.id),
        capacity_project_id=str(a.capacity_project_id),
        week_start=a.week_start.isoformat(),
        minutes=a.minutes,
        allocation_type=a.allocation_type,
        series_id=str(a.series_id) if a.series_id else None,
        notes=a.notes,
        created_at=a.created_at.isoformat() if a.created_at else "",
        updated_at=a.updated_at.isoformat() if a.updated_at else "",
    )


def _timeoff_to_out(t: CapacityTimeOff) -> TimeOffOut:
    return TimeOffOut(
        id=str(t.id),
        date=t.date.isoformat(),
        type=t.type,
        label=t.label,
        hours=t.hours,
        created_at=t.created_at.isoformat() if t.created_at else "",
    )

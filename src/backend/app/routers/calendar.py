"""FastAPI-Router für Kalender-Zugriff via Microsoft Graph API."""

import calendar as cal_mod
import logging
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from app.auth.deps import get_current_user
from app.config import get_settings
from app.models import User
from app.services.graph_cache import cached, invalidate

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logger = logging.getLogger("taskpilot.calendar")
router = APIRouter(prefix="/api/calendar", tags=["calendar"])

_graph_client: GraphClient | None = None


def _get_graph_client() -> GraphClient:
    global _graph_client
    if _graph_client is None:
        s = get_settings()
        config = GraphConfig(
            tenant_id=s.graph_tenant_id,
            client_id=s.graph_client_id,
            client_secret=s.graph_client_secret,
            user_email=s.graph_user_email,
        )
        _graph_client = GraphClient(config)
    return _graph_client


def _require_owner(user: User) -> None:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner dürfen auf den Kalender zugreifen")


def _check_configured() -> None:
    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        raise HTTPException(
            status_code=503,
            detail="Graph API nicht konfiguriert. Setze TP_GRAPH_* in der Umgebung.",
        )


# ── Schemas ──────────────────────────────────────────────────

class CalendarEvent(BaseModel):
    id: str
    subject: str | None
    start: str | None
    end: str | None
    is_all_day: bool = False
    location: str | None = None
    show_as: str | None = None
    body_preview: str | None = None
    organizer: str | None = None
    categories: list[str] = []
    sensitivity: str | None = None
    attendees_count: int = 0
    is_organizer: bool = False


class EventCreateRequest(BaseModel):
    subject: str
    start: str
    end: str
    body: str | None = None
    location: str | None = None
    show_as: str = "busy"


class FreeSlot(BaseModel):
    start: str
    end: str
    duration_minutes: int


# ── Endpoints ────────────────────────────────────────────────

@router.get("/events", response_model=list[CalendarEvent])
async def list_events(
    start: str = Query(..., description="Start ISO 8601"),
    end: str = Query(..., description="End ISO 8601"),
    top: int = Query(50, ge=1, le=100),
    exclude_categories: str | None = Query(None, description="Kommagetrennte Kategorien zum Ausblenden"),
    hide_private: bool = Query(True, description="Termine mit sensitivity=private ausblenden"),
    hide_free: bool = Query(True, description="Termine mit showAs=free ausblenden"),
    user: User = Depends(get_current_user),
) -> list[CalendarEvent]:
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        events = await cached(
            f"events:{start}:{end}:{top}",
            lambda: client.list_events(start, end, top),
        )
    except PermissionError as e:
        logger.warning("Kalender list_events: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf den Kalender verweigert")

    excluded = set()
    if exclude_categories:
        excluded = {c.strip().lower() for c in exclude_categories.split(",") if c.strip()}

    result = []
    for ev in events:
        if ev.get("isCancelled", False):
            continue
        if hide_free and ev.get("showAs") == "free":
            continue
        if hide_private and ev.get("sensitivity") == "private":
            continue

        cats = ev.get("categories", []) or []
        cats_lower = {c.lower() for c in cats}
        if excluded and cats_lower & excluded:
            continue

        org = ev.get("organizer", {}).get("emailAddress", {})
        attendees = ev.get("attendees", []) or []
        result.append(CalendarEvent(
            id=ev.get("id", ""),
            subject=ev.get("subject"),
            start=ev.get("start", {}).get("dateTime"),
            end=ev.get("end", {}).get("dateTime"),
            is_all_day=ev.get("isAllDay", False),
            location=(ev.get("location") or {}).get("displayName"),
            show_as=ev.get("showAs"),
            body_preview=ev.get("bodyPreview", "")[:200],
            organizer=org.get("name") or org.get("address"),
            categories=cats,
            sensitivity=ev.get("sensitivity"),
            attendees_count=len(attendees),
            is_organizer=ev.get("isOrganizer", False),
        ))
    return result


@router.post("/events", response_model=CalendarEvent, status_code=201)
async def create_event(
    body: EventCreateRequest,
    user: User = Depends(get_current_user),
) -> CalendarEvent:
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        ev = await client.create_event(
            subject=body.subject,
            start=body.start,
            end=body.end,
            body=body.body,
            location=body.location,
            show_as=body.show_as,
        )
    except PermissionError as e:
        logger.warning("Kalender create_event: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf den Kalender verweigert")
    invalidate("events:")
    invalidate("capacity:")
    return CalendarEvent(
        id=ev.get("id", ""),
        subject=ev.get("subject"),
        start=ev.get("start", {}).get("dateTime"),
        end=ev.get("end", {}).get("dateTime"),
        is_all_day=ev.get("isAllDay", False),
        show_as=ev.get("showAs"),
    )


@router.get("/free-slots", response_model=list[FreeSlot])
async def find_free_slots(
    start: str = Query(...),
    end: str = Query(...),
    duration: int = Query(60, ge=15, le=480, description="Gewünschte Dauer in Minuten"),
    user: User = Depends(get_current_user),
) -> list[FreeSlot]:
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        slots = await client.find_free_slots(start, end, duration)
    except PermissionError as e:
        logger.warning("Kalender find_free_slots: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf den Kalender verweigert")
    return [FreeSlot(**s) for s in slots]


@router.delete("/events/{event_id}", status_code=204)
async def delete_event(
    event_id: str,
    user: User = Depends(get_current_user),
) -> None:
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        await client.delete_event(event_id)
    except PermissionError as e:
        logger.warning("Kalender delete_event: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf den Kalender verweigert")
    invalidate("events:")
    invalidate("capacity:")


# ── Capacity ──────────────────────────────────────────────────

_TZ = ZoneInfo("Europe/Zurich")
_WORK_START = 8
_WORK_END = 18
_HOURS_PER_DAY = 8.0


class CapacityPeriod(BaseModel):
    total_hours: float
    booked_hours: float
    meeting_hours: float
    blocker_hours: float
    free_hours: float
    work_days: float


class CapacityResponse(BaseModel):
    week: CapacityPeriod
    month: CapacityPeriod
    generated_at: datetime


def _count_workdays(start_date, end_date) -> int:
    """Zählt Werktage (Mo-Fr) von start_date bis einschliesslich end_date."""
    count = 0
    d = start_date
    while d <= end_date:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return count


def _parse_graph_datetime(dt_str: str) -> datetime:
    """Parst Graph API Datetime-Strings robust (truncate nanoseconds)."""
    if '.' in dt_str:
        base, frac = dt_str.split('.', 1)
        frac_clean = frac[:6]
        dt_str = f"{base}.{frac_clean}"
    return datetime.fromisoformat(dt_str)


def _event_booked_minutes(ev: dict, period_start: datetime, period_end: datetime) -> float:
    """Berechnet wie viele Minuten ein Event innerhalb der Arbeitszeit (Mo-Fr 08-18) belegt."""
    is_all_day = ev.get("isAllDay", False)
    start_str = ev.get("start", {}).get("dateTime")
    end_str = ev.get("end", {}).get("dateTime")

    if not start_str or not end_str:
        return 0.0

    ev_start = _parse_graph_datetime(start_str).replace(tzinfo=_TZ)
    ev_end = _parse_graph_datetime(end_str).replace(tzinfo=_TZ)

    if is_all_day:
        days = 0
        d = ev_start.date()
        while d < ev_end.date():
            if d.weekday() < 5 and period_start.date() <= d <= period_end.date():
                days += 1
            d += timedelta(days=1)
        return days * _HOURS_PER_DAY * 60

    total_min = 0.0
    current = max(ev_start, period_start)
    end = min(ev_end, period_end)

    while current < end:
        if current.weekday() < 5:
            day_work_start = current.replace(hour=_WORK_START, minute=0, second=0, microsecond=0)
            day_work_end = current.replace(hour=_WORK_END, minute=0, second=0, microsecond=0)

            overlap_start = max(current, day_work_start)
            overlap_end = min(end, day_work_end)

            if overlap_start < overlap_end:
                total_min += (overlap_end - overlap_start).total_seconds() / 60

        current = (current + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )

    return total_min


def _calc_period(now: datetime, period_end_date, events: list[dict]) -> CapacityPeriod:
    """Berechnet Kapazität für eine Periode (Woche oder Monat)."""
    today = now.date()
    is_workday_today = today.weekday() < 5
    current_hour = now.hour + now.minute / 60

    today_remaining = 0.0
    today_fraction = 0.0
    if is_workday_today and current_hour < _WORK_END:
        remaining_window = max(0.0, _WORK_END - max(_WORK_START, current_hour))
        today_remaining = remaining_window * (_HOURS_PER_DAY / (_WORK_END - _WORK_START))
        today_fraction = remaining_window / (_WORK_END - _WORK_START)

    tomorrow = today + timedelta(days=1)
    future_workdays = _count_workdays(tomorrow, period_end_date)
    total_hours = today_remaining + future_workdays * _HOURS_PER_DAY
    work_days = future_workdays + today_fraction

    period_start = now
    period_end = datetime.combine(period_end_date, datetime.max.time()).replace(tzinfo=_TZ)

    booked_min = 0.0
    meeting_min = 0.0
    for ev in events:
        if ev.get("isCancelled", False):
            continue
        if ev.get("showAs") == "free":
            continue
        try:
            minutes = _event_booked_minutes(ev, period_start, period_end)
            booked_min += minutes
            attendees = ev.get("attendees", []) or []
            if len(attendees) > 1:
                meeting_min += minutes
        except Exception:
            logger.warning("Event konnte nicht verarbeitet werden: %s", ev.get("subject", "?"))
            continue

    blocker_min = booked_min - meeting_min
    booked_hours = booked_min / 60
    free_hours = max(0.0, total_hours - booked_hours)

    return CapacityPeriod(
        total_hours=round(total_hours, 1),
        booked_hours=round(booked_hours, 1),
        meeting_hours=round(meeting_min / 60, 1),
        blocker_hours=round(blocker_min / 60, 1),
        free_hours=round(free_hours, 1),
        work_days=round(work_days, 1),
    )


@router.get("/capacity", response_model=CapacityResponse)
async def get_capacity(
    user: User = Depends(get_current_user),
) -> CapacityResponse:
    """Serverseitige Kapazitätsberechnung (Mo-Fr, 8h/Tag, Europe/Zurich)."""
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()

    now = datetime.now(_TZ)
    today = now.date()

    week_day = today.weekday()
    days_until_friday = max(0, 4 - week_day)
    week_end_date = today + timedelta(days=days_until_friday)

    last_day = cal_mod.monthrange(today.year, today.month)[1]
    month_end_date = today.replace(day=last_day)

    start_iso = now.isoformat()
    month_end_dt = datetime.combine(month_end_date, datetime.max.time()).replace(tzinfo=_TZ)
    end_iso = month_end_dt.isoformat()

    try:
        # Der Cache-Schlüssel wird auf die Minute gerundet -- ``start_iso`` trägt
        # Mikrosekunden und wäre sonst bei jedem Aufruf neu.
        events = await cached(
            f"capacity:{now:%Y-%m-%dT%H:%M}:{month_end_date}",
            lambda: client.list_events(start_iso, end_iso, top=500),
        )
    except PermissionError as e:
        logger.warning("Kalender capacity: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf den Kalender verweigert")

    week = _calc_period(now, week_end_date, events)
    month = _calc_period(now, month_end_date, events)

    if month.free_hours < week.free_hours:
        # Monat kann nie weniger frei haben als die (enthaltene) Woche. Nur
        # free_hours anheben, alle uebrigen Felder erhalten (frueher wurden hier
        # meeting_hours/blocker_hours weggelassen -> 500 ValidationError).
        month = month.model_copy(update={"free_hours": week.free_hours})

    return CapacityResponse(week=week, month=month, generated_at=now)

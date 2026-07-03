"""Deterministische Kontext-Sammler für Daily/Weekly/Monthly-Briefings.

Best Practice für lokale Modelle: Das Backend sammelt alle Zahlen selbst
(direkte Service-/Router-Funktionsaufrufe, gleiche Quelle wie die REST-API)
und übergibt dem LLM einen fertigen Markdown-Kontext. Das Modell synthetisiert
nur noch Text — kein Tool-Orchestrierungs-Risiko, keine erfundenen Zahlen.

Jede Quelle liefert einen Status (``ok`` / ``leer`` / ``nicht_konfiguriert`` /
``fehler``), damit das Briefing fehlende Quellen transparent benennt statt
Lücken zu verschweigen (Muster analog ``financial_snapshot.py``).
"""

import calendar as cal_mod
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, func, select

from app.database import async_session
from app.models import (
    AgentJob,
    CapacityAllocation,
    CapacityProject,
    CapacityTimeOff,
    EmailTriage,
    Project,
    Task,
    User,
)

logger = logging.getLogger("taskpilot.briefing_data")

_TZ = ZoneInfo("Europe/Zurich")

# Budget-Limits, damit der Prompt für das lokale Modell kompakt bleibt.
_MAX_EVENTS = 15
_MAX_TASKS = 15
_MAX_TRIAGE = 12
_MAX_APPROVALS = 8


def _fmt_min(minutes: int | float | None) -> str:
    """Minuten als Stunden-String (z. B. 90 -> '1.5 h')."""
    if not minutes:
        return "0 h"
    return f"{minutes / 60:.1f} h"


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


class Section:
    """Eine Briefing-Sektion mit Titel, Markdown und Quellen-Status."""

    def __init__(self, key: str, title: str, markdown: str, status: str = "ok"):
        self.key = key
        self.title = title
        self.markdown = markdown
        self.status = status


async def _safe_section(key: str, title: str, builder) -> Section:
    """Führt einen Sektions-Builder defensiv aus.

    HTTPException mit 4xx/503 (fehlende Konfiguration) wird als
    ``nicht_konfiguriert`` gewertet, andere Fehler als ``fehler`` — das
    Briefing bricht nie an einer einzelnen Quelle ab.
    """
    try:
        md = await builder()
        if not (md or "").strip():
            return Section(key, title, "", status="leer")
        return Section(key, title, md)
    except HTTPException as e:
        if e.status_code in (400, 403, 503):
            return Section(key, title, "", status="nicht_konfiguriert")
        logger.warning("Briefing-Sektion '%s' fehlgeschlagen: %s", key, e.detail)
        return Section(key, title, "", status="fehler")
    except Exception as e:  # noqa: BLE001 — einzelne Quelle darf nie alles kippen
        logger.warning("Briefing-Sektion '%s' fehlgeschlagen: %s", key, e)
        return Section(key, title, "", status="fehler")


# ── Kalender ─────────────────────────────────────────────────────────────────

async def _load_events(owner: User, start_iso: str, end_iso: str, top: int = 100) -> list:
    """Termine über die Router-Logik (inkl. Privat-/Frei-Filterung) laden."""
    from app.routers import calendar as calendar_router

    return await calendar_router.list_events(
        start=start_iso,
        end=end_iso,
        top=min(top, 100),
        exclude_categories=None,
        hide_private=True,
        hide_free=True,
        user=owner,
    )


def _fmt_event(ev) -> str:
    start = (ev.start or "")[11:16] if ev.start and len(ev.start) >= 16 else ""
    end = (ev.end or "")[11:16] if ev.end and len(ev.end) >= 16 else ""
    time_str = f"{start}–{end}" if start else "ganztägig"
    loc = f", {ev.location}" if ev.location else ""
    att = f" ({ev.attendees_count} Teilnehmende)" if ev.attendees_count > 1 else ""
    return f"- {time_str}: {ev.subject or '(ohne Betreff)'}{loc}{att}"


def _fmt_event_with_day(ev) -> str:
    day = (ev.start or "")[:10] if ev.start else ""
    return f"- {day} {_fmt_event(ev)[2:]}"


async def _sec_calendar_today(owner: User) -> str:
    now = datetime.now(_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    events = await _load_events(owner, day_start.isoformat(), day_end.isoformat())
    return "\n".join(_fmt_event(ev) for ev in events[:_MAX_EVENTS])


def _parse_event_local(dt_str: str) -> datetime:
    """Graph-Zeitstempel (lokal via Prefer-Header) minutengenau parsen."""
    return datetime.strptime(dt_str[:16], "%Y-%m-%dT%H:%M")


async def _sec_calendar_anomalies(owner: User) -> str:
    """NUR Termin-Auffälligkeiten des Tages: Überlappungen und fehlende Puffer.

    Die vollständige Terminliste steht im Cockpit — das Briefing nennt nur,
    was Aufmerksamkeit braucht. Leer = keine Auffälligkeiten (Sektion entfällt).
    """
    now = datetime.now(_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = await _load_events(owner, day_start.isoformat(), (day_start + timedelta(days=1)).isoformat())

    timed = []
    for ev in events:
        if ev.is_all_day or not ev.start or not ev.end:
            continue
        try:
            timed.append((_parse_event_local(ev.start), _parse_event_local(ev.end), ev))
        except ValueError:
            continue
    timed.sort(key=lambda t: t[0])

    lines = []
    for (_s1, e1, a), (s2, _e2, b) in zip(timed, timed[1:]):
        gap_min = (s2 - e1).total_seconds() / 60
        if gap_min < 0:
            lines.append(
                f"- **Konflikt**: «{a.subject}» (bis {e1.strftime('%H:%M')}) überlappt "
                f"mit «{b.subject}» (ab {s2.strftime('%H:%M')})"
            )
        elif gap_min < 15:
            lines.append(
                f"- Ohne Puffer: «{a.subject}» endet {e1.strftime('%H:%M')}, "
                f"«{b.subject}» beginnt {s2.strftime('%H:%M')}"
            )
    return "\n".join(lines)


_WEEKDAYS_DE = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
_WEEKDAYS_DE_FULL = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
_MONTHS_DE = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]


def _month_de(d: date) -> str:
    return f"{_MONTHS_DE[d.month - 1]} {d.year}"


async def _sec_free_slots(owner: User, week_start: date) -> str:
    """Freie Kalenderfenster (≥45 Min, 08:00–18:00) Mo–Fr der Woche ab ``week_start``.

    Deterministische Grundlage für die Slot-Vorschläge im Wochenbriefing — das
    LLM ordnet den Fenstern nur noch Aufgaben zu, berechnet aber nichts selbst.
    """
    min_window = timedelta(minutes=45)
    lines = []
    for i in range(5):
        day = week_start + timedelta(days=i)
        start_local = datetime.combine(day, datetime.min.time()).replace(hour=8)
        end_local = start_local.replace(hour=18)
        start_dt = start_local.replace(tzinfo=_TZ)
        end_dt = end_local.replace(tzinfo=_TZ)
        events = await _load_events(owner, start_dt.isoformat(), end_dt.isoformat())

        blocked_all_day = False
        intervals: list[tuple[datetime, datetime]] = []
        for ev in events:
            if ev.is_all_day:
                blocked_all_day = True
                break
            try:
                s = _parse_event_local(ev.start)
                e = _parse_event_local(ev.end)
            except (ValueError, TypeError):
                continue
            intervals.append((max(s, start_local), min(e, end_local)))
        if blocked_all_day:
            lines.append(f"- {_WEEKDAYS_DE[i]} {day.strftime('%d.%m.')}: ganztägig belegt")
            continue

        intervals.sort()
        windows: list[tuple[datetime, datetime]] = []
        cursor = start_local
        for s, e in intervals:
            if s - cursor >= min_window:
                windows.append((cursor, s))
            cursor = max(cursor, e)
        if end_local - cursor >= min_window:
            windows.append((cursor, end_local))

        if windows:
            wins = ", ".join(
                f"{a.strftime('%H:%M')}–{b.strftime('%H:%M')} ({_fmt_min((b - a).total_seconds() / 60)})"
                for a, b in windows
            )
            lines.append(f"- {_WEEKDAYS_DE[i]} {day.strftime('%d.%m.')}: {wins}")
        else:
            lines.append(f"- {_WEEKDAYS_DE[i]} {day.strftime('%d.%m.')}: kein freies Fenster")
    return "\n".join(lines)


async def _sec_calendar_range(owner: User, start: datetime, end: datetime, cap: int = _MAX_EVENTS) -> str:
    events = await _load_events(owner, start.isoformat(), end.isoformat())
    lines = [_fmt_event_with_day(ev) for ev in events[:cap]]
    if len(events) > cap:
        lines.append(f"- … und {len(events) - cap} weitere Termine")
    return "\n".join(lines)


async def _sec_calendar_free_capacity(owner: User) -> str:
    """Restkapazität (frei/Meetings/Blocker) für Restwoche + Restmonat."""
    from app.routers import calendar as calendar_router

    cap = await calendar_router.get_capacity(user=owner)
    return (
        f"- Restwoche: {cap.week.free_hours} h frei von {cap.week.total_hours} h "
        f"(Meetings {cap.week.meeting_hours} h, Blocker {cap.week.blocker_hours} h)\n"
        f"- Restmonat: {cap.month.free_hours} h frei von {cap.month.total_hours} h "
        f"(Meetings {cap.month.meeting_hours} h, Blocker {cap.month.blocker_hours} h)"
    )


async def _sec_calendar_week_review(owner: User, week_start: date) -> str:
    """Ganze-Perioden-Rückblick der abgelaufenen Woche (nicht nur 'ab jetzt').

    Schliesst Lücke 4 aus dem Datenaudit: ``get_capacity`` rechnet nur ab
    ``now`` — für den Wochenrückblick zählen wir die gebuchten Stunden der
    kompletten Vorwoche über dieselben Event-Helfer.
    """
    from app.routers.calendar import _event_booked_minutes

    start_dt = datetime.combine(week_start, datetime.min.time()).replace(tzinfo=_TZ)
    end_dt = start_dt + timedelta(days=7)
    events = await _load_events(owner, start_dt.isoformat(), end_dt.isoformat(), top=100)

    booked_min = 0.0
    meeting_min = 0.0
    for ev in events:
        raw = {
            "isAllDay": ev.is_all_day,
            "start": {"dateTime": ev.start},
            "end": {"dateTime": ev.end},
        }
        try:
            minutes = _event_booked_minutes(raw, start_dt, end_dt)
        except Exception:  # noqa: BLE001
            continue
        booked_min += minutes
        if ev.attendees_count > 1:
            meeting_min += minutes
    return (
        f"- Gebuchte Arbeitszeit (Kalender): {_fmt_min(booked_min)} — "
        f"davon Meetings {_fmt_min(meeting_min)}, Blocker/Fokuszeit {_fmt_min(booked_min - meeting_min)}\n"
        f"- Termine gesamt: {len(events)}"
    )


# ── Tasks / Agenda ───────────────────────────────────────────────────────────

async def _sec_tasks_due_today(owner: User) -> str:
    from app.routers import tasks as tasks_router

    today = date.today()
    async with async_session() as db:
        items = await tasks_router.list_due_today(db=db, _user=owner)
    lines = []
    for t in items[:_MAX_TASKS]:
        due = t["due_date"]
        overdue = " **(überfällig)**" if due and due < today else ""
        lines.append(f"- {t['title']} [{t['project_name']}]" + (f", fällig {due}{overdue}" if due else ""))
    return "\n".join(lines)


async def _load_pipeline_columns(owner: User) -> dict[str, list]:
    """Pipeline-Spalten (Name -> TaskCards) laden."""
    from app.routers import pipeline as pipeline_router

    async with async_session() as db:
        out = await pipeline_router.get_pipeline(db=db, _user=owner)
    return {col.name: list(col.tasks) for col in out.columns}


def _fmt_task_cards(cards: list, project_names: dict, cap: int = _MAX_TASKS) -> str:
    lines = []
    for t in cards[:cap]:
        proj = project_names.get(str(t.project_id), "")
        due = f", fällig {t.due_date}" if t.due_date else ""
        agent = " (Agent)" if t.assignee == "agent" else ""
        lines.append(f"- {t.title}" + (f" [{proj}]" if proj else "") + due + agent)
    if len(cards) > cap:
        lines.append(f"- … und {len(cards) - cap} weitere")
    return "\n".join(lines)


async def _project_name_map() -> dict[str, str]:
    from app.models import Project

    async with async_session() as db:
        rows = (await db.execute(select(Project.id, Project.name))).all()
    return {str(pid): name for pid, name in rows}


async def _sec_focus_tasks(owner: User) -> str:
    cols = await _load_pipeline_columns(owner)
    names = await _project_name_map()
    parts = []
    for col_name in ("Focus", "This Week"):
        cards = cols.get(col_name, [])
        if cards:
            label = "Fokus" if col_name == "Focus" else "Diese Woche"
            parts.append(f"**{label}** ({len(cards)}):\n" + _fmt_task_cards(cards, names))
    return "\n\n".join(parts)


async def _sec_week_pipeline(owner: User) -> str:
    cols = await _load_pipeline_columns(owner)
    names = await _project_name_map()
    parts = []
    for col_name, label in (
        ("Focus", "Fokus"), ("This Week", "Diese Woche"), ("Next Week", "Nächste Woche"),
    ):
        cards = cols.get(col_name, [])
        if cards:
            parts.append(f"**{label}** ({len(cards)}):\n" + _fmt_task_cards(cards, names, cap=10))
    return "\n\n".join(parts)


async def _sec_month_pipeline(owner: User) -> str:
    """Vorschau: Tasks des nächsten und übernächsten Monats (Spalten + due_date)."""
    cols = await _load_pipeline_columns(owner)
    names = await _project_name_map()
    today = date.today()
    next_month_start = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    after_next_start = (next_month_start + timedelta(days=32)).replace(day=1)
    after_next_end = (after_next_start + timedelta(days=32)).replace(day=1)

    parts = []
    for col_name, label in (
        ("This Month", "Diesen Monat"), ("Next Month", "Nächster Monat"), ("Beyond", "Später (Beyond)"),
    ):
        cards = cols.get(col_name, [])
        if cards:
            parts.append(f"**{label}** ({len(cards)}):\n" + _fmt_task_cards(cards, names, cap=10))

    # Übernächster Monat aus due_dates über alle Spalten (Beyond-Fenster).
    upcoming = []
    for cards in cols.values():
        for t in cards:
            if t.due_date and after_next_start <= t.due_date < after_next_end:
                upcoming.append(t)
    if upcoming:
        upcoming.sort(key=lambda t: t.due_date)
        parts.append(
            f"**Übernächster Monat ({after_next_start.strftime('%B %Y')})** ({len(upcoming)}):\n"
            + _fmt_task_cards(upcoming, names, cap=10)
        )
    return "\n\n".join(parts)


async def _sec_project_metrics(owner: User) -> str:
    from app.routers import projects as projects_router

    async with async_session() as db:
        metrics = await projects_router.project_metrics(db=db, _user=owner)
    lines = []
    for m in metrics:
        if m.open_tasks == 0 and m.overdue_tasks == 0:
            continue
        overdue = f", **{m.overdue_tasks} überfällig**" if m.overdue_tasks else ""
        lines.append(f"- {m.name}: {m.open_tasks} offen{overdue} ({m.progress_pct:.0f}% erledigt)")
    return "\n".join(lines)


# Filter: wiederkehrende Vorlagen sind keine echten Aufgaben (nur ihre Instanzen).
_NOT_TEMPLATE = ~and_(Task.recurrence_rule.isnot(None), Task.template_id.is_(None))


async def _sec_overdue_open(owner: User) -> str:
    """Liegengebliebenes: offene Aufgaben mit überschrittener Fälligkeit (ohne Vorlagen)."""
    today = date.today()
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Task.title, Task.due_date, Project.name)
                .outerjoin(Project, Task.project_id == Project.id)
                .where(
                    Task.is_completed.is_(False),
                    Task.due_date.isnot(None),
                    Task.due_date < today,
                    _NOT_TEMPLATE,
                )
                .order_by(Task.due_date)
                .limit(_MAX_TASKS)
            )
        ).all()
    return "\n".join(
        f"- {title} [{pname or 'ohne Projekt'}], fällig {due} "
        f"({(today - due).days} Tage überfällig)"
        for title, due, pname in rows
    )


async def _sec_planning_check(owner: User, start: date, end: date) -> str:
    """Planungs-Check: geplante Kapazität vs. erfasste Aufgaben pro verknüpftem Board.

    Kernfrage: Wo sind Stunden eingeplant, aber keine (oder kaum) Aufgaben
    erfasst? Dort fehlt die Planung. Bewusst NUR über die echte
    ``project_id``-Verknüpfung — Kapazitätsprojekte ohne Board-Link werden
    transparent ausgewiesen (Verknüpfung in der Kapazitätsplanung nachziehen).
    """
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    CapacityProject.name,
                    CapacityProject.project_id,
                    func.sum(CapacityAllocation.minutes).label("minutes"),
                    func.min(CapacityAllocation.week_start).label("first_week"),
                )
                .join(CapacityAllocation, CapacityAllocation.capacity_project_id == CapacityProject.id)
                .where(
                    CapacityAllocation.week_start >= start,
                    CapacityAllocation.week_start < end,
                    CapacityAllocation.minutes > 0,
                    CapacityProject.status != "archiviert",
                    CapacityProject.is_billable.is_(True),
                )
                .group_by(CapacityProject.name, CapacityProject.project_id)
                .order_by(func.sum(CapacityAllocation.minutes).desc())
            )
        ).all()

        lines: list[str] = []
        unlinked: list[str] = []
        for name, project_id, minutes, first_week in rows:
            hours = _fmt_min(minutes)
            if project_id is None:
                unlinked.append(f"{name} ({hours})")
                continue
            open_count = await db.scalar(
                select(func.count(Task.id)).where(
                    Task.project_id == project_id,
                    Task.is_completed.is_(False),
                    _NOT_TEMPLATE,
                )
            ) or 0
            if open_count == 0:
                lines.append(
                    f"- **{name}: {hours} geplant (ab {first_week}), aber 0 offene "
                    f"Aufgaben auf dem Board — Aufgabenplanung fehlt**"
                )
            else:
                no_due = await db.scalar(
                    select(func.count(Task.id)).where(
                        Task.project_id == project_id,
                        Task.is_completed.is_(False),
                        Task.due_date.is_(None),
                        _NOT_TEMPLATE,
                    )
                ) or 0
                suffix = f", davon {no_due} ohne Fälligkeitsdatum" if no_due else ""
                lines.append(f"- {name}: {hours} geplant (ab {first_week}), {open_count} offene Aufgaben{suffix}")

    if unlinked:
        lines.append(
            "- Ohne Board-Verknüpfung (Planungs-Check nicht möglich — in der "
            "Kapazitätsplanung «TaskPilot-Board» setzen): " + ", ".join(unlinked)
        )
    return "\n".join(lines)


async def _sec_week_after_next(owner: User, week_start: date) -> str:
    """Übernächste Woche im Radar: Termine + fällige Aufgaben (Vorbereitungs-Check).

    Was hier ansteht und Vorlauf braucht, muss bereits DIESE Woche in die
    freien Fenster eingeplant werden.
    """
    start_dt = datetime.combine(week_start, datetime.min.time()).replace(tzinfo=_TZ)
    cal = await _sec_calendar_range(owner, start_dt, start_dt + timedelta(days=7), cap=10)
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Task.title, Task.due_date, Project.name)
                .outerjoin(Project, Task.project_id == Project.id)
                .where(
                    Task.is_completed.is_(False),
                    Task.due_date >= week_start,
                    Task.due_date < week_start + timedelta(days=7),
                    _NOT_TEMPLATE,
                )
                .order_by(Task.due_date)
                .limit(10)
            )
        ).all()
    parts = []
    if cal:
        parts.append("Termine:\n" + cal)
    if rows:
        parts.append(
            "Fällige Aufgaben:\n"
            + "\n".join(f"- {t} [{p or 'ohne Projekt'}], fällig {d}" for t, d, p in rows)
        )
    return "\n\n".join(parts)


# ── Freigaben / Triage ───────────────────────────────────────────────────────

async def _sec_pending_approvals(owner: User) -> str:
    async with async_session() as db:
        jobs = (
            await db.execute(
                select(AgentJob)
                .where(AgentJob.status == "awaiting_approval")
                .order_by(AgentJob.created_at.desc())
                .limit(_MAX_APPROVALS)
            )
        ).scalars().all()
        review_tasks = (
            await db.execute(
                select(Task.title)
                .where(Task.needs_review == True)  # noqa: E712
                .order_by(Task.created_at.desc())
                .limit(_MAX_APPROVALS)
            )
        ).scalars().all()

    lines = []
    for j in jobs:
        meta = j.metadata_json or {}
        subject = meta.get("subject") or meta.get("description") or j.job_type or "Auftrag"
        lines.append(f"- Freigabe wartet: {str(subject)[:90]} ({j.job_type})")
    for title in review_tasks:
        lines.append(f"- Task-Vorschlag offen: {title[:90]}")
    return "\n".join(lines)


async def _sec_triage_since(owner: User, since: datetime) -> str:
    """Triage-Ergebnisse seit ``since`` — direkte DB-Query (Lücke 1: kein REST-Filter)."""
    async with async_session() as db:
        rows = (
            await db.execute(
                select(EmailTriage)
                .where(EmailTriage.created_at >= since)
                .order_by(EmailTriage.received_at.desc())
                .limit(50)
            )
        ).scalars().all()

    if not rows:
        return ""
    by_class: dict[str, int] = {}
    needs_review: list[EmailTriage] = []
    for r in rows:
        by_class[r.triage_class or "offen"] = by_class.get(r.triage_class or "offen", 0) + 1
        if (r.suggested_action or {}).get("needs_review"):
            needs_review.append(r)

    lines = [
        "Verarbeitet: " + ", ".join(f"{v}× {k}" for k, v in sorted(by_class.items())),
    ]
    important = [r for r in rows if r.triage_class in ("task", "auto_reply")][:_MAX_TRIAGE]
    for r in important:
        who = r.from_name or r.from_address or "?"
        lines.append(f"- [{r.triage_class}] {who}: {(r.subject or '')[:80]}")
    for r in needs_review[:5]:
        lines.append(f"- **Unsicher (needs_review)**: {(r.subject or '')[:80]}")
    return "\n".join(lines)


# ── Kapazität / Toggl ────────────────────────────────────────────────────────

async def _sec_weekly_capacity(owner: User, week_start: date, weeks: int = 2) -> str:
    from app.routers import capacity as capacity_router

    items = await capacity_router.get_weekly_summary(
        from_date=week_start.isoformat(),
        to_date=(week_start + timedelta(weeks=weeks - 1)).isoformat(),
        include_tentative=True,
        user=owner,
    )
    lines = []
    for it in items:
        lines.append(
            f"- KW ab {it.week_start}: geplant {_fmt_min(it.planned_minutes)} von "
            f"{_fmt_min(it.available_minutes)} verfügbar ({it.utilization_pct:.0f}% Auslastung"
            + (f", davon vorläufig {_fmt_min(it.tentative_minutes)}" if it.tentative_minutes else "")
            + ")"
        )
    return "\n".join(lines)


async def _sec_plan_vs_actual(owner: User, week_start: date) -> str:
    """Plan vs. Toggl-Ist der Woche ab ``week_start`` — nur relevante Abweichungen.

    Bewusst gefiltert auf >30% Abweichung (oder Ist ganz ohne Plan): kleine
    Differenzen sind Rauschen und verwässern das Briefing.
    """
    from app.routers import capacity as capacity_router

    data = await capacity_router.get_plan_vs_actual(
        from_date=week_start.isoformat(),
        to_date=(week_start + timedelta(days=6)).isoformat(),
        user=owner,
    )
    lines = []
    for proj in data.get("projects", []):
        planned = sum(w["planned_minutes"] for w in proj.get("weeks", []))
        actual = sum(w["actual_minutes"] for w in proj.get("weeks", []))
        if planned == 0 and actual == 0:
            continue
        delta = actual - planned
        if planned > 0 and abs(delta) / planned <= 0.3:
            continue
        delta_str = f"+{_fmt_min(delta)}" if delta >= 0 else f"-{_fmt_min(abs(delta))}"
        lines.append(
            f"- {proj['name']}: geplant {_fmt_min(planned)}, effektiv {_fmt_min(actual)} ({delta_str})"
        )

    # Lücke 2: Projekte ohne Toggl-Verknüpfung explizit benennen (nicht verschweigen).
    async with async_session() as db:
        unlinked = (
            await db.execute(
                select(CapacityProject.name).where(
                    CapacityProject.toggl_project_id.is_(None),
                    CapacityProject.toggl_client_id.is_(None),
                    CapacityProject.status != "archiviert",
                )
            )
        ).scalars().all()
    if unlinked:
        lines.append(
            "- Ohne Zeiterfassungs-Verknüpfung (kein Ist verfügbar): " + ", ".join(unlinked[:8])
        )
    if not data.get("toggl_data_date"):
        lines.append("- Hinweis: Keine Toggl-Ist-Daten verfügbar (Token/Verknüpfung prüfen).")
    return "\n".join(lines)


async def _sec_monthly_actual(owner: User, month: str) -> str:
    from app.routers import capacity as capacity_router

    data = await capacity_router.get_monthly_actual(month=month, prev_month=True, user=owner)
    lines = []
    for p in data.get("projects", []):
        lines.append(
            f"- {p['name']}: Soll {_fmt_min(p.get('planned_minutes'))} / Ist {_fmt_min(p.get('actual_minutes'))}"
            + (
                f" (Vormonat: {_fmt_min(p.get('prev_month_planned'))} / {_fmt_min(p.get('prev_month_actual'))})"
                if p.get("prev_month_planned") is not None else ""
            )
        )
    return "\n".join(lines)


async def _sec_forecast_revenue(owner: User, d_from: date, d_to: date) -> str:
    from app.routers import capacity as capacity_router

    items = await capacity_router.get_forecast_revenue(
        from_date=d_from.isoformat(), to_date=d_to.isoformat(), user=owner,
    )
    return "\n".join(
        f"- {it.month}: CHF {it.revenue:,.0f} ({it.hours:.0f} h geplant)".replace(",", "'")
        for it in items
    )


def _chf(value: float) -> str:
    return f"CHF {value:,.0f}".replace(",", "'")


async def _sec_revenue_soll_ist(owner: User, month: str) -> str:
    """Umsatz Soll (Kapazitätsprognose) vs. Ist (fakturiert, Bexio) für einen Monat."""
    from app.routers import capacity as capacity_router
    from app.routers import debtors as debtors_router

    y, m = (int(x) for x in month.split("-"))
    m_start = date(y, m, 1)
    m_end = date(y, m, cal_mod.monthrange(y, m)[1])

    soll = 0.0
    for it in await capacity_router.get_forecast_revenue(
        from_date=m_start.isoformat(), to_date=m_end.isoformat(), user=owner,
    ):
        if it.month == month:
            soll = float(it.revenue)

    deb = await debtors_router.get_debtors(user=owner)
    ist = sum(float((rt.months or {}).get(month, 0)) for rt in deb.revenue_trend)
    delta = ist - soll
    delta_str = f"+{_chf(delta)}" if delta >= 0 else f"-{_chf(abs(delta))}"
    lines = [
        f"- Soll (Kapazitätsplanung × Stundensatz): {_chf(soll)}",
        f"- Ist (fakturiert, Bexio): {_chf(ist)}",
        f"- Differenz: {delta_str}",
    ]
    if deb.total_open:
        lines.append(f"- Offene Debitoren gesamt: {_chf(deb.total_open)}")
    return "\n".join(lines)


async def _sec_pipeline_coverage(owner: User, d_from: date, d_to: date) -> str:
    """Pipeline-Deckung: offener Deal-Wert (Pipedrive) vs. geplanter Umsatz."""
    from app.routers import capacity as capacity_router
    from app.routers import pipedrive as pipedrive_router

    summary = await pipedrive_router.get_pipeline_summary(pipeline_id=None, user=owner)
    total_value = 0.0
    total_deals = 0
    stage_lines: list[str] = []
    for pl in summary or []:
        for st in pl.get("stages", []):
            if not st.get("deal_count"):
                continue
            total_deals += int(st["deal_count"])
            total_value += float(st.get("total_value") or 0)
            stage_lines.append(
                f"  - {st.get('name')}: {st['deal_count']} Deal(s), {_chf(float(st.get('total_value') or 0))}"
            )

    planned = sum(
        float(it.revenue)
        for it in await capacity_router.get_forecast_revenue(
            from_date=d_from.isoformat(), to_date=d_to.isoformat(), user=owner,
        )
    )
    lines = [
        f"- Offene Pipeline (Pipedrive): {total_deals} Deal(s), {_chf(total_value)} gesamt",
        f"- Bereits eingeplanter Umsatz {d_from.strftime('%m/%Y')}–{d_to.strftime('%m/%Y')}: {_chf(planned)}",
    ]
    lines.extend(stage_lines)
    return "\n".join(lines)


async def _sec_time_off(start: date, end: date) -> str:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(CapacityTimeOff)
                .where(CapacityTimeOff.date >= start, CapacityTimeOff.date <= end)
                .order_by(CapacityTimeOff.date)
            )
        ).scalars().all()
    return "\n".join(
        f"- {t.date.isoformat()}: {t.label or t.type} ({t.hours:.0f} h)" for t in rows
    )


# ── Optionale Quellen (SIGNA, InvoiceInsight) ────────────────────────────────

async def _sec_signa(owner: User, since: str) -> str:
    from app.routers import signa as signa_router

    resp = await signa_router.list_signals(
        limit=8, offset=0, min_score=None, type=None, topic=None,
        persona=None, since=since, status="relevant", search=None, user=owner,
    )
    signals = getattr(resp, "signals", None) or (resp.get("signals") if isinstance(resp, dict) else [])
    lines = []
    for s in (signals or [])[:8]:
        title = getattr(s, "title", None) or (s.get("title") if isinstance(s, dict) else "?")
        reason = getattr(s, "ai_reason", None) or (s.get("ai_reason") if isinstance(s, dict) else "")
        lines.append(f"- {title}" + (f" — {str(reason)[:100]}" if reason else ""))
    return "\n".join(lines)


async def _sec_creditor_warnings(owner: User) -> str:
    from app.routers import creditors as creditors_router

    anomalies = await creditors_router.get_anomalies(year_from=None, year_to=None, user=owner)
    renewals = await creditors_router.get_renewal_calendar(vendors=None, months_ahead=2, user=owner)
    lines = []
    for group, label in (("critical", "Kritisch"), ("warning", "Warnung")):
        for item in (anomalies.get(group) or [])[:4] if isinstance(anomalies, dict) else []:
            vendor = item.get("kreditor") or item.get("Kreditor") or ""
            title = item.get("title") or item.get("description") or ""
            desc = f"{vendor}: {title}" if vendor and title else (vendor or title or str(item))
            lines.append(f"- Anomalie ({label}): {str(desc)[:120]}")
        for item in (renewals.get(group) or [])[:4] if isinstance(renewals, dict) else []:
            desc = item.get("vendor") or item.get("Kreditor") or str(item)
            days = item.get("days_until") or item.get("Tage")
            lines.append(f"- Renewal ({label}): {str(desc)[:80]}" + (f" in {days} Tagen" if days else ""))
    return "\n".join(lines)


# ── Öffentliche API ──────────────────────────────────────────────────────────

_STATUS_LABELS = {
    "leer": "keine Einträge",
    "nicht_konfiguriert": "Quelle nicht konfiguriert",
    "fehler": "Quelle derzeit nicht erreichbar",
}


def _render(sections: list[Section], header: str) -> dict:
    """Sektionen zu Markdown + Quellen-Statusliste zusammensetzen."""
    parts = [header]
    sources: dict[str, str] = {}
    for sec in sections:
        sources[sec.key] = sec.status
        if sec.status == "ok":
            parts.append(f"### {sec.title}\n\n{sec.markdown}")
        elif sec.status in ("nicht_konfiguriert", "fehler"):
            parts.append(f"### {sec.title}\n\n_({_STATUS_LABELS[sec.status]})_")
        # 'leer' wird bewusst weggelassen — keine leeren Sektionen im Prompt.
    return {"markdown": "\n\n".join(parts), "sources": sources}


async def build_daily_context(owner: User) -> dict:
    """Kontext für das Tagesbriefing — Entscheidungshilfe, kein Cockpit-Duplikat.

    Bewusst schlank: Terminliste, Freigaben, Triage und Warnungen stehen bereits
    im Cockpit. Hier nur, was für die Top-3-Priorisierung nötig ist.
    """
    now = datetime.now(_TZ)
    sections = [
        await _safe_section(
            "termin_auffaellig", "Termin-Auffälligkeiten heute (Konflikte, fehlende Puffer)",
            lambda: _sec_calendar_anomalies(owner),
        ),
        await _safe_section("faellig", "Heute fällige und überfällige Aufgaben", lambda: _sec_tasks_due_today(owner)),
        await _safe_section("fokus", "Fokus-Aufgaben", lambda: _sec_focus_tasks(owner)),
        await _safe_section("restzeit", "Verfügbare Restzeit (Kalender)", lambda: _sec_calendar_free_capacity(owner)),
    ]
    header = f"## Datenlage Tagesbriefing — {_WEEKDAYS_DE_FULL[now.weekday()]}, {now.strftime('%d.%m.%Y')}"
    return _render(sections, header)


async def build_weekly_context(owner: User) -> dict:
    """Kontext für das Wochenbriefing (Rückblick Vorwoche + Planung kommende Woche)."""
    now = datetime.now(_TZ)
    today = now.date()
    this_monday = _monday_of(today)
    # Sonntagabend: 'kommende Woche' = morgen beginnende Woche; Rückblick = laufende.
    next_monday = this_monday + timedelta(weeks=1)
    review_monday = this_monday if today.weekday() >= 5 else this_monday - timedelta(weeks=1)

    next_week_start_dt = datetime.combine(next_monday, datetime.min.time()).replace(tzinfo=_TZ)

    week_after = next_monday + timedelta(weeks=1)

    sections = [
        await _safe_section(
            "liegengeblieben", "Liegengeblieben: offene Aufgaben mit überschrittener Fälligkeit",
            lambda: _sec_overdue_open(owner),
        ),
        await _safe_section(
            "rueckblick_zeit",
            f"Plan vs. Ist Woche ab {review_monday.strftime('%d.%m.')} (nur Abweichungen >30%)",
            lambda: _sec_plan_vs_actual(owner, review_monday),
        ),
        await _safe_section(
            "planungscheck",
            "Planungs-Check: geplante Kapazität vs. erfasste Aufgaben (kommende 2 Wochen)",
            lambda: _sec_planning_check(owner, next_monday, next_monday + timedelta(weeks=2)),
        ),
        await _safe_section(
            "slots", "Freie Kalenderfenster der kommenden Woche (für Slot-Vorschläge)",
            lambda: _sec_free_slots(owner, next_monday),
        ),
        await _safe_section("projekte", "Offene Aufgaben pro Projekt", lambda: _sec_project_metrics(owner)),
        await _safe_section(
            "kapazitaet", "Kapazitätsplanung kommende Wochen",
            lambda: _sec_weekly_capacity(owner, next_monday, weeks=2),
        ),
        await _safe_section(
            "termine", "Termine der kommenden Woche",
            lambda: _sec_calendar_range(owner, next_week_start_dt, next_week_start_dt + timedelta(days=7)),
        ),
        await _safe_section(
            "uebernaechste", f"Übernächste Woche im Radar (ab {week_after.strftime('%d.%m.')})",
            lambda: _sec_week_after_next(owner, week_after),
        ),
        await _safe_section(
            "timeoff", "Abwesenheiten (14 Tage)",
            lambda: _sec_time_off(next_monday, next_monday + timedelta(days=13)),
        ),
    ]
    header = f"## Datenlage Wochenbriefing — KW {next_monday.isocalendar()[1]} (Woche ab {next_monday.strftime('%d.%m.%Y')})"
    return _render(sections, header)


async def build_monthly_context(owner: User) -> dict:
    """Kontext für das Monatsbriefing (Bilanz + Vorschau 2 Monate).

    Adaptiver Bilanzmonat: Der reguläre Lauf am Monatsletzten bilanziert den
    laufenden Monat; ein manueller Trigger früh im Monat (Tag <= 7) bilanziert
    den Vormonat — sonst entstünde eine irreführende «Bilanz» eines Monats,
    der gerade erst begonnen hat.
    """
    now = datetime.now(_TZ)
    today = now.date()
    if today.day <= 7:
        review_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        review_start = today.replace(day=1)
    review_month = review_start.strftime("%Y-%m")

    next_month_start = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    after_next_start = (next_month_start + timedelta(days=32)).replace(day=1)
    after_next_end = after_next_start.replace(
        day=cal_mod.monthrange(after_next_start.year, after_next_start.month)[1]
    )
    next_month_dt = datetime.combine(next_month_start, datetime.min.time()).replace(tzinfo=_TZ)

    sections = [
        await _safe_section(
            "umsatz_sollist", f"Monatsbilanz {review_month}: Umsatz Soll vs. Ist",
            lambda: _sec_revenue_soll_ist(owner, review_month),
        ),
        await _safe_section(
            "umsatz", "Umsatzprognose kommende Monate",
            lambda: _sec_forecast_revenue(owner, next_month_start, after_next_end),
        ),
        await _safe_section(
            "coverage", "Pipeline-Deckung (Pipedrive vs. eingeplanter Umsatz)",
            lambda: _sec_pipeline_coverage(owner, next_month_start, after_next_end),
        ),
        await _safe_section(
            "vorlauf",
            "Vorlauf-Radar: geplante Kapazität vs. erfasste Aufgaben (nächste 2 Monate)",
            lambda: _sec_planning_check(owner, next_month_start, after_next_end),
        ),
        await _safe_section(
            "kapazitaet", "Kapazitätsplanung kommende 8 Wochen",
            lambda: _sec_weekly_capacity(owner, _monday_of(next_month_start), weeks=8),
        ),
        await _safe_section(
            "termine", f"Termine im {_month_de(next_month_start)}",
            lambda: _sec_calendar_range(
                owner, next_month_dt,
                next_month_dt + timedelta(days=cal_mod.monthrange(next_month_start.year, next_month_start.month)[1]),
                cap=20,
            ),
        ),
        await _safe_section(
            "timeoff", "Abwesenheiten (2 Monate)",
            lambda: _sec_time_off(next_month_start, after_next_end),
        ),
        await _safe_section("kreditoren", "Kreditoren-Warnungen und Renewals", lambda: _sec_creditor_warnings(owner)),
    ]
    header = (
        f"## Datenlage Monatsbriefing — Bilanzmonat {review_month}, "
        f"Vorschau {_month_de(next_month_start)} und {_month_de(after_next_start)}"
    )
    return _render(sections, header)


BUILDERS = {
    "daily_briefing": build_daily_context,
    "weekly_briefing": build_weekly_context,
    "monthly_briefing": build_monthly_context,
}

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
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from sqlalchemy import and_, func, select

from app.database import async_session
from app.models import (
    CapacityAllocation,
    CapacityProject,
    CapacityTimeOff,
    FollowupSuggestion,
    Project,
    Task,
    User,
)

logger = logging.getLogger("taskpilot.briefing_data")

_TZ = ZoneInfo("Europe/Zurich")

# Budget-Limit, damit der Prompt für das lokale Modell kompakt bleibt.
_MAX_TASKS = 15


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


async def _sec_events_needing_prep(owner: User) -> str:
    """Termine heute/morgen mit Externen, für die keine Aufgabe vorbereitet ist.

    Wächter-Sektion: ein Erstgespräch oder Kundentermin ohne vorbereitete
    Aufgabe rutscht sonst durch. Abgleich rein über Titel-Stichwörter des
    Termins gegen offene Aufgaben — bewusst grob, aber ohne falsche Ruhe.
    """
    now = datetime.now(_TZ)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    events = await _load_events(
        owner, day_start.isoformat(), (day_start + timedelta(days=2)).isoformat()
    )

    async with async_session() as db:
        open_titles = [
            (t or "").lower()
            for t in (
                await db.execute(
                    select(Task.title).where(Task.is_completed.is_(False), _NOT_TEMPLATE)
                )
            ).scalars().all()
        ]

    lines: list[str] = []
    for ev in events:
        if ev.is_all_day or (ev.attendees_count or 0) < 2:
            continue
        subject = (ev.subject or "").strip()
        if not subject:
            continue
        # Signifikante Stichwörter des Termins (Serientermine wie «Weekly X»
        # ausgenommen — die brauchen keine eigene Vorbereitungsaufgabe).
        if any(w in subject.lower() for w in ("weekly", "bi-weekly", "tri-weekly", "daily", "blocker")):
            continue
        keywords = [w for w in re.split(r"[\s:,–-]+", subject.lower()) if len(w) > 4]
        if keywords and any(any(k in title for k in keywords) for title in open_titles):
            continue
        day = "heute" if (ev.start or "")[:10] == day_start.date().isoformat() else "morgen"
        start = (ev.start or "")[11:16]
        lines.append(f"- {day} {start}: «{subject}» — keine vorbereitete Aufgabe gefunden")
    return "\n".join(lines[:8])


# ── Wächter-Sektionen (Tagesbriefing) ────────────────────────────────────────

async def _sec_followups_due(owner: User) -> str:
    """Fällige Follow-ups: gesendete Mails ohne Antwort (Erkennung: followup.py).

    Nur Vorschläge, deren Task noch offen ist — erledigte oder verworfene fallen
    heraus. Ist die Erkennung abgeschaltet, bleibt die Sektion leer und wird von
    ``_render`` weggelassen; sonst zeigte das Briefing weiterhin Altbestand.
    """
    from app.services.followup import is_followup_enabled

    async with async_session() as db:
        if not await is_followup_enabled(db):
            return ""
        rows = (
            await db.execute(
                select(FollowupSuggestion, Task)
                .outerjoin(Task, FollowupSuggestion.task_id == Task.id)
                .where(FollowupSuggestion.status == "suggested")
                .order_by(FollowupSuggestion.sent_at)
                .limit(_MAX_TASKS)
            )
        ).all()

    today = date.today()
    lines = []
    for sug, task in rows:
        if task is not None and task.is_completed:
            continue
        sent = sug.sent_at.astimezone(_TZ).date() if sug.sent_at else None
        age = f", seit {(today - sent).days} Tagen offen" if sent else ""
        lines.append(
            f"- {sug.recipient or '?'}: «{(sug.subject or '(ohne Betreff)')[:80]}»{age}"
        )
    return "\n".join(lines)


async def _sec_meetings_without_protocol(owner: User) -> str:
    """Meetings der letzten 7 Tage ohne Protokoll — Nachbereitung fehlt."""
    from app.routers import meetings as meetings_router

    items = await meetings_router.list_meetings(limit=30, _user=owner)
    cutoff = (datetime.now(_TZ) - timedelta(days=7)).date().isoformat()
    lines = []
    for m in items:
        if m.has_protocol or not m.started_at or m.started_at[:10] < cutoff:
            continue
        lines.append(f"- {m.started_at[:10]}: «{(m.subject or '(ohne Betreff)')[:80]}»")
    return "\n".join(lines[:6])


async def _sec_lead_time_deadlines(owner: User, ab_tagen: int = 7, bis_tagen: int = 14) -> str:
    """Fristen im Vorlauf-Fenster: was jetzt begonnen werden muss, um nicht knapp zu werden.

    Bewusst NICHT das, was heute oder diese Woche fällig ist (steht im Cockpit)
    — sondern das, was erst später fällig ist und deshalb übersehen wird. Im
    Wochenbriefing ist dies die Gegenseite zur Überbuchung: Dort steht, wie viel
    Zeit frei ist, hier, womit sie zu füllen wäre.
    """
    today = date.today()
    async with async_session() as db:
        rows = (
            await db.execute(
                select(Task.title, Task.due_date, Project.name)
                .outerjoin(Project, Task.project_id == Project.id)
                .where(
                    Task.is_completed.is_(False),
                    Task.due_date >= today + timedelta(days=ab_tagen),
                    Task.due_date <= today + timedelta(days=bis_tagen),
                    _NOT_TEMPLATE,
                )
                .order_by(Task.due_date)
                .limit(10)
            )
        ).all()
    return "\n".join(
        f"- {title} [{pname or 'ohne Projekt'}], fällig {due} (in {(due - today).days} Tagen)"
        for title, due, pname in rows
    )


async def _sec_missing_time_tracking(owner: User) -> str:
    """Arbeitstage der letzten Woche ohne Toggl-Erfassung."""
    from app.routers import debtors as debtors_router

    today = date.today()
    summary = await debtors_router.get_toggl_month(month=today.strftime("%Y-%m"), user=owner)
    tracked = {
        d.date for d in (summary.daily_hours or []) if (d.billable + d.non_billable) > 0
    }
    missing = []
    for delta in range(1, 8):
        day = today - timedelta(days=delta)
        if day.weekday() >= 5 or day.month != today.month:
            continue
        if day.isoformat() not in tracked:
            missing.append(f"{_WEEKDAYS_DE[day.weekday()]} {day.strftime('%d.%m.')}")
    if not missing:
        return ""
    return "- Keine Zeiterfassung an: " + ", ".join(missing)


# ── Tasks / Agenda ───────────────────────────────────────────────────────────

# Filter: wiederkehrende Vorlagen sind keine echten Aufgaben (nur ihre Instanzen).
_NOT_TEMPLATE = ~and_(Task.recurrence_rule.isnot(None), Task.template_id.is_(None))


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
            "Kapazitätsplanung «InnoSmith OS-Board» setzen): " + ", ".join(unlinked)
        )
    return "\n".join(lines)


# ── Kapazität / Toggl ────────────────────────────────────────────────────────

async def _sec_overbooking(owner: User, week_start: date) -> str:
    """Verplante gegen verfügbare Stunden der kommenden Woche — eine Zahl, eine Frage.

    Die Vorgängerfassung listete zwei Wochen mit Auslastungsprozenten. Ein Prozentwert
    ist aber keine Entscheidung: «112% Auslastung» sagt nicht, dass vier Stunden
    keinen Platz haben. Darum wird die Differenz hier ausgerechnet und benannt, und
    zwar nur für die Woche, die tatsächlich geplant wird.
    """
    from app.routers import capacity as capacity_router

    items = await capacity_router.get_weekly_summary(
        from_date=week_start.isoformat(),
        to_date=week_start.isoformat(),
        include_tentative=True,
        user=owner,
    )
    if not items:
        return ""

    it = items[0]
    ueberhang = it.planned_minutes - it.available_minutes
    vorlaeufig = (
        f" Davon vorläufig zugesagt: {_fmt_min(it.tentative_minutes)}."
        if it.tentative_minutes
        else ""
    )

    if ueberhang > 0:
        return (
            f"- **Überbucht um {_fmt_min(ueberhang)}**: geplant {_fmt_min(it.planned_minutes)}, "
            f"verfügbar {_fmt_min(it.available_minutes)}.{vorlaeufig}"
        )
    return (
        f"- Frei: {_fmt_min(-ueberhang)} — geplant {_fmt_min(it.planned_minutes)} von "
        f"{_fmt_min(it.available_minutes)} verfügbar.{vorlaeufig}"
    )


async def _sec_plan_vs_actual(owner: User, week_start: date) -> str:
    """Plan vs. Toggl-Ist der Woche ab ``week_start`` — nur relevante Abweichungen.

    Bewusst gefiltert auf >30% Abweichung (oder Ist ganz ohne Plan): kleine
    Differenzen sind Rauschen und verwässern das Briefing.

    Nur Stunden, kein Geldwert. Eine frühere Fassung rechnete den Mehraufwand zum
    Stundensatz in CHF um und fragte, ob er verrechnet sei. Das ist eine
    Verrechnungsfrage und gehört in die Finanz- und Kapazitätsansichten — das
    Briefing bleibt geldfrei (Entscheid 02.09.2026).
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
        name = proj["name"]
        delta_str = f"+{_fmt_min(delta)}" if delta >= 0 else f"-{_fmt_min(abs(delta))}"
        lines.append(
            f"- {name}: geplant {_fmt_min(planned)}, effektiv {_fmt_min(actual)} ({delta_str})"
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


# ── Monatsplanung ────────────────────────────────────────────────────────────

async def _sec_stalled_projects(owner: User, month_start: date, month_end: date) -> str:
    """Projekte mit offenen Aufgaben, in denen im Bilanzmonat nichts bewegt wurde.

    Rückblick-Sektion des Monatsbriefings: nicht «was war», sondern «wo stand
    alles still». Bewegung = letzte Änderung an einer Aufgabe des Projekts.
    """
    async with async_session() as db:
        rows = (
            await db.execute(
                select(
                    Project.name,
                    func.count(Task.id).label("open_count"),
                    func.max(Task.updated_at).label("last_touch"),
                )
                .join(Task, Task.project_id == Project.id)
                .where(Task.is_completed.is_(False), _NOT_TEMPLATE)
                .group_by(Project.name)
                .having(func.count(Task.id) > 0)
            )
        ).all()

    cutoff = datetime.combine(month_start, datetime.min.time()).replace(tzinfo=_TZ)
    lines = []
    for name, open_count, last_touch in rows:
        if last_touch is None or last_touch >= cutoff:
            continue
        days = (datetime.now(_TZ) - last_touch).days
        lines.append(
            f"- {name}: {open_count} offene Aufgaben, letzte Bewegung "
            f"{last_touch.astimezone(_TZ).strftime('%d.%m.%Y')} (vor {days} Tagen)"
        )
    lines.sort()
    return "\n".join(lines[:10])


async def _sec_month_ahead(owner: User, month_start: date, month_end: date) -> str:
    """Verfügbare Arbeitstage und mehrtägige Termine des kommenden Monats.

    Arbeitstage und Abwesenheiten sind bereits gegeneinander gerechnet — das
    Modell soll keine Kalenderarithmetik betreiben.

    Mehrtägige Termine bleiben drin, weil sie die Zahl korrigieren: Eine Reise
    oder ein Kurs zählt in ``workdays`` als verfügbar, ist es aber nicht. Die
    Gesamtzahl der Termine im Monat ist dagegen gestrichen — eine Statistik, aus
    der keine Entscheidung folgt.
    """
    workdays = sum(
        1
        for i in range((month_end - month_start).days + 1)
        if (month_start + timedelta(days=i)).weekday() < 5
    )
    async with async_session() as db:
        off_dates = (
            await db.execute(
                select(CapacityTimeOff.date).where(
                    CapacityTimeOff.date >= month_start,
                    CapacityTimeOff.date <= month_end,
                )
            )
        ).scalars().all()
    off_count = sum(1 for d in off_dates if d.weekday() < 5)

    lines = [
        f"- Arbeitstage im Monat: {workdays}, davon {off_count} abwesend "
        f"→ {workdays - off_count} verfügbar",
    ]

    # Mehrtägige Termine und Reisen prägen den Monat stärker als Einzeltermine.
    start_dt = datetime.combine(month_start, datetime.min.time()).replace(tzinfo=_TZ)
    end_dt = datetime.combine(month_end, datetime.min.time()).replace(tzinfo=_TZ) + timedelta(days=1)
    events = await _load_events(owner, start_dt.isoformat(), end_dt.isoformat(), top=100)
    all_day = [ev for ev in events if ev.is_all_day]
    if all_day:
        lines.append("- Ganztägige Termine und Reisen:")
        for ev in all_day[:8]:
            lines.append(f"  - {(ev.start or '')[:10]}: {ev.subject or '(ohne Betreff)'}")
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
    """Kontext für das Tagesbriefing — **abgeschaltet seit 02.09.2026**.

    Der Schalter steht in ``briefing.py`` (``briefing_daily_enabled``) auf aus.
    Der Code bleibt, damit ein späterer Anlauf nicht bei Null beginnt — aber er
    darf nicht unverändert wieder eingeschaltet werden.

    **Warum es aus ist.** Nicht wegen schlecht gewählter Regeln, sondern weil auf
    Tagesebene nichts zu verschneiden ist. Was heute gilt, steht vollständig in
    je einer Quelle, und diese Quellen sieht Anthony ohnehin: Kalender, Agenda,
    Freigaben im Cockpit, die tägliche Toggl-Mail. Wer dort trotzdem etwas
    erzeugt, landet zwangsläufig bei einem von zwei Fehlern:

    - **Vermutete Lücke.** ``_sec_meetings_without_protocol`` schliesst von «kein
      ``protocol_md`` in der Datenbank» auf «es gibt kein Protokoll» — TaskPilot
      kennt aber nur seine eigene Ablage. ``_sec_events_needing_prep`` vergleicht
      Wörter des Terminbetreffs mit Aufgabentiteln und behauptet bei fehlendem
      Treffer, es sei nichts vorbereitet. Beides war in der Praxis falsch.
    - **Wahrnehmung ohne Handlung.** ``_sec_calendar_anomalies`` stimmt, aber dass
      zwei Termine aneinanderstossen, ist eine Beschreibung und keine Meldung.

    **Prüfregel für jede künftige Tagessektion** — alle drei Fragen müssen mit Ja
    beantwortbar sein, sonst gehört sie nicht ins Briefing:

    1. Verschneidet der Punkt zwei Quellen, oder wiederholt er eine?
    2. Weiss das System die Tatsache vollständig, oder rät es über Anthonys Kopf?
    3. Folgt daraus eine Handlung, die ohne den Hinweis unterbliebe?

    Von den Sektionen unten besteht nur ``_sec_missing_time_tracking`` alle drei —
    und die deckt Toggl selbst per Tagesmail ab.
    """
    now = datetime.now(_TZ)
    sections = [
        await _safe_section(
            "followups", "Fällige Follow-ups (gesendete E-Mails ohne Antwort)",
            lambda: _sec_followups_due(owner),
        ),
        await _safe_section(
            "termin_auffaellig", "Termin-Auffälligkeiten heute (Konflikte, fehlende Puffer)",
            lambda: _sec_calendar_anomalies(owner),
        ),
        await _safe_section(
            "termin_vorbereitung", "Termine heute und morgen ohne vorbereitete Aufgabe",
            lambda: _sec_events_needing_prep(owner),
        ),
        await _safe_section(
            "protokolle", "Meetings der letzten 7 Tage ohne Protokoll",
            lambda: _sec_meetings_without_protocol(owner),
        ),
        await _safe_section(
            "vorlauf_fristen", "Fristen in 7 bis 14 Tagen (Vorlauf beginnt jetzt)",
            lambda: _sec_lead_time_deadlines(owner),
        ),
        await _safe_section(
            "zeiterfassung", "Arbeitstage der letzten Woche ohne Zeiterfassung",
            lambda: _sec_missing_time_tracking(owner),
        ),
    ]
    header = f"## Datenlage Tagesbriefing — {_WEEKDAYS_DE_FULL[now.weekday()]}, {now.strftime('%d.%m.%Y')}"
    return _render(sections, header)


async def build_weekly_context(owner: User) -> dict:
    """Kontext für das Wochenbriefing — vier Verschnitte, keine Listen.

    Behalten wird nur, was zwei Systeme gegeneinander hält: Kapazität gegen
    Kalender (Überbuchung), Kapazität gegen Board (Planungslücken), Planung gegen
    Toggl (Plan vs. Ist) — dazu die Fristen im Vorlauf als Gegenstück zur freien
    Zeit. Terminlisten, freie Fenster, Projektrückstand, Überfälliges und
    Abwesenheiten sind gestrichen: alles davon steht im Cockpit oder in der Agenda,
    und ein Briefing, das eine Ansicht wiederholt, wird nicht gelesen.
    """
    now = datetime.now(_TZ)
    today = now.date()
    this_monday = _monday_of(today)
    # Sonntagabend: 'kommende Woche' = morgen beginnende Woche; Rückblick = laufende.
    next_monday = this_monday + timedelta(weeks=1)
    review_monday = this_monday if today.weekday() >= 5 else this_monday - timedelta(weeks=1)

    # Prompt-Härtung: Kalenderwochen immer mit Nummer UND Datumsspanne
    # benennen. Aus «Woche ab 27.07.» hat das Modell schon «KW 27» gemacht.
    review_label = (
        f"KW {review_monday.isocalendar()[1]} "
        f"({review_monday.strftime('%d.%m.')}–{(review_monday + timedelta(days=6)).strftime('%d.%m.%Y')})"
    )
    next_label = (
        f"KW {next_monday.isocalendar()[1]} "
        f"({next_monday.strftime('%d.%m.')}–{(next_monday + timedelta(days=6)).strftime('%d.%m.%Y')})"
    )

    sections = [
        await _safe_section(
            "ueberbuchung",
            f"Überbuchung {next_label}: verplante gegen verfügbare Stunden",
            lambda: _sec_overbooking(owner, next_monday),
        ),
        await _safe_section(
            "planungscheck",
            f"Planungslücken: geplante Kapazität vs. erfasste Aufgaben ({next_label} und Folgewoche)",
            lambda: _sec_planning_check(owner, next_monday, next_monday + timedelta(weeks=2)),
        ),
        await _safe_section(
            "rueckblick_zeit",
            f"Plan vs. Ist {review_label} — Abweichungen über 30% (nur Stunden)",
            lambda: _sec_plan_vs_actual(owner, review_monday),
        ),
        await _safe_section(
            "vorlauf", "Vorlauf: Fristen in 7 bis 21 Tagen",
            lambda: _sec_lead_time_deadlines(owner, ab_tagen=7, bis_tagen=21),
        ),
    ]
    header = f"## Datenlage Wochenbriefing — Planung für {next_label}"
    return _render(sections, header)


async def build_monthly_context(owner: User) -> dict:
    """Kontext für das Monatsbriefing — Planungsinstrument, kein Geschäftsbericht.

    Bewusst OHNE Finanz-, Kapazitäts- und Pipeline-Zahlen: Umsatz, Liquidität,
    Debitoren und Deals prüft Anthony in den dafür gebauten Ansichten
    (Finanzen, Kapazität, Debitoren, Pipedrive). Ein Briefing, das diese Zahlen
    wiederholt, konkurriert mit präziseren Quellen und war zudem die Ursache
    der falschen Umsatzangaben (Fakturierung erfolgt erst nach Monatsende).

    Auftrag hier: wo muss die Planung jetzt beginnen, wie viel Zeit ist dafür da,
    und wo stand im Bilanzmonat etwas still. Fristenlisten, Terminzahlen und
    Abwesenheiten als eigene Sektion sind gestrichen — Fristen stehen in der
    Agenda, und die Abwesenheiten sind in der verfügbaren Zeit bereits verrechnet.

    Adaptiver Bilanzmonat: Der reguläre Lauf am letzten Arbeitstag bilanziert
    den laufenden Monat; ein manueller Trigger früh im Monat (Tag <= 7)
    bilanziert den Vormonat.
    """
    now = datetime.now(_TZ)
    today = now.date()
    if today.day <= 7:
        review_start = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    else:
        review_start = today.replace(day=1)
    review_end = review_start.replace(
        day=cal_mod.monthrange(review_start.year, review_start.month)[1]
    )

    next_month_start = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    next_month_end = next_month_start.replace(
        day=cal_mod.monthrange(next_month_start.year, next_month_start.month)[1]
    )
    after_next_start = (next_month_start + timedelta(days=32)).replace(day=1)
    after_next_end = after_next_start.replace(
        day=cal_mod.monthrange(after_next_start.year, after_next_start.month)[1]
    )

    sections = [
        await _safe_section(
            "vorlauf",
            f"Vorlauf-Radar: geplante Kapazität ohne erfasste Aufgaben "
            f"({_month_de(next_month_start)} und {_month_de(after_next_start)})",
            lambda: _sec_planning_check(owner, next_month_start, after_next_end),
        ),
        await _safe_section(
            "monat_voraus",
            f"Verfügbare Zeit und mehrtägige Termine im {_month_de(next_month_start)}",
            lambda: _sec_month_ahead(owner, next_month_start, next_month_end),
        ),
        await _safe_section(
            "stillstand", f"Stillstand im {_month_de(review_start)}: Projekte ohne Bewegung",
            lambda: _sec_stalled_projects(owner, review_start, review_end),
        ),
    ]
    header = (
        f"## Datenlage Monatsbriefing — Rückblick {_month_de(review_start)}, "
        f"Planung {_month_de(next_month_start)} und {_month_de(after_next_start)}"
    )
    return _render(sections, header)


BUILDERS = {
    "daily_briefing": build_daily_context,
    "weekly_briefing": build_weekly_context,
    "monthly_briefing": build_monthly_context,
}

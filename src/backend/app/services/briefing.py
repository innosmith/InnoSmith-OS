"""Briefing-Scheduler: erzeugt geplante Daily/Weekly/Monthly-Briefing-Jobs.

Prüft alle 5 Minuten, ob ein Briefing fällig ist (Zeitpläne aus den
Owner-Settings, Europe/Zurich). Beim Auslösen wird der deterministische
Datenkontext (``briefing_data``) gesammelt und ein ``AgentJob`` direkt in
``queued`` erzeugt — der Hermes-Worker synthetisiert daraus das Briefing.

Dedupe: pro Briefing-Typ und Periode genau ein Job (Vergleich gegen den
berechneten Auslösezeitpunkt). Verpasste Zeitfenster (z. B. System war aus)
werden nur innerhalb einer Karenzfrist nachgeholt — veraltete Briefings
bringen keinen Nutzen mehr.

Das Monatsbriefing läuft am letzten **Arbeitstag** des Monats: Wochenenden und
Abwesenheiten aus ``capacity_time_off`` werden übersprungen, damit das Briefing
vor und nicht während der Ferien erscheint.
"""

import asyncio
import calendar as cal_mod
import logging
import uuid
from datetime import date, datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.database import async_session
from app.models import AgentJob, CapacityTimeOff, User
from app.core.principal import get_owner

logger = logging.getLogger("taskpilot.briefing")

CHECK_INTERVAL_SECONDS = 300
_TZ = ZoneInfo("Europe/Zurich")

# Defaults (überschreibbar via Owner-Settings, siehe _get_schedule)
_DEFAULTS = {
    "briefing_daily_enabled": True,
    "briefing_daily_time": "06:30",
    "briefing_weekly_enabled": True,
    "briefing_weekly_day": 6,       # 0=Montag … 6=Sonntag
    "briefing_weekly_time": "17:00",
    "briefing_monthly_enabled": True,
    "briefing_monthly_time": "17:00",  # am letzten Arbeitstag des Monats
}

# Karenzfrist: wie lange nach dem Soll-Zeitpunkt ein Briefing noch nachgeholt wird.
_GRACE = {
    "daily_briefing": timedelta(hours=12),
    "weekly_briefing": timedelta(hours=36),
    "monthly_briefing": timedelta(hours=48),
}

_TYPE_LABELS = {
    "daily_briefing": "Tagesbriefing",
    "weekly_briefing": "Wochenbriefing",
    "monthly_briefing": "Monatsbriefing",
}


def _parse_time(value: str | None, fallback: str) -> dtime:
    raw = (value or fallback).strip()
    try:
        hour, minute = raw.split(":")
        return dtime(int(hour), int(minute))
    except (ValueError, AttributeError):
        h, m = fallback.split(":")
        return dtime(int(h), int(m))


def _last_workday(year: int, month: int, off_days: set[date]) -> date:
    """Letzter Arbeitstag eines Monats — Wochenenden und Abwesenheiten übersprungen.

    Anthony plant den kommenden Monat gegen Monatsende, aber der konkrete Tag
    hängt von Ferien und Feiertagen ab. Ein Briefing, das während der Ferien
    erscheint, ist zum Zeitpunkt der Planung veraltet.
    """
    d = date(year, month, cal_mod.monthrange(year, month)[1])
    while d.day > 1 and (d.weekday() >= 5 or d in off_days):
        d -= timedelta(days=1)
    return d


def _scheduled_for(
    briefing_type: str,
    settings: dict,
    now: datetime,
    off_days: set[date] | None = None,
) -> datetime | None:
    """Berechnet den jüngsten Soll-Zeitpunkt (<= now) für einen Briefing-Typ.

    Gibt None zurück, wenn der Typ deaktiviert ist oder der Soll-Zeitpunkt in
    der Zukunft liegt bzw. ausserhalb der Karenzfrist.
    """
    off_days = off_days or set()
    def _enabled(key: str) -> bool:
        val = settings.get(key)
        return _DEFAULTS[key] if val is None else bool(val)

    today = now.date()

    if briefing_type == "daily_briefing":
        if not _enabled("briefing_daily_enabled"):
            return None
        t = _parse_time(settings.get("briefing_daily_time"), _DEFAULTS["briefing_daily_time"])
        # Werktags: rückwärts den jüngsten Werktag-Slot suchen (max. 3 Tage).
        for delta in range(4):
            d = today - timedelta(days=delta)
            if d.weekday() >= 5:
                continue
            candidate = datetime.combine(d, t, tzinfo=_TZ)
            if candidate <= now:
                return candidate
        return None

    if briefing_type == "weekly_briefing":
        if not _enabled("briefing_weekly_enabled"):
            return None
        day = settings.get("briefing_weekly_day")
        day = _DEFAULTS["briefing_weekly_day"] if day is None else int(day)
        t = _parse_time(settings.get("briefing_weekly_time"), _DEFAULTS["briefing_weekly_time"])
        days_back = (today.weekday() - day) % 7
        candidate = datetime.combine(today - timedelta(days=days_back), t, tzinfo=_TZ)
        if candidate > now:
            candidate -= timedelta(weeks=1)
        return candidate

    if briefing_type == "monthly_briefing":
        if not _enabled("briefing_monthly_enabled"):
            return None
        t = _parse_time(settings.get("briefing_monthly_time"), _DEFAULTS["briefing_monthly_time"])
        # Letzter Arbeitstag des aktuellen Monats; sonst des Vormonats.
        candidate = datetime.combine(
            _last_workday(today.year, today.month, off_days), t, tzinfo=_TZ
        )
        if candidate > now:
            prev_month_end = today.replace(day=1) - timedelta(days=1)
            candidate = datetime.combine(
                _last_workday(prev_month_end.year, prev_month_end.month, off_days), t, tzinfo=_TZ
            )
        return candidate

    return None


async def _job_exists_since(db, briefing_type: str, since: datetime) -> bool:
    result = await db.execute(
        select(AgentJob.id)
        .where(
            AgentJob.job_type == briefing_type,
            AgentJob.created_at >= since.astimezone(timezone.utc),
        )
        .limit(1)
    )
    return result.scalar_one_or_none() is not None


async def create_briefing_job(
    briefing_type: str,
    owner: User,
    scheduled: datetime,
    *,
    manual: bool = False,
) -> uuid.UUID:
    """Sammelt den Datenkontext und erzeugt den Briefing-Job (queued).

    Wird sowohl vom Scheduler (``check_briefings_due``) als auch vom manuellen
    Trigger (``POST /api/briefings/generate``) genutzt. Bei ``manual=True`` wird
    ``metadata_json["manual_trigger"]`` gesetzt; Dedupe/Karenz greifen bewusst
    nicht (der Aufrufer entscheidet). Gibt die erzeugte Job-ID zurück.
    """
    from app.services.briefing_data import BUILDERS

    # Frische Toggl-Daten für Weekly/Monthly (Lücke 3: 24h-Cache).
    if briefing_type in ("weekly_briefing", "monthly_briefing"):
        try:
            from app.routers.capacity import _toggl_cache

            _toggl_cache.clear()
        except Exception:  # noqa: BLE001
            pass

    context = await BUILDERS[briefing_type](owner)

    async with async_session() as db:
        job = AgentJob(
            user_id=owner.id if owner else None,
            job_type=briefing_type,
            status="queued",
            metadata_json={
                "briefing_type": briefing_type,
                "scheduled_for": scheduled.isoformat(),
                "context_markdown": context["markdown"],
                "sources": context["sources"],
                "autonomy_level": "L2",
                "description": f"{_TYPE_LABELS[briefing_type]} erstellen",
                "manual_trigger": manual,
            },
        )
        db.add(job)
        await db.flush()
        job_id = job.id
        await db.commit()
    logger.info(
        "%s-Job erzeugt (%s, Soll-Zeitpunkt %s)",
        _TYPE_LABELS[briefing_type],
        "manuell" if manual else "geplant",
        scheduled.isoformat(),
    )
    return job_id


async def check_briefings_due() -> int:
    """Ein Scheduler-Durchlauf: prüft alle Briefing-Typen. Anzahl erzeugter Jobs."""
    now = datetime.now(_TZ)
    created = 0

    async with async_session() as db:
        owner = await get_owner(db)
        if owner is None:
            return 0
        settings = dict(owner.settings or {})

        # Abwesenheiten des laufenden und des Vormonats: sie verschieben den
        # Auslösetag des Monatsbriefings auf den letzten Arbeitstag davor.
        window_start = (now.date().replace(day=1) - timedelta(days=1)).replace(day=1)
        off_days = set(
            (
                await db.execute(
                    select(CapacityTimeOff.date).where(CapacityTimeOff.date >= window_start)
                )
            ).scalars().all()
        )

        due: list[tuple[str, datetime]] = []
        for briefing_type in ("daily_briefing", "weekly_briefing", "monthly_briefing"):
            scheduled = _scheduled_for(briefing_type, settings, now, off_days)
            if scheduled is None:
                continue
            if now - scheduled > _GRACE[briefing_type]:
                continue
            if await _job_exists_since(db, briefing_type, scheduled):
                continue
            due.append((briefing_type, scheduled))

    for briefing_type, scheduled in due:
        try:
            await create_briefing_job(briefing_type, owner, scheduled)
            created += 1
        except Exception:
            logger.exception("Briefing-Job (%s) konnte nicht erzeugt werden", briefing_type)

    return created


async def briefing_scheduler_loop() -> None:
    logger.info("Briefing-Scheduler gestartet (Intervall: %ds)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            await check_briefings_due()
        except Exception:
            logger.exception("Briefing-Scheduler: unerwarteter Fehler")
        try:
            # Follow-up-Erkennung läuft im selben Takt, intern max. 1x pro Tag.
            from app.services.followup import maybe_run_daily_followup_check

            await maybe_run_daily_followup_check()
        except Exception:
            logger.exception("Follow-up-Check: unerwarteter Fehler")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


_scheduler_task: asyncio.Task | None = None


async def start_briefing_scheduler() -> None:
    global _scheduler_task
    _scheduler_task = asyncio.create_task(briefing_scheduler_loop())


async def stop_briefing_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    _scheduler_task = None

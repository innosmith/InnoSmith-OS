"""Recurring-Task-Scheduler: prüft periodisch, ob neue Task-Instanzen
aus Vorlagen (Templates) erzeugt werden müssen.

Ein Template ist ein Task mit `recurrence_rule IS NOT NULL` und
`template_id IS NULL`.  Instanzen sind Tasks mit gesetztem `template_id`.
"""

import asyncio
import logging
import os
import sys
import uuid
from datetime import date, datetime, timedelta, timezone

from croniter import croniter
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import async_session
from app.models import AgentJob, ChecklistItem, Tag, Task, TaskTag
from app.core.principal import system_principal_id

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logger = logging.getLogger("taskpilot.recurring")

CHECK_INTERVAL_SECONDS = 300  # 5 Minuten


async def _get_templates(db: AsyncSession) -> list[Task]:
    result = await db.execute(
        select(Task).where(
            and_(
                Task.recurrence_rule.isnot(None),
                Task.recurrence_rule != "",
                Task.template_id.is_(None),
            )
        )
    )
    return list(result.scalars().all())


async def _latest_instance_due(
    db: AsyncSession, template_id: uuid.UUID
) -> datetime | None:
    """Liefert den letzten geplanten Termin (due_date) aller Instanzen.

    Fallback auf created_at falls keine Instanz ein due_date hat.
    """
    result = await db.execute(
        select(func.max(Task.due_date)).where(Task.template_id == template_id)
    )
    latest_due = result.scalar_one_or_none()
    if latest_due:
        return datetime.combine(latest_due, datetime.max.time(), tzinfo=timezone.utc)
    result2 = await db.execute(
        select(func.max(Task.created_at)).where(Task.template_id == template_id)
    )
    return result2.scalar_one_or_none()


def _resolve_last_spawn_ts(
    last_spawn: date | None,
    latest_instance_ts: datetime | None,
) -> datetime | None:
    """Kombiniert persistierten Spawn-Merker und abgeleiteten Instanz-Stand.

    Der Merker überlebt Instanz-Löschungen und verhindert so den Respawn
    einer bereits erzeugten Okkurrenz. Beide Quellen werden als Tagesende-UTC
    normalisiert; der spätere Wert gewinnt (Merker wandert nur vorwärts).
    """
    candidates: list[datetime] = []
    if last_spawn is not None:
        candidates.append(
            datetime.combine(last_spawn, datetime.max.time(), tzinfo=timezone.utc)
        )
    if latest_instance_ts is not None:
        ts = latest_instance_ts
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        candidates.append(ts)
    return max(candidates) if candidates else None


async def _has_active_instance(db: AsyncSession, template_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(func.count()).where(
            and_(
                Task.template_id == template_id,
                Task.is_completed.is_(False),
            )
        )
    )
    return (result.scalar_one() or 0) > 0


def _select_target_occurrence(
    rule: str,
    now: datetime,
    last_ts: datetime | None,
    created_at: datetime,
) -> datetime:
    """Bestimmt die zu spawnende Cron-Okkurrenz (timezone-aware, UTC-normalisiert).

    - Existieren bereits Instanzen (`last_ts`), wird die nächste Okkurrenz nach
      der letzten Instanz gewählt (cadence durch Abschluss gesteuert).
    - Für die erste Instanz wird die aktuelle Periode nachgeholt, wenn die
      jüngste vergangene Okkurrenz heute liegt (z.B. Vorlage am selben Tag NACH
      der Cron-Uhrzeit erstellt). Andernfalls die nächste Okkurrenz nach der
      Erstellung — kein Backfill von Okkurrenzen vor der Vorlagen-Erstellung.
    """
    def _aware(dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    if last_ts is not None:
        target = croniter(rule, last_ts).get_next(datetime)
    else:
        prev_run = _aware(croniter(rule, now).get_prev(datetime))
        created = _aware(created_at)
        if prev_run.date() == now.date() and prev_run.date() >= created.date():
            target = prev_run
        else:
            target = croniter(rule, created).get_next(datetime)
    return _aware(target)


async def _instance_exists_for_date(
    db: AsyncSession, template_id: uuid.UUID, due: date
) -> bool:
    """Prüft, ob für eine Vorlage bereits eine Instanz mit diesem due_date
    existiert (auch erledigte). Verhindert Mehrfach-Spawn derselben Okkurrenz."""
    result = await db.execute(
        select(func.count()).where(
            and_(
                Task.template_id == template_id,
                Task.due_date == due,
            )
        )
    )
    return (result.scalar_one() or 0) > 0


async def _copy_template(
    db: AsyncSession, template: Task, due_date: date | None = None,
) -> Task:
    """Erzeugt eine frische Instanz aus dem Template inkl. Checkliste + Tags.

    Wenn due_date uebergeben wird, wird die Instanz via Smart Placement
    automatisch in die passende Agenda-Spalte platziert.
    """
    from app.services.pipeline_promoter import auto_place_task

    instance = Task(
        title=template.title,
        description=template.description,
        project_id=template.project_id,
        board_column_id=template.board_column_id,
        board_position=0.0,
        pipeline_column_id=template.pipeline_column_id,
        pipeline_position=None,
        assignee=template.assignee,
        due_date=due_date,
        data_class=template.data_class,
        llm_override=template.llm_override,
        autonomy_level=template.autonomy_level,
        template_id=template.id,
    )

    max_pos_result = await db.execute(
        select(func.coalesce(func.max(Task.board_position), 0.0)).where(
            Task.board_column_id == template.board_column_id
        )
    )
    instance.board_position = (max_pos_result.scalar_one() or 0.0) + 1.0

    db.add(instance)
    await db.flush()

    placed = await auto_place_task(db, instance)
    if not placed and template.pipeline_column_id:
        max_pp = await db.execute(
            select(func.coalesce(func.max(Task.pipeline_position), 0.0)).where(
                Task.pipeline_column_id == template.pipeline_column_id
            )
        )
        instance.pipeline_position = (max_pp.scalar_one() or 0.0) + 1.0

    cl_result = await db.execute(
        select(ChecklistItem)
        .where(ChecklistItem.task_id == template.id)
        .order_by(ChecklistItem.position)
    )
    for item in cl_result.scalars().all():
        new_item = ChecklistItem(
            task_id=instance.id,
            text=item.text,
            is_checked=False,
            position=item.position,
        )
        db.add(new_item)

    tag_result = await db.execute(
        select(Tag).join(TaskTag).where(TaskTag.task_id == template.id)
    )
    for tag in tag_result.scalars().all():
        db.add(TaskTag(task_id=instance.id, tag_id=tag.id))

    # Agent-Delegation: Instanzen mit assignee='agent' erzeugen wie im
    # Task-Router einen 'planned'-Job. Der Agent-Scheduler gibt ihn am
    # Fälligkeitstag frei (planned -> queued) -- vorher lief die Zuweisung
    # bei Recurring-Instanzen komplett ins Leere (kein Job, keine Ausführung).
    if instance.assignee == "agent":
        db.add(AgentJob(
            user_id=await system_principal_id(db),
            task_id=instance.id,
            job_type="task",
            status="planned",
            llm_model=instance.llm_override,
            metadata_json={
                "autonomy_level": instance.autonomy_level,
                "data_class": instance.data_class,
                "llm_override": instance.llm_override,
                "recurring_template_id": str(template.id),
            },
        ))
        logger.info(
            "Recurring-Instanz %s: Agent-Job (planned) erzeugt", instance.id,
        )

    await _create_calendar_blocker(instance, template)

    return instance


async def _create_calendar_blocker(instance: Task, template: Task) -> None:
    """Kalender-Zeitblocker fuer eine Recurring-Task-Instanz erstellen."""
    duration = template.calendar_duration_minutes
    if not duration or duration <= 0:
        return

    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        return

    client = GraphClient(GraphConfig(
        tenant_id=s.graph_tenant_id,
        client_id=s.graph_client_id,
        client_secret=s.graph_client_secret,
        user_email=s.graph_user_email,
    ))

    try:
        now = datetime.now(timezone.utc)
        target_date = instance.due_date or now.date()

        # Hybrid-Konzept: Cron-Zeit = Kalender-Startzeit.
        # Stunde/Minute aus der Recurrence-Rule extrahieren.
        cron = croniter(template.recurrence_rule)
        cron_next = cron.get_next(datetime)
        cron_hour = cron_next.hour
        cron_minute = cron_next.minute

        fmt = "%Y-%m-%dT%H:%M:%S"

        search_start = datetime.combine(
            target_date, datetime.min.time()
        ).replace(hour=cron_hour, minute=cron_minute)
        search_end = datetime.combine(
            target_date, datetime.min.time()
        ).replace(hour=20, minute=0)

        if search_start >= search_end:
            search_start = search_start.replace(hour=7, minute=0)

        slots = await client.find_free_slots(
            start=search_start.strftime(fmt),
            end=search_end.strftime(fmt),
            duration_minutes=duration,
        )

        if not slots:
            next_day_start = datetime.combine(
                target_date + timedelta(days=1), datetime.min.time()
            ).replace(hour=cron_hour, minute=cron_minute)
            next_day_end = datetime.combine(
                target_date + timedelta(days=1), datetime.min.time()
            ).replace(hour=20, minute=0)
            slots = await client.find_free_slots(
                start=next_day_start.strftime(fmt),
                end=next_day_end.strftime(fmt),
                duration_minutes=duration,
            )

        if slots:
            slot_start = slots[0]["start"]
            from dateutil.parser import isoparse
            start_dt = isoparse(slot_start)
            end_dt = start_dt + timedelta(minutes=duration)

            event = await client.create_event(
                subject=instance.title,
                start=start_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                end=end_dt.strftime("%Y-%m-%dT%H:%M:%S"),
                body=instance.description or "",
                show_as="busy",
            )
            instance.calendar_event_id = event.get("id")
            logger.info(
                "Kalender-Blocker erstellt: '%s' (%d Min) fuer Task %s",
                instance.title, duration, instance.id,
            )
        else:
            logger.warning("Kein freier Slot fuer Kalender-Blocker: %s", instance.title)

    except Exception:
        logger.exception("Fehler beim Erstellen des Kalender-Blockers fuer Task %s", instance.id)
    finally:
        await client.close()


async def _check_and_spawn(db: AsyncSession) -> int:
    """Prüft alle Templates und erzeugt fällige Instanzen.
    Gibt die Anzahl neu erzeugter Instanzen zurück."""
    templates = await _get_templates(db)
    now = datetime.now(timezone.utc)
    spawned = 0

    for tmpl in templates:
        try:
            if not croniter.is_valid(tmpl.recurrence_rule):
                logger.warning("Ungültige Cron-Expression '%s' bei Task %s", tmpl.recurrence_rule, tmpl.id)
                continue

            if await _has_active_instance(db, tmpl.id):
                continue

            if tmpl.recurrence_end_date and now.date() > tmpl.recurrence_end_date:
                continue

            if tmpl.recurrence_max_instances:
                count_result = await db.execute(
                    select(func.count()).where(Task.template_id == tmpl.id)
                )
                if (count_result.scalar_one() or 0) >= tmpl.recurrence_max_instances:
                    continue

            latest_instance_ts = await _latest_instance_due(db, tmpl.id)
            last_ts = _resolve_last_spawn_ts(
                tmpl.recurrence_last_spawn, latest_instance_ts
            )
            target_run = _select_target_occurrence(
                tmpl.recurrence_rule, now, last_ts, tmpl.created_at
            )

            if tmpl.recurrence_end_date and target_run.date() > tmpl.recurrence_end_date:
                continue

            if target_run.date() > now.date():
                continue

            # Fix C: Mehrfach-Spawn derselben Okkurrenz verhindern.
            if await _instance_exists_for_date(db, tmpl.id, target_run.date()):
                continue

            instance = await _copy_template(db, tmpl, due_date=target_run.date())
            # Persistierter Merker überlebt Instanz-Löschung → kein Respawn.
            tmpl.recurrence_last_spawn = target_run.date()
            spawned += 1
            logger.info(
                "Recurring-Instanz '%s' (id=%s) aus Template %s erzeugt (due=%s)",
                instance.title, instance.id, tmpl.id, target_run.date(),
            )
        except Exception:
            logger.exception("Fehler bei Recurring-Check für Template %s", tmpl.id)

    return spawned


async def recurring_loop() -> None:
    """Endlosschleife, die periodisch recurring Tasks prüft."""
    logger.info("Recurring-Scheduler gestartet (Intervall: %ds)", CHECK_INTERVAL_SECONDS)
    while True:
        try:
            async with async_session() as db:
                count = await _check_and_spawn(db)
                await db.commit()
                if count:
                    logger.info("%d neue Recurring-Instanz(en) erzeugt", count)
        except Exception:
            logger.exception("Recurring-Scheduler: unerwarteter Fehler")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


_scheduler_task: asyncio.Task | None = None


async def start_recurring_scheduler() -> None:
    global _scheduler_task
    _scheduler_task = asyncio.create_task(recurring_loop())


async def stop_recurring_scheduler() -> None:
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        try:
            await _scheduler_task
        except asyncio.CancelledError:
            pass
    _scheduler_task = None

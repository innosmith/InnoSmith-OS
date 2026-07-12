"""Notification-Service: Erstellt, dedupliziert und verwaltet In-App-Notifications.

Enthält ausserdem den periodischen Due-Soon-Scheduler und den
Health-Check-Monitor für den Hermes-Worker.
"""

import asyncio
import logging
import re
import uuid as uuid_mod
from datetime import date, datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import AgentJob, Notification, Task, User
from app.core.principal import get_owner

logger = logging.getLogger("taskpilot.notifications")

MENTION_PATTERN = re.compile(r"@\[([^\]]+)\]\(([0-9a-f\-]{36})\)")

_scheduler_task: asyncio.Task | None = None
_health_task: asyncio.Task | None = None

DUE_SOON_INTERVAL = 3600  # 1 Stunde
HEALTH_CHECK_INTERVAL = 300  # 5 Minuten
HEALTH_STALE_MINUTES = 30


async def create_notification(
    db: AsyncSession,
    *,
    user_id: UUID,
    type: str,
    title: str,
    body: str | None = None,
    link: str | None = None,
    source_type: str | None = None,
    source_id: UUID | None = None,
) -> Notification:
    notif = Notification(
        user_id=user_id,
        type=type,
        title=title,
        body=body,
        link=link,
        source_type=source_type,
        source_id=source_id,
    )
    db.add(notif)
    await db.flush()
    logger.info("Notification erstellt: type=%s, user=%s, title=%s", type, user_id, title[:60])
    return notif


async def parse_mentions(text: str) -> list[dict]:
    """Extrahiert @[Name](uuid) Mentions und gibt [{id, display_name}] zurück."""
    matches = MENTION_PATTERN.findall(text)
    if not matches:
        return []

    results = []
    seen = set()
    async with async_session() as db:
        for display_name, user_id_str in matches:
            if user_id_str in seen:
                continue
            seen.add(user_id_str)
            try:
                uid = uuid_mod.UUID(user_id_str)
            except ValueError:
                continue
            result = await db.execute(select(User).where(User.id == uid, User.is_active.is_(True)))
            user = result.scalar_one_or_none()
            if user:
                results.append({"id": uid, "display_name": user.display_name})
    return results


async def notify_mentions(
    db: AsyncSession,
    text: str,
    task_id: UUID,
    task_title: str,
    actor_email: str,
    actor_id: UUID,
) -> None:
    """Erzeugt Notifications für alle @-Mentions in einem Kommentar."""
    mentioned = await parse_mentions(text)
    for m in mentioned:
        if m["id"] == actor_id:
            continue
        await create_notification(
            db,
            user_id=m["id"],
            type="comment_mention",
            title=f"{actor_email} hat dich erwähnt",
            body=f'In "{task_title}": {_strip_mention_markup(text)[:200]}',
            link=f"/tasks/{task_id}",
            source_type="task",
            source_id=task_id,
        )


async def notify_task_assigned(
    db: AsyncSession,
    task: Task,
    new_assignee_id: UUID,
    assigner_email: str,
) -> None:
    await create_notification(
        db,
        user_id=new_assignee_id,
        type="task_assigned",
        title=f"Aufgabe zugewiesen: {task.title}",
        body=f"{assigner_email} hat dir diese Aufgabe zugewiesen.",
        link=f"/tasks/{task.id}",
        source_type="task",
        source_id=task.id,
    )


async def notify_agent_awaiting_approval(
    db: AsyncSession,
    job_id: UUID,
    subject: str | None = None,
) -> None:
    owner = await _get_owner(db)
    if not owner:
        return
    title = "Agent wartet auf Freigabe"
    if subject:
        title = f"Freigabe nötig: {subject}"
    await create_notification(
        db,
        user_id=owner.id,
        type="agent_awaiting_approval",
        title=title,
        link="/agenten",
        source_type="agent_job",
        source_id=job_id,
    )


async def notify_agent_completed(
    db: AsyncSession,
    job_id: UUID,
    subject: str | None = None,
) -> None:
    """Post-hoc-Benachrichtigung für autonom (L2 'Melden') erledigte Aufträge."""
    owner = await _get_owner(db)
    if not owner:
        return
    title = "Agent hat einen Auftrag erledigt"
    if subject:
        title = f"Erledigt: {subject}"
    await create_notification(
        db,
        user_id=owner.id,
        type="agent_completed",
        title=title,
        link="/agenten",
        source_type="agent_job",
        source_id=job_id,
    )


async def notify_task_suggested(
    db: AsyncSession,
    task_id: UUID,
    task_title: str,
    from_email: str | None = None,
) -> None:
    owner = await _get_owner(db)
    if not owner:
        return
    body = f"Aus E-Mail von {from_email}" if from_email else None
    await create_notification(
        db,
        user_id=owner.id,
        type="task_suggested",
        title=f"Task-Vorschlag: {task_title}",
        body=body,
        link="/cockpit",
        source_type="task",
        source_id=task_id,
    )


async def notify_chat_triage_task(
    db: AsyncSession,
    task_id: UUID,
    task_title: str,
    from_name: str | None = None,
) -> None:
    owner = await _get_owner(db)
    if not owner:
        return
    body = f"Aus Teams-Chat von {from_name}" if from_name else None
    await create_notification(
        db,
        user_id=owner.id,
        type="chat_triage_task",
        title=f"Chat-Task erkannt: {task_title}",
        body=body,
        link="/cockpit",
        source_type="task",
        source_id=task_id,
    )


async def notify_briefing_ready(
    db: AsyncSession,
    job_id: UUID,
    briefing_label: str,
) -> None:
    """Benachrichtigung, sobald ein Daily/Weekly/Monthly-Briefing bereitliegt."""
    owner = await _get_owner(db)
    if not owner:
        return
    await create_notification(
        db,
        user_id=owner.id,
        type="briefing_ready",
        title=f"{briefing_label} ist bereit",
        link="/",
        source_type="agent_job",
        source_id=job_id,
    )


async def notify_meeting_summary_ready(
    db: AsyncSession,
    transcript_id: UUID,
    subject: str | None = None,
    action_item_count: int = 0,
) -> None:
    """Benachrichtigung, sobald ein Meeting-Protokoll bereitliegt."""
    owner = await _get_owner(db)
    if not owner:
        return
    title = f"Meeting-Protokoll bereit: {subject}" if subject else "Meeting-Protokoll bereit"
    body = (
        f"{action_item_count} Action-Item-Vorschlag/Vorschläge zur Prüfung"
        if action_item_count else None
    )
    await create_notification(
        db,
        user_id=owner.id,
        type="meeting_summary_ready",
        title=title,
        body=body,
        link="/agenten?tab=meetings",
        source_type="meeting_transcript",
        source_id=transcript_id,
    )


async def _get_owner(db: AsyncSession) -> User | None:
    return await get_owner(db, active_only=True)


def _strip_mention_markup(text: str) -> str:
    """Entfernt @[Name](id) Markup und gibt lesbaren Text zurück."""
    return MENTION_PATTERN.sub(r"@\1", text)


# --- Due-Soon-Scheduler ---

async def _due_soon_loop() -> None:
    await asyncio.sleep(60)
    logger.info("Due-Soon-Scheduler gestartet (Intervall: %ds)", DUE_SOON_INTERVAL)

    while True:
        try:
            await _check_due_soon()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Due-Soon-Check fehlgeschlagen")
        await asyncio.sleep(DUE_SOON_INTERVAL)


async def _check_due_soon() -> int:
    tomorrow = date.today() + timedelta(days=1)
    created = 0

    async with async_session() as db:
        result = await db.execute(
            select(Task).where(
                Task.is_completed.is_(False),
                Task.due_date.isnot(None),
                Task.due_date <= tomorrow,
                Task.due_date >= date.today(),
            )
        )
        tasks = list(result.scalars().all())

        for task in tasks:
            try:
                assignee_id = uuid_mod.UUID(task.assignee)
            except ValueError:
                continue

            existing = await db.execute(
                select(func.count()).select_from(Notification).where(
                    Notification.user_id == assignee_id,
                    Notification.type == "task_due_soon",
                    Notification.source_id == task.id,
                    Notification.created_at >= datetime.now(timezone.utc) - timedelta(hours=20),
                )
            )
            if existing.scalar() > 0:
                continue

            await create_notification(
                db,
                user_id=assignee_id,
                type="task_due_soon",
                title=f"Fällig: {task.title}",
                body=f"Diese Aufgabe ist am {task.due_date} fällig.",
                link=f"/tasks/{task.id}",
                source_type="task",
                source_id=task.id,
            )
            created += 1

        if created:
            await db.commit()
            logger.info("Due-Soon: %d Notifications erstellt", created)

    return created


# --- Health-Check-Monitor ---

async def _health_check_loop() -> None:
    await asyncio.sleep(120)
    logger.info("Health-Check-Monitor gestartet (Intervall: %ds)", HEALTH_CHECK_INTERVAL)

    while True:
        try:
            await _check_worker_health()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Health-Check fehlgeschlagen")
        await asyncio.sleep(HEALTH_CHECK_INTERVAL)


async def _check_worker_health() -> None:
    async with async_session() as db:
        queued_result = await db.execute(
            select(func.count()).select_from(AgentJob).where(AgentJob.status == "queued")
        )
        queued_count = queued_result.scalar() or 0
        if queued_count == 0:
            return

        cutoff = datetime.now(timezone.utc) - timedelta(minutes=HEALTH_STALE_MINUTES)
        oldest_result = await db.execute(
            select(AgentJob.created_at)
            .where(AgentJob.status == "queued")
            .order_by(AgentJob.created_at)
            .limit(1)
        )
        oldest = oldest_result.scalar_one_or_none()
        if oldest is None or oldest > cutoff:
            return

        existing = await db.execute(
            select(func.count()).select_from(Notification).where(
                Notification.type == "system_health_warning",
                Notification.created_at >= datetime.now(timezone.utc) - timedelta(hours=2),
            )
        )
        if existing.scalar() > 0:
            return

        owner = await _get_owner(db)
        if not owner:
            return

        await create_notification(
            db,
            user_id=owner.id,
            type="system_health_warning",
            title="Worker blockiert",
            body=f"{queued_count} Jobs warten seit über {HEALTH_STALE_MINUTES} Minuten.",
            link="/agenten",
            source_type="system",
        )
        await db.commit()
        logger.warning("Health-Warning: %d queued Jobs seit >%d Min", queued_count, HEALTH_STALE_MINUTES)


# --- Lifecycle ---

async def start_notification_scheduler() -> None:
    global _scheduler_task, _health_task
    _scheduler_task = asyncio.create_task(_due_soon_loop())
    _health_task = asyncio.create_task(_health_check_loop())
    logger.info("Notification-Scheduler gestartet")


async def stop_notification_scheduler() -> None:
    global _scheduler_task, _health_task
    for task in (_scheduler_task, _health_task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _scheduler_task = None
    _health_task = None
    logger.info("Notification-Scheduler gestoppt")

"""Pipeline-Promoter: verschiebt Tasks automatisch in die passende
Agenda-Spalte anhand ihres Due Dates.

Zwei Einsatzarten:
1. **Reaktiv** — beim Aendern von due_date (PATCH) oder bei Instanz-Erstellung
2. **Periodisch** — Hintergrund-Loop alle 60 Minuten fuer zeitbasierten Vorschub
"""

import asyncio
import logging
import uuid
from datetime import date, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import async_session
from app.models import PipelineColumn, Task
from app.core.principal import get_owner_id

logger = logging.getLogger("taskpilot.pipeline_promoter")

PROMOTER_INTERVAL_SECONDS = 3600  # 60 Minuten

_owner_id: uuid.UUID | None = None


async def _get_owner_id(db: AsyncSession) -> uuid.UUID | None:
    """Owner-UUID aus der DB laden (gecacht, da es nur einen Owner gibt)."""
    global _owner_id
    if _owner_id is not None:
        return _owner_id
    _owner_id = await get_owner_id(db)
    return _owner_id

COLUMN_NAME_ORDER = [
    "Focus",
    "This Week",
    "Next Week",
    "This Month",
    "Next Month",
    "Beyond",
]


async def get_pipeline_columns_by_name(
    db: AsyncSession,
) -> dict[str, PipelineColumn]:
    """Laedt alle Pipeline-Spalten und gibt sie als {name: column} zurueck."""
    result = await db.execute(select(PipelineColumn))
    return {col.name: col for col in result.scalars().all()}


def determine_target_column_name(due: date, today: date) -> str:
    """Bestimmt den Spaltennamen anhand des Due Dates relativ zu heute.

    Reine Logik ohne DB-Zugriff — gut testbar.
    """
    if due <= today:
        return "Focus"

    iso_today = today.isocalendar()
    iso_due = due.isocalendar()

    if iso_due[0] == iso_today[0] and iso_due[1] == iso_today[1]:
        return "This Week"

    next_monday = today + timedelta(days=(7 - today.weekday()))
    next_sunday = next_monday + timedelta(days=6)
    if next_monday <= due <= next_sunday:
        return "Next Week"

    if due.year == today.year and due.month == today.month:
        return "This Month"

    next_month = today.month + 1 if today.month < 12 else 1
    next_year = today.year if today.month < 12 else today.year + 1
    if due.year == next_year and due.month == next_month:
        return "Next Month"

    return "Beyond"


async def determine_pipeline_column(
    db: AsyncSession,
    due: date | None,
) -> PipelineColumn | None:
    """Bestimmt die passende Pipeline-Spalte fuer ein gegebenes Due Date.

    Gibt None zurueck wenn kein due_date oder keine passende Spalte existiert.
    """
    if due is None:
        return None
    columns = await get_pipeline_columns_by_name(db)
    target_name = determine_target_column_name(due, date.today())
    if target_name in columns:
        return columns[target_name]
    idx = COLUMN_NAME_ORDER.index(target_name) if target_name in COLUMN_NAME_ORDER else -1
    for fallback_name in COLUMN_NAME_ORDER[idx + 1:]:
        if fallback_name in columns:
            return columns[fallback_name]
    return None


async def place_task_in_pipeline(
    db: AsyncSession,
    task: Task,
    target_column: PipelineColumn,
) -> bool:
    """Setzt pipeline_column_id und pipeline_position auf dem Task.

    Gibt True zurueck wenn eine Aenderung stattfand.
    """
    if task.pipeline_column_id == target_column.id:
        return False

    max_pos_result = await db.execute(
        select(func.coalesce(func.max(Task.pipeline_position), 0.0)).where(
            Task.pipeline_column_id == target_column.id
        )
    )
    task.pipeline_column_id = target_column.id
    task.pipeline_position = (max_pos_result.scalar_one() or 0.0) + 1.0
    return True


async def auto_place_task(db: AsyncSession, task: Task) -> bool:
    """Convenience: bestimmt die Spalte und platziert den Task.

    Fuer reaktives Placement (PATCH, Instanz-Erstellung).
    Verschiebt in beide Richtungen (vor und zurueck).
    """
    if not task.due_date or task.assignee == "agent":
        return False
    owner_id = await _get_owner_id(db)
    if owner_id and task.assignee != str(owner_id):
        return False
    target = await determine_pipeline_column(db, task.due_date)
    if not target:
        return False
    return await place_task_in_pipeline(db, task, target)


async def _promote_tasks(db: AsyncSession) -> int:
    """Prueft alle relevanten Tasks und verschiebt sie nur vorwaerts (Richtung Focus)."""
    owner_id = await _get_owner_id(db)
    if not owner_id:
        return 0

    columns = await get_pipeline_columns_by_name(db)
    today = date.today()

    parked_ids: set[uuid.UUID] = set()
    col_position_map: dict[uuid.UUID, float] = {}
    for col in columns.values():
        col_position_map[col.id] = col.position
        if col.column_type == "parked":
            parked_ids.add(col.id)

    result = await db.execute(
        select(Task).where(
            and_(
                Task.due_date.isnot(None),
                Task.pipeline_column_id.isnot(None),
                Task.is_completed.is_(False),
                Task.assignee == str(owner_id),
                ~and_(Task.recurrence_rule.isnot(None), Task.template_id.is_(None)),
            )
        )
    )
    tasks = result.scalars().all()
    promoted = 0

    for task in tasks:
        if task.pipeline_column_id in parked_ids:
            continue

        target_name = determine_target_column_name(task.due_date, today)
        target_col = columns.get(target_name)
        if not target_col:
            continue

        current_pos = col_position_map.get(task.pipeline_column_id, 0.0)
        if target_col.position >= current_pos:
            continue

        changed = await place_task_in_pipeline(db, task, target_col)
        if changed:
            promoted += 1
            logger.info(
                "Task '%s' (%s) vorgerueckt: -> %s (due=%s)",
                task.title, task.id, target_name, task.due_date,
            )

    return promoted


async def promoter_loop() -> None:
    """Endlosschleife fuer den periodischen Pipeline-Promoter."""
    logger.info("Pipeline-Promoter gestartet (Intervall: %ds)", PROMOTER_INTERVAL_SECONDS)
    while True:
        try:
            async with async_session() as db:
                count = await _promote_tasks(db)
                await db.commit()
                if count:
                    logger.info("%d Task(s) in der Agenda vorgerueckt", count)
        except Exception:
            logger.exception("Pipeline-Promoter: unerwarteter Fehler")
        await asyncio.sleep(PROMOTER_INTERVAL_SECONDS)


_promoter_task: asyncio.Task | None = None


async def start_pipeline_promoter() -> None:
    global _promoter_task
    _promoter_task = asyncio.create_task(promoter_loop())


async def stop_pipeline_promoter() -> None:
    global _promoter_task
    if _promoter_task and not _promoter_task.done():
        _promoter_task.cancel()
        try:
            await _promoter_task
        except asyncio.CancelledError:
            pass
    _promoter_task = None

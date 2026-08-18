import html
import logging
import os
import pathlib
import sys
import uuid
from datetime import date, datetime, timezone

import bleach
from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from typing import Literal

from app.auth.deps import MEMBER_RESTRICTED_TASK_FIELDS, check_project_access, get_current_user, require_role
from app.routers.uploads import _scan_with_clamav
from app.database import get_db
from app.models import ActivityLog, AgentJob, Attachment, BoardColumn, BoardMember, ChatTriage, ChecklistItem, EmailTriage, FollowupSuggestion, MeetingTranscript, PipelineColumn, Project, Task, User
from app.services.email_links import outlook_deeplink
from app.services.notification import notify_mentions, notify_task_assigned

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402
from app.config import get_settings  # noqa: E402

logger = logging.getLogger("taskpilot.tasks")
from app.schemas import (
    AssigneeUser,
    ChecklistItemCreate,
    ChecklistItemOut,
    ChecklistItemUpdate,
    TaskCreate,
    TaskOut,
    TaskReorderBody,
    TaskUpdate,
)


async def _resolve_task_origin(
    db: AsyncSession, task: Task
) -> tuple[bool, uuid.UUID | None, str | None]:
    """Erkennt, ob ein Task vom Agenten stammt, und löst Job-ID + Absender auf.

    Agent-stammend = aus E-Mail abgeleitet (``email_message_id``), als Vorschlag
    markiert (``needs_review``) oder dem Agenten zugewiesen. Best-effort.
    """
    is_agent = (
        bool(task.email_message_id)
        or bool(getattr(task, "needs_review", False))
        or task.assignee == "agent"
    )
    if not is_agent:
        return False, None, None
    job_id: uuid.UUID | None = None
    sender: str | None = None
    if task.email_message_id:
        try:
            row = await db.execute(
                select(EmailTriage.agent_job_id, EmailTriage.from_address)
                .where(EmailTriage.message_id == task.email_message_id)
                .limit(1)
            )
            r = row.first()
            if r:
                job_id, sender = r[0], r[1]
        except Exception:  # noqa: BLE001 - best-effort
            logger.warning("Task-Origin konnte nicht aufgelöst werden")
    return True, job_id, sender


def _get_email_client() -> GraphClient | None:
    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        return None
    return GraphClient(GraphConfig(
        tenant_id=s.graph_tenant_id,
        client_id=s.graph_client_id,
        client_secret=s.graph_client_secret,
        user_email=s.graph_user_email,
    ))


async def _archive_source_email(email_message_id: str | None) -> None:
    """Archiviert die Quell-Mail in Outlook (best-effort)."""
    if not email_message_id:
        return
    client = _get_email_client()
    if not client:
        return
    try:
        await client.archive_email(email_message_id)
        logger.info("Quell-Mail %s archiviert", email_message_id)
    except Exception:
        logger.warning("Quell-Mail %s konnte nicht archiviert werden", email_message_id)
    finally:
        await client.close()


async def _resolve_assignee_user(assignee: str, db: AsyncSession) -> AssigneeUser | None:
    """Löst eine assignee-UUID in ein AssigneeUser-Objekt auf."""
    if not assignee or assignee == "agent":
        return None
    try:
        uid = uuid.UUID(assignee)
    except ValueError:
        return None
    result = await db.execute(select(User).where(User.id == uid))
    u = result.scalar_one_or_none()
    if not u:
        return None
    return AssigneeUser(id=u.id, display_name=u.display_name, avatar_url=u.avatar_url)


def _resolve_assignee_input(assignee: str | None, user: User) -> str | None:
    """Wandelt 'me' in die User-UUID um."""
    if assignee == "me":
        return str(user.id)
    return assignee


async def _validate_assignee(
    assignee: str, project_id: uuid.UUID, db: AsyncSession,
) -> None:
    """Stellt sicher, dass ein UUID-Assignee BoardMember oder Owner ist."""
    if assignee in ("agent", "me"):
        return
    try:
        uid = uuid.UUID(assignee)
    except ValueError:
        raise HTTPException(status_code=400, detail="Ungültiger Assignee-Wert")
    owner_result = await db.execute(
        select(User).where(User.id == uid, User.role == "owner")
    )
    if owner_result.scalar_one_or_none() is not None:
        return
    result = await db.execute(
        select(BoardMember).where(
            BoardMember.project_id == project_id,
            BoardMember.user_id == uid,
        )
    )
    if result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=400,
            detail="Zugewiesene Person ist kein Mitglied dieses Projekts",
        )

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _sanitize_text(text: str | None) -> str | None:
    if text is None:
        return None
    # Tags entfernen (XSS-Schutz), dann HTML-Entities wieder zu literalen
    # Zeichen auflösen, damit z.B. "&", "<", ">" erhalten bleiben statt als
    # "&amp;" etc. gespeichert zu werden. Das Frontend escaped beim Rendern
    # selbst (Titel via React, Beschreibung/Kommentar via Markdown).
    return html.unescape(bleach.clean(text, tags=[], strip=True))


def _validate_cron(rule: str | None) -> None:
    """Wirft 422, wenn die Wiederholungsregel keine gültige Cron-Expression ist.

    Ohne diese Prüfung landet eine kaputte Regel stillschweigend in der DB und
    der Scheduler überspringt die Serie dauerhaft — sichtbar nur im Log.
    """
    if rule and not croniter.is_valid(rule):
        raise HTTPException(
            status_code=422,
            detail=f"Ungültige Wiederholungsregel (Cron-Expression): '{rule}'",
        )


@router.post("", response_model=TaskOut, status_code=status.HTTP_201_CREATED)
async def create_task(
    body: TaskCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> TaskOut:
    if user.role != "owner":
        if hasattr(body, "assignee") and body.assignee == "agent":
            raise HTTPException(status_code=403, detail="Agent-Zuweisung ist nicht erlaubt")
        for field in MEMBER_RESTRICTED_TASK_FIELDS:
            if hasattr(body, field):
                setattr(body, field, None)

    _validate_cron(body.recurrence_rule)

    # Vorlagen-Härtung: auf einer Vorlage steuert die Cron-Regel die Termine,
    # ein eigenes Fälligkeitsdatum führt nur zu falschem «überfällig».
    if body.recurrence_rule:
        body.due_date = None

    col_result = await db.execute(
        select(BoardColumn.project_id).where(BoardColumn.id == body.board_column_id)
    )
    project_id = col_result.scalar_one_or_none()
    if project_id and not await check_project_access(project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")

    if body.board_position is None:
        max_result = await db.execute(
            select(Task.board_position)
            .where(Task.board_column_id == body.board_column_id)
            .order_by(Task.board_position.desc())
            .limit(1)
        )
        max_pos = max_result.scalar_one_or_none() or 0.0
        body.board_position = max_pos + 1.0

    if body.pipeline_column_id and body.pipeline_position is None:
        max_result = await db.execute(
            select(Task.pipeline_position)
            .where(Task.pipeline_column_id == body.pipeline_column_id)
            .order_by(Task.pipeline_position.desc())
            .limit(1)
        )
        max_pos = max_result.scalar_one_or_none() or 0.0
        body.pipeline_position = max_pos + 1.0

    body.title = _sanitize_text(body.title) or body.title
    body.description = _sanitize_text(body.description)
    body.assignee = _resolve_assignee_input(body.assignee, user) or body.assignee
    if project_id:
        await _validate_assignee(body.assignee, project_id, db)

    task = Task(**body.model_dump())
    db.add(task)
    await db.flush()

    # Agent-Delegation direkt bei der Erstellung: gleicher 'planned'-Pfad wie
    # beim PATCH (Zuweisung entkoppelt Ausführung; Freigabe via "Jetzt
    # ausführen" oder Agent-Scheduler am Fälligkeitstag). Vorher erzeugte nur
    # der Umweg über PATCH einen Job -- POST mit assignee='agent' lief leer.
    if task.assignee == "agent" and user.role == "owner":
        db.add(AgentJob(
            user_id=user.id,
            task_id=task.id,
            job_type="task",
            status="planned",
            llm_model=task.llm_override,
            metadata_json={
                "autonomy_level": task.autonomy_level,
                "data_class": task.data_class,
                "llm_override": task.llm_override,
            },
        ))

    if task.due_date and task.assignee != "agent":
        from app.services.pipeline_promoter import auto_place_task
        await auto_place_task(db, task)

    result = await db.execute(
        select(Task)
        .options(selectinload(Task.tags), selectinload(Task.checklist_items))
        .where(Task.id == task.id)
    )
    task_obj = result.scalar_one()
    task_out = TaskOut.model_validate(task_obj)
    task_out.assignee_user = await _resolve_assignee_user(task_obj.assignee, db)
    return task_out


@router.post("/reorder", status_code=status.HTTP_204_NO_CONTENT)
async def reorder_tasks(
    body: TaskReorderBody,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> None:
    """Schreibt die Reihenfolge ganzer Spalten in einem Zug.

    Nach einem Drag & Drop sendet das Frontend die vollständige Reihenfolge der
    Zielspalte (bei einem Spaltenwechsel zusätzlich die Herkunftsspalte). Die
    Positionen werden mit 1..N neu durchnummeriert, statt nur den gezogenen Task
    zu aktualisieren. So können keine doppelten Positionswerte entstehen, und
    Altbestände mit kollidierenden Werten heilen sich beim ersten Drop selbst.
    """
    is_pipeline = body.scope == "pipeline"

    # pipeline_column_id/-position stehen in MEMBER_RESTRICTED_TASK_FIELDS --
    # die Agenda ist persönliches Owner-Territorium.
    if is_pipeline and user.role != "owner":
        raise HTTPException(
            status_code=403,
            detail="Nur der Owner darf die Agenda-Reihenfolge ändern",
        )

    for group in body.columns:
        if not group.task_ids:
            continue
        if len(set(group.task_ids)) != len(group.task_ids):
            raise HTTPException(
                status_code=422, detail="Doppelte Task-IDs in der Reihenfolge"
            )

        project_id: uuid.UUID | None = None
        if is_pipeline:
            column_exists = await db.scalar(
                select(PipelineColumn.id).where(PipelineColumn.id == group.column_id)
            )
            if column_exists is None:
                raise HTTPException(status_code=404, detail="Agenda-Spalte nicht gefunden")
        else:
            project_id = await db.scalar(
                select(BoardColumn.project_id).where(BoardColumn.id == group.column_id)
            )
            if project_id is None:
                raise HTTPException(status_code=404, detail="Board-Spalte nicht gefunden")
            if not await check_project_access(project_id, user, db):
                raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")

        result = await db.execute(select(Task).where(Task.id.in_(group.task_ids)))
        tasks_by_id = {t.id: t for t in result.scalars().all()}
        if len(tasks_by_id) != len(group.task_ids):
            raise HTTPException(status_code=404, detail="Mindestens ein Task existiert nicht")

        # Board-Reorder verschiebt nie über Projektgrenzen -- sonst könnte ein
        # Member fremde Tasks in sein eigenes Board ziehen.
        if not is_pipeline:
            for task in tasks_by_id.values():
                if task.project_id != project_id:
                    raise HTTPException(
                        status_code=403, detail="Task gehört nicht zu diesem Projekt"
                    )

        # Auch die Tasks laden, die das Frontend nicht kennt (erledigte,
        # wiederkehrende Vorlagen). Sie werden hinter der übergebenen
        # Reihenfolge einsortiert, damit die ganze Spalte kollisionsfrei
        # durchnummeriert ist und nicht nur ihr sichtbarer Teil.
        if is_pipeline:
            column_filter = Task.pipeline_column_id == group.column_id
            position_col = Task.pipeline_position
        else:
            column_filter = Task.board_column_id == group.column_id
            position_col = Task.board_position
        hidden_result = await db.execute(
            select(Task)
            .where(column_filter, Task.id.notin_(group.task_ids))
            .order_by(position_col.nulls_last(), Task.created_at)
        )

        ordered = [tasks_by_id[tid] for tid in group.task_ids]
        ordered.extend(hidden_result.scalars().all())

        for index, task in enumerate(ordered, start=1):
            if is_pipeline:
                task.pipeline_column_id = group.column_id
                task.pipeline_position = float(index)
            else:
                task.board_column_id = group.column_id
                task.board_position = float(index)


# --- Pending Review (auto-erstellte Tasks aus E-Mail-Triage) ---

class PendingReviewOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    project_id: uuid.UUID
    project_name: str
    board_column_id: uuid.UUID
    pipeline_column_id: uuid.UUID | None
    due_date: str | None
    email_message_id: str | None
    email_conversation_id: str | None = None
    source_email_subject: str | None = None
    source_email_from: str | None = None
    meeting_transcript_id: uuid.UUID | None = None
    meeting_subject: str | None = None
    source: str | None = None  # 'followup' | 'meeting' | None (Herkunfts-Badge im Cockpit)
    created_at: str


class TaskConfirmBody(BaseModel):
    title: str | None = None
    project_id: uuid.UUID | None = None
    board_column_id: uuid.UUID | None = None


class RecurringSeriesOut(BaseModel):
    """Eine wiederkehrende Vorlage samt Serien-Status für die Serien-Übersicht."""

    id: uuid.UUID
    title: str
    project_id: uuid.UUID
    project_name: str
    project_color: str | None = None
    board_column_id: uuid.UUID
    assignee: str
    recurrence_rule: str
    recurrence_description: str
    recurrence_end_date: str | None = None
    recurrence_max_instances: int | None = None
    last_spawn: str | None = None
    next_occurrence: str | None = None
    instance_count: int = 0
    open_instance_id: uuid.UUID | None = None
    open_instance_due_date: str | None = None
    is_valid: bool = True


@router.get("/due-today")
async def list_due_today(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
):
    today = date.today()
    stmt = (
        select(Task)
        .where(
            Task.is_completed.is_(False),
            Task.due_date.isnot(None),
            Task.due_date <= today,
            # Wiederkehrende Vorlagen ausschliessen — Fälligkeit gilt nur für Instanzen.
            ~and_(Task.recurrence_rule.isnot(None), Task.template_id.is_(None)),
        )
        .options(selectinload(Task.tags))
        .order_by(Task.due_date)
        .limit(20)
    )
    result = await db.execute(stmt)
    tasks = result.scalars().all()

    project_ids = {t.project_id for t in tasks if t.project_id}
    project_names: dict[str, str] = {}
    if project_ids:
        pstmt = select(Project).where(Project.id.in_(project_ids))
        presult = await db.execute(pstmt)
        for p in presult.scalars().all():
            project_names[str(p.id)] = p.name

    return [
        {
            "id": str(t.id),
            "title": t.title,
            "project_id": str(t.project_id),
            "project_name": project_names.get(str(t.project_id), ""),
            "board_column_id": str(t.board_column_id),
            "board_position": t.board_position,
            "pipeline_column_id": str(t.pipeline_column_id) if t.pipeline_column_id else None,
            "pipeline_position": t.pipeline_position,
            "assignee": t.assignee,
            "due_date": t.due_date,
            "is_completed": t.is_completed,
            "is_pinned": t.is_pinned,
            "recurrence_rule": t.recurrence_rule,
            "template_id": str(t.template_id) if t.template_id else None,
            "tags": [{"id": str(tag.id), "name": tag.name, "color": tag.color} for tag in t.tags],
            "checklist_total": 0,
            "checklist_done": 0,
        }
        for t in tasks
    ]


@router.get("/pending-review", response_model=list[PendingReviewOut])
async def list_pending_review(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> list[PendingReviewOut]:
    """Tasks mit needs_review=True laden (auto-erstellte Task-Vorschlaege)."""
    result = await db.execute(
        select(
            Task, Project.name,
            EmailTriage.subject, EmailTriage.from_name, EmailTriage.from_address,
            FollowupSuggestion.id,
            MeetingTranscript.subject,
        )
        .join(Project, Task.project_id == Project.id)
        .outerjoin(EmailTriage, Task.email_message_id == EmailTriage.message_id)
        .outerjoin(FollowupSuggestion, FollowupSuggestion.task_id == Task.id)
        .outerjoin(MeetingTranscript, Task.meeting_transcript_id == MeetingTranscript.id)
        .where(Task.needs_review == True)  # noqa: E712
        .order_by(Task.created_at.desc())
    )
    rows = result.all()

    def _source(followup_id, transcript_subject, transcript_id) -> str | None:
        if followup_id:
            return "followup"
        if transcript_id is not None:
            return "meeting"
        return None

    return [
        PendingReviewOut(
            id=task.id,
            title=task.title,
            description=task.description,
            project_id=task.project_id,
            project_name=proj_name,
            board_column_id=task.board_column_id,
            pipeline_column_id=task.pipeline_column_id,
            due_date=task.due_date.isoformat() if task.due_date else None,
            email_message_id=task.email_message_id,
            email_conversation_id=task.email_conversation_id,
            source_email_subject=email_subject,
            source_email_from=email_from_name or email_from_addr,
            meeting_transcript_id=task.meeting_transcript_id,
            meeting_subject=meeting_subject,
            source=_source(followup_id, meeting_subject, task.meeting_transcript_id),
            created_at=task.created_at.isoformat(),
        )
        for task, proj_name, email_subject, email_from_name, email_from_addr, followup_id, meeting_subject in rows
    ]


@router.get("/recurring", response_model=list[RecurringSeriesOut])
async def list_recurring_series(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> list[RecurringSeriesOut]:
    """Alle wiederkehrenden Vorlagen mit nächstem Termin und Serien-Status.

    Muss vor der ``/{task_id}``-Route stehen, sonst schluckt der UUID-Matcher
    den Pfad und liefert 422.
    """
    result = await db.execute(
        select(Task, Project.name, Project.color)
        .join(Project, Task.project_id == Project.id)
        .where(
            Task.recurrence_rule.isnot(None),
            Task.recurrence_rule != "",
            Task.template_id.is_(None),
        )
        .order_by(Project.name, Task.title)
    )
    rows = result.all()

    now = datetime.now(timezone.utc)
    series: list[RecurringSeriesOut] = []

    for task, project_name, project_color in rows:
        if not await check_project_access(task.project_id, user, db):
            continue

        is_valid = croniter.is_valid(task.recurrence_rule)
        next_occurrence: str | None = None
        if is_valid:
            next_occurrence = croniter(task.recurrence_rule, now).get_next(datetime).isoformat()

        count_result = await db.execute(
            select(func.count()).select_from(Task).where(Task.template_id == task.id)
        )
        instance_count = count_result.scalar_one() or 0

        open_result = await db.execute(
            select(Task.id, Task.due_date)
            .where(Task.template_id == task.id, Task.is_completed == False)  # noqa: E712
            .order_by(Task.due_date.desc())
            .limit(1)
        )
        open_row = open_result.first()

        series.append(RecurringSeriesOut(
            id=task.id,
            title=task.title,
            project_id=task.project_id,
            project_name=project_name,
            project_color=project_color,
            board_column_id=task.board_column_id,
            assignee=task.assignee,
            recurrence_rule=task.recurrence_rule,
            recurrence_description=(
                _cron_to_human(task.recurrence_rule)
                if is_valid
                else "Ungültige Wiederholungsregel"
            ),
            recurrence_end_date=(
                task.recurrence_end_date.isoformat() if task.recurrence_end_date else None
            ),
            recurrence_max_instances=task.recurrence_max_instances,
            last_spawn=(
                task.recurrence_last_spawn.isoformat() if task.recurrence_last_spawn else None
            ),
            next_occurrence=next_occurrence,
            instance_count=instance_count,
            open_instance_id=open_row[0] if open_row else None,
            open_instance_due_date=(
                open_row[1].isoformat() if open_row and open_row[1] else None
            ),
            is_valid=is_valid,
        ))

    return series


@router.get("/{task_id}", response_model=TaskOut)
async def get_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> TaskOut:
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.tags), selectinload(Task.checklist_items))
        .where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not await check_project_access(task.project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")
    task_out = TaskOut.model_validate(task)
    task_out.assignee_user = await _resolve_assignee_user(task.assignee, db)
    if task.email_message_id:
        task_out.source_email_web_link = outlook_deeplink(task.email_message_id)
        et_result = await db.execute(
            select(EmailTriage.subject, EmailTriage.from_name, EmailTriage.from_address)
            .where(EmailTriage.message_id == task.email_message_id)
        )
        et_row = et_result.one_or_none()
        if et_row:
            task_out.source_email_subject = et_row.subject
            task_out.source_email_from = et_row.from_name or et_row.from_address
    if task.meeting_transcript_id:
        mt_subject = await db.scalar(
            select(MeetingTranscript.subject).where(MeetingTranscript.id == task.meeting_transcript_id)
        )
        task_out.meeting_subject = mt_subject
    return task_out


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(
    task_id: uuid.UUID,
    body: TaskUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> TaskOut:
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.tags), selectinload(Task.checklist_items))
        .where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if not await check_project_access(task.project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")

    old_assignee = task.assignee
    old_project_id = task.project_id
    update_data = body.model_dump(exclude_unset=True)

    if "assignee" in update_data:
        update_data["assignee"] = _resolve_assignee_input(update_data["assignee"], user)
        await _validate_assignee(update_data["assignee"], task.project_id, db)

    if user.role != "owner":
        for field in MEMBER_RESTRICTED_TASK_FIELDS:
            update_data.pop(field, None)
        if update_data.get("assignee") == "agent":
            raise HTTPException(status_code=403, detail="Agent-Zuweisung ist nicht erlaubt")

    if "title" in update_data:
        update_data["title"] = _sanitize_text(update_data["title"]) or update_data["title"]
    if "description" in update_data:
        update_data["description"] = _sanitize_text(update_data["description"])

    # Wiederkehrende Vorlagen: is_completed und due_date sind auf Vorlagen
    # bedeutungslos (der Scheduler steuert alles über Cron-Regel + Instanzen)
    # und stiften nur Verwirrung (falsches «überfällig» im Board/Briefing).
    is_template = bool(task.recurrence_rule) and task.template_id is None
    if is_template and update_data.get("is_completed"):
        raise HTTPException(
            status_code=422,
            detail=(
                "Wiederkehrende Vorlagen können nicht als erledigt markiert "
                "werden — erledige stattdessen die aktuelle Instanz."
            ),
        )
    if "recurrence_rule" in update_data:
        _validate_cron(update_data["recurrence_rule"])

    becomes_template = (
        bool(update_data.get("recurrence_rule", task.recurrence_rule))
        and task.template_id is None
    )
    if becomes_template:
        if update_data.get("recurrence_rule"):
            # Task wird (neu) zur Vorlage oder Regel ändert: Fälligkeit neutralisieren.
            update_data["due_date"] = None
            # Bei geänderter Kadenz startet der Scheduler neu: der alte Merker
            # gehört zur alten Regel und würde die nächste Okkurrenz verzögern.
            if update_data["recurrence_rule"] != task.recurrence_rule:
                update_data["recurrence_last_spawn"] = None
        else:
            # Bestehende Vorlage: due_date-Änderungen ignorieren.
            update_data.pop("due_date", None)

    # Eiserne Regel: E-Mail-stämmige Tasks sind externe Kommunikation und bleiben
    # serverseitig immer auf Freigabe (L1) -- höhere Autonomie wird hart gekappt.
    if update_data.get("autonomy_level") in ("L2", "L3"):
        is_external = bool(
            update_data.get("email_message_id", task.email_message_id)
            or update_data.get("email_conversation_id", task.email_conversation_id)
        )
        if is_external:
            update_data["autonomy_level"] = "L1"

    for field, value in update_data.items():
        setattr(task, field, value)

    if "due_date" in update_data and task.assignee != "agent" and task.pipeline_column_id:
        from app.services.pipeline_promoter import auto_place_task
        await auto_place_task(db, task)

    # Implizites Lernsignal: agent-stammender Task in anderes Projekt verschoben.
    if "project_id" in update_data and task.project_id != old_project_id:
        try:
            is_agent, job_id, sender = await _resolve_task_origin(db, task)
            if is_agent:
                from app.services.learning import record_feedback

                await record_feedback(
                    db,
                    feedback_type="task_moved",
                    agent_job_id=job_id,
                    sender_email=sender,
                    source="cockpit",
                    original={"project_id": str(old_project_id)},
                    corrected={"project_id": str(task.project_id)},
                )
        except Exception:  # noqa: BLE001 - best-effort
            logger.warning("task_moved-Signal konnte nicht erfasst werden")

    if body.assignee == "agent" and old_assignee != "agent" and user.role == "owner":
        # Zuweisung entkoppelt Ausführung: der Job wird 'planned' (wartet), nicht
        # 'queued'. So rennt der Agent nicht sofort los -- die Task kann fertig
        # konfiguriert und dann via "Jetzt ausführen" freigegeben werden (oder der
        # Scheduler löst am Fälligkeitstag aus). Autonomie/LLM/Datenklasse als
        # Snapshot fürs spätere Enforcement; die Beschreibung lädt der Worker zur
        # Laufzeit frisch aus der Task (voller Kontext inkl. Checkliste/Anhänge).
        job = AgentJob(
            user_id=user.id,
            task_id=task.id,
            job_type="task",
            status="planned",
            llm_model=task.llm_override,
            metadata_json={
                "autonomy_level": task.autonomy_level,
                "data_class": task.data_class,
                "llm_override": task.llm_override,
            },
        )
        db.add(job)

    new_assignee = task.assignee
    if new_assignee != old_assignee and new_assignee not in ("agent", "me"):
        try:
            new_uid = uuid.UUID(new_assignee)
            if new_uid != user.id:
                await notify_task_assigned(db, task, new_uid, user.email)
        except ValueError:
            pass

    task_out = TaskOut.model_validate(task)
    task_out.assignee_user = await _resolve_assignee_user(task.assignee, db)
    return task_out


async def _detach_agent_job_references(db: AsyncSession, task_ids: list[uuid.UUID]) -> None:
    """Löst FK-Verweise auf die Agent-Jobs der genannten Tasks.

    ``agent_jobs.task_id`` kaskadiert beim Task-Löschen, aber ``email_triage``,
    ``chat_triage`` und ``meeting_transcripts`` referenzieren ``agent_jobs`` ohne
    ``ON DELETE`` — ohne dieses Aufräumen bricht das Löschen mit IntegrityError.
    """
    if not task_ids:
        return
    job_ids = (
        await db.execute(select(AgentJob.id).where(AgentJob.task_id.in_(task_ids)))
    ).scalars().all()
    if not job_ids:
        return
    for model in (EmailTriage, ChatTriage, MeetingTranscript):
        await db.execute(
            sa_update(model)
            .where(model.agent_job_id.in_(job_ids))
            .values(agent_job_id=None)
        )


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_task(
    task_id: uuid.UUID,
    series: Literal["template_only", "all"] = Query(
        "template_only",
        description=(
            "Nur bei wiederkehrenden Vorlagen relevant: 'template_only' beendet "
            "die Serie und behält die erzeugten Instanzen als normale Tasks, "
            "'all' löscht Vorlage und Instanzen."
        ),
    ),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> None:
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not await check_project_access(task.project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")

    # Wiederkehrende Vorlage: die Instanzen hängen per FK an der Vorlage und
    # müssen vor dem Löschen entweder entkoppelt oder mitgelöscht werden.
    is_template = bool(task.recurrence_rule) and task.template_id is None
    if is_template:
        instance_ids = (
            await db.execute(select(Task.id).where(Task.template_id == task.id))
        ).scalars().all()
        if series == "all":
            await _detach_agent_job_references(db, list(instance_ids))
            if instance_ids:
                await db.execute(sa_delete(Task).where(Task.id.in_(instance_ids)))
        else:
            await db.execute(
                sa_update(Task)
                .where(Task.template_id == task.id)
                .values(template_id=None)
            )
        await db.flush()
    await _detach_agent_job_references(db, [task.id])

    # Implizites Lernsignal: Löschen eines agent-stammenden Tasks = stille Korrektur.
    try:
        is_agent, job_id, sender = await _resolve_task_origin(db, task)
        if is_agent:
            from app.services.learning import mark_episode_corrected, record_feedback

            await record_feedback(
                db,
                feedback_type="task_deleted",
                agent_job_id=job_id,
                sender_email=sender,
                source="cockpit",
                original={"title": task.title, "project_id": str(task.project_id)},
            )
            if job_id:
                await mark_episode_corrected(
                    db, agent_job_id=job_id, lesson="Abgeleiteter Task wurde gelöscht"
                )
    except Exception:  # noqa: BLE001 - best-effort, darf Löschen nie blockieren
        logger.warning("task_deleted-Signal konnte nicht erfasst werden")

    await db.delete(task)


@router.post("/{task_id}/confirm", response_model=TaskOut)
async def confirm_review_task(
    task_id: uuid.UUID,
    body: TaskConfirmBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> TaskOut:
    """Task-Vorschlag bestätigen (setzt needs_review=False, erlaubt Änderungen)."""
    result = await db.execute(
        select(Task)
        .options(selectinload(Task.tags), selectinload(Task.checklist_items))
        .where(Task.id == task_id)
    )
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.needs_review:
        raise HTTPException(status_code=409, detail="Task ist bereits bestaetigt")

    task.needs_review = False

    if body.title is not None:
        task.title = body.title
    if body.project_id is not None:
        task.project_id = body.project_id
        if body.board_column_id is not None:
            task.board_column_id = body.board_column_id
        else:
            col_result = await db.execute(
                select(BoardColumn)
                .where(BoardColumn.project_id == body.project_id)
                .order_by(BoardColumn.position)
                .limit(1)
            )
            first_col = col_result.scalar_one_or_none()
            if first_col:
                task.board_column_id = first_col.id

    await _archive_source_email(task.email_message_id)

    return task


@router.post("/{task_id}/dismiss-review", status_code=status.HTTP_204_NO_CONTENT)
async def dismiss_review_task(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> None:
    """Task-Vorschlag verwerfen (Task löschen)."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.needs_review:
        raise HTTPException(status_code=409, detail="Nur unbestaetigte Vorschlaege koennen verworfen werden")

    # Lernsignal: Ein verworfener Agent-Vorschlag ist eine stille Korrektur.
    try:
        is_agent, job_id, sender = await _resolve_task_origin(db, task)
        if is_agent:
            from app.services.learning import mark_episode_corrected, record_feedback

            await record_feedback(
                db,
                feedback_type="task_deleted",
                agent_job_id=job_id,
                sender_email=sender,
                source="cockpit",
                original={"title": task.title, "project_id": str(task.project_id)},
                reason="Task-Vorschlag verworfen (dismiss-review)",
            )
            if job_id:
                await mark_episode_corrected(
                    db, agent_job_id=job_id, lesson="Abgeleiteter Task-Vorschlag verworfen"
                )
    except Exception:  # noqa: BLE001 - best-effort
        logger.warning("dismiss-review-Signal konnte nicht erfasst werden")

    # Verwerfen-Entscheid auf der Quell-Triage festschreiben: Ohne diesen Marker
    # erzeugte die nächste praktisch identische E-Mail (z. B. n8n-Fehler-Burst)
    # denselben Vorschlag erneut, weil der Dedupe nur OFFENE Tasks prüft.
    if task.email_message_id:
        try:
            triage_row = (
                await db.execute(
                    select(EmailTriage).where(EmailTriage.message_id == task.email_message_id)
                )
            ).scalar_one_or_none()
            if triage_row is not None:
                action = dict(triage_row.suggested_action or {})
                action["task_dismissed"] = True
                triage_row.suggested_action = action
        except Exception:  # noqa: BLE001 - best-effort
            logger.warning("dismiss-review: Triage-Marker konnte nicht gesetzt werden")

    await db.delete(task)


# --- Checklist ---

@router.get("/{task_id}/checklist", response_model=list[ChecklistItemOut])
async def list_checklist(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> list[ChecklistItemOut]:
    result = await db.execute(
        select(ChecklistItem)
        .where(ChecklistItem.task_id == task_id)
        .order_by(ChecklistItem.position)
    )
    return result.scalars().all()


@router.post("/{task_id}/checklist", response_model=ChecklistItemOut, status_code=201)
async def add_checklist_item(
    task_id: uuid.UUID,
    body: ChecklistItemCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> ChecklistItemOut:
    if body.position is None:
        max_result = await db.execute(
            select(ChecklistItem.position)
            .where(ChecklistItem.task_id == task_id)
            .order_by(ChecklistItem.position.desc())
            .limit(1)
        )
        max_pos = max_result.scalar_one_or_none() or 0.0
        body.position = max_pos + 1.0

    body.text = _sanitize_text(body.text) or body.text
    item = ChecklistItem(task_id=task_id, **body.model_dump())
    db.add(item)
    await db.flush()
    return item


@router.patch("/{task_id}/checklist/{item_id}", response_model=ChecklistItemOut)
async def update_checklist_item(
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    body: ChecklistItemUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> ChecklistItemOut:
    result = await db.execute(
        select(ChecklistItem)
        .where(ChecklistItem.id == item_id, ChecklistItem.task_id == task_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Checklist item not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        if field == "text" and value is not None:
            value = _sanitize_text(value) or value
        setattr(item, field, value)
    return item


@router.delete("/{task_id}/checklist/{item_id}", status_code=204)
async def delete_checklist_item(
    task_id: uuid.UUID,
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> None:
    result = await db.execute(
        select(ChecklistItem)
        .where(ChecklistItem.id == item_id, ChecklistItem.task_id == task_id)
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Checklist item not found")
    await db.delete(item)


@router.get("/{task_id}/recurrence")
async def get_recurrence_info(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> dict:
    """Gibt Wiederholungsinformationen zurück: nächstes Auftreten,
    letzte Instanz und menschenlesbarer Cron-Text."""
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if not task.recurrence_rule:
        return {
            "recurrence_rule": None,
            "next_occurrence": None,
            "description": None,
            "last_spawn": None,
        }

    if not croniter.is_valid(task.recurrence_rule):
        return {
            "recurrence_rule": task.recurrence_rule,
            "next_occurrence": None,
            "description": "Ungültige Cron-Expression",
            "last_spawn": (
                task.recurrence_last_spawn.isoformat()
                if task.recurrence_last_spawn
                else None
            ),
        }

    now = datetime.now(timezone.utc)
    cron = croniter(task.recurrence_rule, now)
    next_run = cron.get_next(datetime)

    description = _cron_to_human(task.recurrence_rule)

    return {
        "recurrence_rule": task.recurrence_rule,
        "next_occurrence": next_run.isoformat(),
        "description": description,
        "last_spawn": (
            task.recurrence_last_spawn.isoformat()
            if task.recurrence_last_spawn
            else None
        ),
    }


def _cron_to_human(cron_expr: str) -> str:
    """Konvertiert gängige Cron-Ausdrücke in lesbaren deutschen Text."""
    presets = {
        "0 0 * * *": "Täglich um Mitternacht",
        "0 7 * * *": "Täglich um 07:00",
        "0 8 * * *": "Täglich um 08:00",
        "0 9 * * *": "Täglich um 09:00",
        "0 7 * * MON": "Jeden Montag um 07:00",
        "0 7 * * 1": "Jeden Montag um 07:00",
        "0 8 * * MON": "Jeden Montag um 08:00",
        "0 8 * * 1": "Jeden Montag um 08:00",
        "0 9 * * MON": "Jeden Montag um 09:00",
        "0 9 * * 1": "Jeden Montag um 09:00",
        "0 7 * * MON-FRI": "Werktags um 07:00",
        "0 7 * * 1-5": "Werktags um 07:00",
        "0 8 1 * *": "Monatlich am 1. um 08:00",
        "0 9 1 * *": "Monatlich am 1. um 09:00",
        "0 8 15 * *": "Monatlich am 15. um 08:00",
    }
    if cron_expr in presets:
        return presets[cron_expr]

    parts = cron_expr.split()
    if len(parts) != 5:
        return cron_expr

    minute, hour, dom, month, dow = parts

    time_str = ""
    if hour != "*" and minute != "*":
        time_str = f" um {hour.zfill(2)}:{minute.zfill(2)}"

    if dom == "*" and dow == "*":
        return f"Täglich{time_str}"
    if dom == "*" and dow != "*":
        day_names = {
            "0": "Sonntag", "SUN": "Sonntag",
            "1": "Montag", "MON": "Montag",
            "2": "Dienstag", "TUE": "Dienstag",
            "3": "Mittwoch", "WED": "Mittwoch",
            "4": "Donnerstag", "THU": "Donnerstag",
            "5": "Freitag", "FRI": "Freitag",
            "6": "Samstag", "SAT": "Samstag",
        }
        day = day_names.get(dow.upper(), dow)
        return f"Jeden {day}{time_str}"
    if dow == "*" and dom != "*":
        # 'L' = letzter Tag des Monats (vom Recurrence-Selector im Frontend erzeugt).
        if dom.upper() == "L":
            return f"Monatlich am letzten Tag{time_str}"
        return f"Monatlich am {dom}.{time_str}"

    return cron_expr


# --- Activity Log ---

class ActivityLogOut(BaseModel):
    id: str
    task_id: str
    event_type: str
    actor: str
    details: dict | None
    created_at: str

class CommentCreate(BaseModel):
    text: str

class CommentUpdate(BaseModel):
    text: str

@router.get("/{task_id}/activity", response_model=list[ActivityLogOut])
async def list_activity(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> list[ActivityLogOut]:
    result = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.task_id == task_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(50)
    )
    logs = result.scalars().all()
    return [
        ActivityLogOut(
            id=str(log.id),
            task_id=str(log.task_id),
            event_type=log.event_type,
            actor=log.actor,
            details=log.details,
            created_at=log.created_at.isoformat(),
        )
        for log in logs
    ]


@router.post("/{task_id}/activity", response_model=ActivityLogOut, status_code=201)
async def add_comment(
    task_id: uuid.UUID,
    body: CommentCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> ActivityLogOut:
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    log = ActivityLog(
        task_id=task_id,
        event_type="comment",
        actor=user.email,
        details={"text": body.text},
    )
    db.add(log)
    await db.flush()

    await notify_mentions(db, body.text, task_id, task.title, user.email, user.id)

    return ActivityLogOut(
        id=str(log.id),
        task_id=str(log.task_id),
        event_type=log.event_type,
        actor=log.actor,
        details=log.details,
        created_at=log.created_at.isoformat(),
    )


@router.patch("/{task_id}/activity/{activity_id}", response_model=ActivityLogOut)
async def update_comment(
    task_id: uuid.UUID,
    activity_id: uuid.UUID,
    body: CommentUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> ActivityLogOut:
    result = await db.execute(select(Task).where(Task.id == task_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Task not found")

    result = await db.execute(
        select(ActivityLog).where(
            ActivityLog.id == activity_id,
            ActivityLog.task_id == task_id,
        )
    )
    log = result.scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=404, detail="Aktivitätseintrag nicht gefunden")
    if log.event_type != "comment":
        raise HTTPException(status_code=400, detail="Nur Kommentare können bearbeitet werden")
    if user.role != "owner" and log.actor != user.email:
        raise HTTPException(status_code=403, detail="Nur der Autor oder Owner darf Kommentare bearbeiten")

    sanitized = _sanitize_text(body.text) or body.text
    log.details = {"text": sanitized}
    return ActivityLogOut(
        id=str(log.id),
        task_id=str(log.task_id),
        event_type=log.event_type,
        actor=log.actor,
        details=log.details,
        created_at=log.created_at.isoformat(),
    )


@router.delete("/{task_id}/activity/{activity_id}")
async def delete_comment(
    task_id: uuid.UUID,
    activity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> dict:
    result = await db.execute(select(Task).where(Task.id == task_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Task not found")

    result = await db.execute(
        select(ActivityLog).where(
            ActivityLog.id == activity_id,
            ActivityLog.task_id == task_id,
        )
    )
    log = result.scalar_one_or_none()
    if log is None:
        raise HTTPException(status_code=404, detail="Aktivitätseintrag nicht gefunden")
    if log.event_type != "comment":
        raise HTTPException(status_code=400, detail="Nur Kommentare können gelöscht werden")
    if user.role != "owner" and log.actor != user.email:
        raise HTTPException(status_code=403, detail="Nur der Autor oder Owner darf Kommentare löschen")

    await db.delete(log)
    return {"ok": True}


# --- Attachments ---

TASK_UPLOADS_DIR = pathlib.Path(__file__).resolve().parent.parent.parent / "uploads" / "tasks"
MAX_ATTACHMENT_SIZE = 10 * 1024 * 1024  # 10 MB

class AttachmentOut(BaseModel):
    id: str
    task_id: str
    filename: str
    filepath: str
    mime_type: str | None
    size: int
    uploaded_at: str


@router.get("/{task_id}/attachments", response_model=list[AttachmentOut])
async def list_attachments(
    task_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> list[AttachmentOut]:
    result = await db.execute(
        select(Attachment)
        .where(Attachment.task_id == task_id)
        .order_by(Attachment.uploaded_at.desc())
    )
    attachments = result.scalars().all()
    base = pathlib.Path(__file__).resolve().parent.parent.parent / "uploads"
    out = []
    for a in attachments:
        full_path = base / a.filepath.lstrip("/").removeprefix("uploads/")
        size = full_path.stat().st_size if full_path.exists() else 0
        out.append(AttachmentOut(
            id=str(a.id), task_id=str(a.task_id), filename=a.filename,
            filepath=a.filepath, mime_type=a.mime_type, size=size,
            uploaded_at=a.uploaded_at.isoformat(),
        ))
    return out


@router.post("/{task_id}/attachments", response_model=AttachmentOut, status_code=201)
async def upload_attachment(
    task_id: uuid.UUID,
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> AttachmentOut:
    result = await db.execute(select(Task).where(Task.id == task_id))
    task = result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if not await check_project_access(task.project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")

    data = await file.read()
    if len(data) > MAX_ATTACHMENT_SIZE:
        raise HTTPException(status_code=400, detail="Datei zu gross (max 10 MB)")

    if not await _scan_with_clamav(data):
        raise HTTPException(status_code=422, detail="Datei wurde als schädlich erkannt")

    task_dir = TASK_UPLOADS_DIR / str(task_id)
    task_dir.mkdir(parents=True, exist_ok=True)

    safe_name = (file.filename or "datei").replace("/", "_").replace("\\", "_")
    ext = pathlib.Path(safe_name).suffix
    stored_name = f"{uuid.uuid4().hex}{ext}"
    dest = task_dir / stored_name
    dest.write_bytes(data)

    relative_path = f"/uploads/tasks/{task_id}/{stored_name}"
    attachment = Attachment(
        task_id=task_id,
        filename=safe_name,
        filepath=relative_path,
        mime_type=file.content_type,
    )
    db.add(attachment)
    await db.flush()

    return AttachmentOut(
        id=str(attachment.id), task_id=str(attachment.task_id),
        filename=attachment.filename, filepath=attachment.filepath,
        mime_type=attachment.mime_type, size=len(data),
        uploaded_at=attachment.uploaded_at.isoformat(),
    )


@router.delete("/{task_id}/attachments/{attachment_id}", status_code=204)
async def delete_attachment(
    task_id: uuid.UUID,
    attachment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("member")),
) -> None:
    result = await db.execute(
        select(Attachment)
        .where(Attachment.id == attachment_id, Attachment.task_id == task_id)
    )
    attachment = result.scalar_one_or_none()
    if attachment is None:
        raise HTTPException(status_code=404, detail="Attachment not found")

    if not attachment.filepath.startswith("onedrive://"):
        base = pathlib.Path(__file__).resolve().parent.parent.parent / "uploads"
        full_path = base / attachment.filepath.lstrip("/").removeprefix("uploads/")
        if full_path.exists():
            full_path.unlink()

    await db.delete(attachment)


class OneDriveAttachBody(BaseModel):
    item_id: str
    name: str


@router.post("/{task_id}/attachments/onedrive", response_model=AttachmentOut, status_code=201)
async def add_onedrive_attachment(
    task_id: uuid.UUID,
    body: OneDriveAttachBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> AttachmentOut:
    """Speichert eine OneDrive-Dateireferenz als Attachment (keine lokale Kopie)."""
    task = (await db.execute(select(Task).where(Task.id == task_id))).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    mime_ext = body.name.rsplit(".", 1)[-1].lower() if "." in body.name else None
    mime_map = {
        "pdf": "application/pdf", "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "txt": "text/plain", "csv": "text/csv", "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    }
    mime_type = mime_map.get(mime_ext or "", "application/octet-stream")

    attachment = Attachment(
        task_id=task_id,
        filename=body.name,
        filepath=f"onedrive://{body.item_id}",
        mime_type=mime_type,
    )
    db.add(attachment)
    await db.flush()

    return AttachmentOut(
        id=str(attachment.id), task_id=str(attachment.task_id),
        filename=attachment.filename, filepath=attachment.filepath,
        mime_type=attachment.mime_type, size=0,
        uploaded_at=attachment.uploaded_at.isoformat(),
    )

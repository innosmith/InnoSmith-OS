import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import and_, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth.deps import check_project_access, get_current_user, require_role
from app.database import get_db
from app.models import BoardColumn, BoardMember, PipelineColumn, Project, Task, User
from app.schemas import (
    AssigneeUser,
    BoardColumnCreate,
    BoardColumnOut,
    BoardColumnUpdate,
    BoardColumnWithTasks,
    BoardOut,
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    ProjectWithColumns,
    TaskCard,
)

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectWithColumns])
async def list_projects(
    include_archived: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> list[ProjectWithColumns]:
    q = select(Project).options(selectinload(Project.board_columns))
    if not include_archived:
        q = q.where(Project.status != "archived")
    if user.role != "owner":
        q = q.where(
            Project.id.in_(
                select(BoardMember.project_id).where(BoardMember.user_id == user.id)
            )
        )
    result = await db.execute(q.order_by(Project.priority.desc(), Project.name))
    return result.scalars().all()


@router.post("", response_model=ProjectOut, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("owner")),
) -> ProjectOut:
    project = Project(**body.model_dump())
    db.add(project)
    await db.flush()

    db.add(BoardMember(project_id=project.id, user_id=user.id, role="member"))

    return project


@router.get("/{project_id}", response_model=ProjectWithColumns)
async def get_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> ProjectWithColumns:
    if not await check_project_access(project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.board_columns))
        .where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.patch("/{project_id}", response_model=ProjectOut)
async def update_project(
    project_id: uuid.UUID,
    body: ProjectUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> ProjectOut:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(project, field, value)
    return project


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> None:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.delete(project)


@router.get("/{project_id}/board", response_model=BoardOut)
async def get_board(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> BoardOut:
    if not await check_project_access(project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")
    result = await db.execute(
        select(Project)
        .options(selectinload(Project.board_columns))
        .where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    user_cache: dict[str, AssigneeUser] = {}
    all_users = (await db.execute(select(User))).scalars().all()
    for u in all_users:
        user_cache[str(u.id)] = AssigneeUser(id=u.id, display_name=u.display_name, avatar_url=u.avatar_url)
        if u.role == "owner":
            user_cache["me"] = user_cache[str(u.id)]

    pipe_cols = (await db.execute(select(PipelineColumn))).scalars().all()
    pipe_map = {pc.id: pc for pc in pipe_cols}

    columns_out = []
    for col in sorted(project.board_columns, key=lambda c: c.position):
        task_result = await db.execute(
            select(Task)
            .options(selectinload(Task.tags), selectinload(Task.checklist_items))
            .where(
                Task.board_column_id == col.id,
                Task.is_completed == False,  # noqa: E712
                Task.template_id.is_(None),
            )
            .order_by(Task.board_position)
        )
        tasks = task_result.scalars().all()

        task_cards = []
        for t in tasks:
            pc = pipe_map.get(t.pipeline_column_id) if t.pipeline_column_id else None
            card = TaskCard(
                id=t.id,
                title=t.title,
                project_id=t.project_id,
                board_column_id=t.board_column_id,
                board_position=t.board_position,
                pipeline_column_id=t.pipeline_column_id,
                pipeline_position=t.pipeline_position,
                assignee=t.assignee,
                assignee_user=user_cache.get(t.assignee),
                due_date=t.due_date,
                is_completed=t.is_completed,
                is_pinned=t.is_pinned,
                tags=t.tags,
                checklist_total=len(t.checklist_items),
                checklist_done=sum(1 for ci in t.checklist_items if ci.is_checked),
                recurrence_rule=t.recurrence_rule,
                template_id=t.template_id,
                pipeline_column_name=pc.name if pc else None,
                pipeline_column_color=pc.color if pc else None,
            )
            task_cards.append(card)

        columns_out.append(BoardColumnWithTasks(
            id=col.id,
            name=col.name,
            color=col.color,
            icon_emoji=col.icon_emoji,
            position=col.position,
            is_archive=col.is_archive,
            tasks=task_cards,
        ))

    return BoardOut(project=project, columns=columns_out)


@router.post("/{project_id}/columns", response_model=BoardColumnCreate, status_code=201)
async def create_column(
    project_id: uuid.UUID,
    body: BoardColumnCreate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> BoardColumn:
    result = await db.execute(select(Project).where(Project.id == project_id))
    if result.scalar_one_or_none() is None:
        raise HTTPException(status_code=404, detail="Project not found")

    if body.position is None:
        max_result = await db.execute(
            select(BoardColumn.position)
            .where(BoardColumn.project_id == project_id)
            .order_by(BoardColumn.position.desc())
            .limit(1)
        )
        max_pos = max_result.scalar_one_or_none() or 0.0
        body.position = max_pos + 1.0

    col = BoardColumn(project_id=project_id, **body.model_dump())
    db.add(col)
    await db.flush()
    return col


@router.patch("/{project_id}/columns/{col_id}", response_model=BoardColumnOut)
async def update_column(
    project_id: uuid.UUID,
    col_id: uuid.UUID,
    body: BoardColumnUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> BoardColumn:
    result = await db.execute(
        select(BoardColumn).where(BoardColumn.id == col_id, BoardColumn.project_id == project_id)
    )
    col = result.scalar_one_or_none()
    if col is None:
        raise HTTPException(status_code=404, detail="Column not found")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(col, field, value)
    return col


@router.delete("/{project_id}/columns/{col_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_column(
    project_id: uuid.UUID,
    col_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> None:
    result = await db.execute(
        select(BoardColumn).where(BoardColumn.id == col_id, BoardColumn.project_id == project_id)
    )
    col = result.scalar_one_or_none()
    if col is None:
        raise HTTPException(status_code=404, detail="Column not found")

    # Ziel-Spalte bestimmen: erste verbleibende Spalte des Projekts nach Position.
    target_result = await db.execute(
        select(BoardColumn)
        .where(BoardColumn.project_id == project_id, BoardColumn.id != col_id)
        .order_by(BoardColumn.position)
        .limit(1)
    )
    target = target_result.scalar_one_or_none()
    if target is None:
        raise HTTPException(
            status_code=409,
            detail="Die letzte Spalte eines Projekts kann nicht gelöscht werden.",
        )

    # Alle Tasks der Spalte (inkl. ausgeblendete erledigte/wiederkehrende) in die Ziel-Spalte
    # umhängen. board_position mit Offset anheben, damit keine Kollisionen entstehen und die
    # bisherige Reihenfolge erhalten bleibt. Sonst blockiert der NOT-NULL-FK das Löschen.
    max_pos_result = await db.execute(
        select(func.max(Task.board_position)).where(Task.board_column_id == target.id)
    )
    offset = (max_pos_result.scalar_one_or_none() or 0.0) + 1.0
    await db.execute(
        update(Task)
        .where(Task.board_column_id == col_id)
        .values(
            board_column_id=target.id,
            board_position=Task.board_position + offset,
        )
    )

    await db.delete(col)


class ProjectMetrics(BaseModel):
    id: uuid.UUID
    name: str
    color: str
    icon_url: str | None = None
    icon_emoji: str | None = None
    status: str
    total_tasks: int
    open_tasks: int
    completed_tasks: int
    overdue_tasks: int
    progress_pct: float


@router.get("/overview/metrics", response_model=list[ProjectMetrics])
async def project_metrics(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> list[ProjectMetrics]:
    projects = (
        await db.execute(
            select(Project).order_by(Project.priority.desc(), Project.name)
        )
    ).scalars().all()

    result = []
    today = date.today()
    # Wiederkehrende Vorlagen zählen nicht als Aufgaben — nur ihre Instanzen.
    not_template = ~and_(Task.recurrence_rule.isnot(None), Task.template_id.is_(None))
    for p in projects:
        counts = await db.execute(
            select(
                func.count(Task.id).label("total"),
                func.count(Task.id).filter(Task.is_completed == False).label("open"),
                func.count(Task.id).filter(Task.is_completed == True).label("done"),
                func.count(Task.id).filter(
                    Task.is_completed == False,
                    Task.due_date != None,
                    Task.due_date < today,
                ).label("overdue"),
            ).where(Task.project_id == p.id, not_template)
        )
        row = counts.one()
        total = row.total or 0
        done = row.done or 0
        result.append(
            ProjectMetrics(
                id=p.id, name=p.name, color=p.color, status=p.status,
                icon_url=p.icon_url, icon_emoji=p.icon_emoji,
                total_tasks=total, open_tasks=row.open or 0,
                completed_tasks=done, overdue_tasks=row.overdue or 0,
                progress_pct=round((done / total * 100) if total > 0 else 0, 1),
            )
        )
    return result


# ---------------------------------------------------------------------------
# Mitglieder-Verwaltung
# ---------------------------------------------------------------------------


class MemberOut(BaseModel):
    user_id: uuid.UUID
    email: str
    display_name: str
    avatar_url: str | None = None
    role: str
    invited_at: datetime | None = None


@router.get("/{project_id}/members", response_model=list[MemberOut])
async def list_members(
    project_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("member")),
) -> list[MemberOut]:
    if not await check_project_access(project_id, user, db):
        raise HTTPException(status_code=403, detail="Kein Zugriff auf dieses Projekt")

    result = await db.execute(
        select(BoardMember, User)
        .join(User, BoardMember.user_id == User.id)
        .where(BoardMember.project_id == project_id)
        .order_by(User.display_name)
    )
    rows = result.all()
    return [
        MemberOut(
            user_id=u.id,
            email=u.email,
            display_name=u.display_name,
            avatar_url=u.avatar_url,
            role=u.role if u.role == "owner" else bm.role,
            invited_at=bm.invited_at,
        )
        for bm, u in rows
    ]


@router.delete(
    "/{project_id}/members/{member_user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def remove_member(
    project_id: uuid.UUID,
    member_user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> None:
    result = await db.execute(
        select(BoardMember).where(
            BoardMember.project_id == project_id,
            BoardMember.user_id == member_user_id,
        )
    )
    bm = result.scalar_one_or_none()
    if bm is None:
        raise HTTPException(status_code=404, detail="Mitglied nicht gefunden")

    await db.delete(bm)

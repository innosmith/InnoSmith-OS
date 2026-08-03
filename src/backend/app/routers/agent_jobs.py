import logging
import uuid
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import httpx

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user, require_role
from app.database import get_db
from app.models import AgentJob, ChatTriage, EmailTriage, Task, User
from app.schemas import AgentJobCreate, AgentJobOut, AgentJobUpdate, AgentJobWithTask
from app.services.learning import (
    capture_draft_feedback,
    mark_episode_corrected,
    record_feedback,
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402
from app.config import get_settings  # noqa: E402

logger = logging.getLogger("taskpilot.agent_jobs")

router = APIRouter(prefix="/api/agent-jobs", tags=["agent-jobs"])


# ── Stats ─────────────────────────────────────────────────────

class AiJobBreakdown(BaseModel):
    triage: int
    drafts: int
    suggestions: int
    other: int


class AiStatsResponse(BaseModel):
    pending_decisions: int
    completed_week: int
    completed_month: int
    breakdown_week: AiJobBreakdown


@router.get("/stats", response_model=AiStatsResponse)
async def get_ai_stats(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> AiStatsResponse:
    """Aggregierte AI-Job-Statistiken für das Cockpit."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Zurich")
    now = datetime.now(tz)

    week_start = (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    pending_jobs = await db.execute(
        select(func.count()).where(AgentJob.status == "awaiting_approval").select_from(AgentJob)
    )
    pending_tasks = await db.execute(
        select(func.count()).where(Task.needs_review == True).select_from(Task)  # noqa: E712
    )
    pending_decisions = (pending_jobs.scalar_one() or 0) + (pending_tasks.scalar_one() or 0)

    week_result = await db.execute(
        select(func.count()).where(
            and_(AgentJob.status == "completed", AgentJob.completed_at >= week_start)
        ).select_from(AgentJob)
    )
    completed_week = week_result.scalar_one() or 0

    month_result = await db.execute(
        select(func.count()).where(
            and_(AgentJob.status == "completed", AgentJob.completed_at >= month_start)
        ).select_from(AgentJob)
    )
    completed_month = month_result.scalar_one() or 0

    triage_result = await db.execute(
        select(func.count()).where(
            and_(
                AgentJob.status == "completed",
                AgentJob.completed_at >= week_start,
                AgentJob.job_type.in_(["email_triage", "chat_triage"]),
            )
        ).select_from(AgentJob)
    )
    triage_count = triage_result.scalar_one() or 0

    drafts_result = await db.execute(
        select(func.count()).where(
            and_(
                AgentJob.status == "completed",
                AgentJob.completed_at >= week_start,
                AgentJob.job_type == "send_email",
            )
        ).select_from(AgentJob)
    )
    drafts_count = drafts_result.scalar_one() or 0

    suggestions_result = await db.execute(
        select(func.count()).where(Task.needs_review == True).select_from(Task)  # noqa: E712
    )
    suggestions_count = suggestions_result.scalar_one() or 0

    other_count = completed_week - triage_count - drafts_count

    return AiStatsResponse(
        pending_decisions=pending_decisions,
        completed_week=completed_week,
        completed_month=completed_month,
        breakdown_week=AiJobBreakdown(
            triage=triage_count,
            drafts=drafts_count,
            suggestions=suggestions_count,
            other=max(0, other_count),
        ),
    )


def _get_email_client() -> GraphClient | None:
    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        return None
    config = GraphConfig(
        tenant_id=s.graph_tenant_id,
        client_id=s.graph_client_id,
        client_secret=s.graph_client_secret,
        user_email=s.graph_user_email,
    )
    return GraphClient(config)


def _extract_draft_id(job: AgentJob) -> str | None:
    """Extrahiert die draft_id aus Metadata oder LLM-Output."""
    meta = job.metadata_json or {}
    draft_id = meta.get("draft_id")
    if draft_id:
        return draft_id

    if not job.output:
        return None

    json_match = re.search(r"```json\s*\n(.*?)\n\s*```", job.output, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(1))
            draft_id = parsed.get("draft_id")
            if draft_id:
                return draft_id
        except json.JSONDecodeError:
            pass

    id_match = re.search(r"AAMk[A-Za-z0-9_-]{20,}", job.output)
    if id_match:
        return id_match.group(0)

    return None


@router.get("", response_model=list[AgentJobWithTask])
async def list_agent_jobs(
    status: str | None = None,
    task_id: uuid.UUID | None = None,
    job_type: str | None = Query(None, description="Job-Typ oder kommagetrennte Liste (z.B. daily_briefing,weekly_briefing)"),
    limit: int = Query(100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("owner")),
) -> list[AgentJobWithTask]:
    query = (
        select(AgentJob, Task.title)
        .outerjoin(Task, AgentJob.task_id == Task.id)
        .where(AgentJob.user_id == user.id)
    )
    if status:
        query = query.where(AgentJob.status == status)
    if task_id:
        query = query.where(AgentJob.task_id == task_id)
    if job_type:
        types = [t.strip() for t in job_type.split(",") if t.strip()]
        if types:
            query = query.where(AgentJob.job_type.in_(types))
    query = query.order_by(AgentJob.created_at.desc()).limit(limit)

    result = await db.execute(query)
    rows = result.all()
    return [
        AgentJobWithTask.model_validate({**job.__dict__, "task_title": title})
        for job, title in rows
    ]


@router.get("/{job_id}", response_model=AgentJobWithTask)
async def get_agent_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> AgentJobWithTask:
    result = await db.execute(
        select(AgentJob, Task.title)
        .outerjoin(Task, AgentJob.task_id == Task.id)
        .where(AgentJob.id == job_id)
    )
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Agent job not found")
    job, title = row
    return AgentJobWithTask.model_validate({**job.__dict__, "task_title": title})


@router.get("/{job_id}/draft-preview")
async def get_draft_preview(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> dict:
    """Lädt den Draft-Inhalt aus Outlook für die Vorschau im Approval-Flow."""
    result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")

    draft_id = _extract_draft_id(job)
    if not draft_id:
        raise HTTPException(status_code=404, detail="Kein Entwurf mit diesem Job verknüpft")

    client = _get_email_client()
    if not client:
        raise HTTPException(status_code=503, detail="Graph API nicht konfiguriert")

    meta = job.metadata_json or {}
    try:
        draft = await client.get_email(draft_id)
        to_list = [
            r.get("emailAddress", {}).get("address", "")
            for r in draft.get("toRecipients", [])
        ]
        cc_list = [
            r.get("emailAddress", {}).get("address", "")
            for r in draft.get("ccRecipients", [])
        ]
        return {
            "draft_id": draft_id,
            "subject": draft.get("subject"),
            "body_html": draft.get("body", {}).get("content"),
            "body_preview": draft.get("bodyPreview"),
            "to_recipients": to_list,
            "cc_recipients": cc_list,
            "source_subject": meta.get("subject"),
            "source_from": meta.get("from_address"),
            "conversation_id": meta.get("conversation_id"),
        }
    except httpx.HTTPStatusError as e:
        # NUR ein echtes 404 schliesst den Job ab (Entwurf wirklich gesendet/
        # geloescht). Bei transienten Fehlern (5xx, Drosselung) bleibt die Freigabe
        # erhalten -- das blosse Oeffnen des Cockpits darf einen gueltigen Entwurf
        # niemals zerstoeren.
        if e.response.status_code == 404 and job.status == "awaiting_approval":
            job.status = "completed"
            job.output = (job.output or "") + "\n\n--- Entwurf wurde in Outlook gesendet oder gelöscht. Job automatisch abgeschlossen. ---"
            job.completed_at = datetime.now(timezone.utc)
            await db.commit()
            logger.info("Draft-Preview: Job %s abgeschlossen (Draft 404)", job_id)
            raise HTTPException(status_code=404, detail="Entwurf wurde in Outlook gesendet oder gelöscht")
        logger.warning("Draft-Preview: transienter Graph-Fehler (%s) für Job %s", e.response.status_code, job_id)
        raise HTTPException(status_code=502, detail="Draft konnte nicht geladen werden (vorübergehender Fehler)")
    except Exception:
        # Unklarer Fehler -> Job NICHT abschliessen, transient behandeln.
        logger.exception("Draft konnte nicht geladen werden für Job %s", job_id)
        raise HTTPException(status_code=502, detail="Draft konnte nicht geladen werden (vorübergehender Fehler)")
    finally:
        await client.close()


class DraftUpdateBody(BaseModel):
    subject: str | None = None
    body_html: str | None = None
    to_recipients: list[str] | None = None
    cc_recipients: list[str] | None = None


@router.patch("/{job_id}/draft")
async def update_draft(
    job_id: uuid.UUID,
    body: DraftUpdateBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> dict:
    """Entwurf in Outlook aktualisieren (Betreff, Body, Empfaenger)."""
    result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")

    draft_id = _extract_draft_id(job)
    if not draft_id:
        raise HTTPException(status_code=404, detail="Kein Entwurf mit diesem Job verknüpft")

    client = _get_email_client()
    if not client:
        raise HTTPException(status_code=503, detail="Graph API nicht konfiguriert")

    try:
        await client.update_draft(
            message_id=draft_id,
            subject=body.subject,
            body_html=body.body_html,
            to_recipients=body.to_recipients,
            cc_recipients=body.cc_recipients,
        )
        return {"ok": True, "draft_id": draft_id}
    except Exception as e:
        logger.exception("Draft konnte nicht aktualisiert werden für Job %s", job_id)
        raise HTTPException(status_code=502, detail="Draft konnte nicht aktualisiert werden")
    finally:
        await client.close()


@router.post("", response_model=AgentJobOut, status_code=201)
async def create_agent_job(
    body: AgentJobCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_role("owner")),
) -> AgentJobOut:
    task_result = await db.execute(select(Task).where(Task.id == body.task_id))
    task = task_result.scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    job = AgentJob(user_id=user.id, task_id=body.task_id, llm_model=body.llm_model)
    db.add(job)
    await db.flush()
    return job


@router.patch("/{job_id}", response_model=AgentJobOut)
async def update_agent_job(
    job_id: uuid.UUID,
    body: AgentJobUpdate,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> AgentJobOut:
    result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")

    old_status = job.status

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(job, field, value)

    if body.status == "running" and job.started_at is None:
        job.started_at = datetime.now(timezone.utc)
    if body.status in ("completed", "failed") and job.completed_at is None:
        job.completed_at = datetime.now(timezone.utc)

    if (
        old_status == "awaiting_approval"
        and body.status == "completed"
        and job.job_type in ("send_email", "email_triage")
    ):
        draft_id = _extract_draft_id(job)
        if draft_id:
            client = _get_email_client()
            if client:
                try:
                    # Lernsignal VOR dem Versand erfassen: Der Entwurf in Outlook
                    # spiegelt jetzt die finale, ggf. editierte Fassung wider.
                    try:
                        draft_msg = await client.get_email(draft_id)
                        body_obj = draft_msg.get("body", {}) or {}
                        sent_html = (
                            body_obj.get("content")
                            if body_obj.get("contentType") == "html"
                            else draft_msg.get("bodyPreview")
                        )
                        recipient = next(
                            (
                                r.get("emailAddress", {}).get("address", "")
                                for r in draft_msg.get("toRecipients", [])
                            ),
                            None,
                        )
                        await capture_draft_feedback(
                            db,
                            draft_id=draft_id,
                            sent_html=sent_html,
                            recipient=recipient,
                            source="cockpit",
                        )
                    except Exception:  # noqa: BLE001 - Capture darf Versand nicht blockieren
                        logger.warning("Draft-Feedback-Capture fehlgeschlagen (job %s)", job_id)
                    await client.send_draft(draft_id)
                    job.output = (job.output or "") + "\n\nE-Mail erfolgreich gesendet."
                    source_email_id = (job.metadata_json or {}).get("email_message_id")
                    if source_email_id:
                        try:
                            await client.archive_email(source_email_id)
                            job.output += " Quell-Mail archiviert."
                        except Exception:
                            logger.warning("Quell-Mail %s konnte nicht archiviert werden", source_email_id)
                except Exception as e:
                    job.status = "failed"
                    job.error_message = str(e)
                finally:
                    await client.close()

    if (
        old_status == "awaiting_approval"
        and body.status == "failed"
        and job.job_type in ("send_email", "email_triage")
    ):
        # Lernsignal: der Berater hat den Entwurf abgelehnt.
        meta = job.metadata_json or {}
        await record_feedback(
            db,
            feedback_type="rejected",
            agent_job_id=job.id,
            sender_email=meta.get("from_address"),
            source="cockpit",
        )
        await mark_episode_corrected(db, agent_job_id=job.id, lesson="Entwurf abgelehnt")
        draft_id = _extract_draft_id(job)
        if draft_id:
            client = _get_email_client()
            if client:
                try:
                    await client.delete_message(draft_id)
                    job.output = (job.output or "") + "\n\nEntwurf gelöscht."
                except Exception:
                    pass
                finally:
                    await client.close()

    # Nutzer-Entscheid festschreiben: Ohne diesen Schritt blieb die zugehörige
    # email_triage-Zeile bei Parser-Fehlschlägen auf (class=NULL, status=pending)
    # hängen -- der Resweep reihte die Mail dann erneut ein und die bereits
    # bestätigte/abgelehnte Freigabe tauchte im Cockpit wieder auf.
    if (
        old_status == "awaiting_approval"
        and body.status in ("completed", "failed")
        and job.job_type in ("send_email", "email_triage")
    ):
        from sqlalchemy import or_, update as sa_update

        from app.models import EmailTriage

        await db.execute(
            sa_update(EmailTriage)
            .where(
                EmailTriage.agent_job_id == job.id,
                or_(EmailTriage.status == "pending", EmailTriage.triage_class.is_(None)),
            )
            .values(status="acted" if body.status == "completed" else "dismissed")
        )

    return job


class JobFeedbackBody(BaseModel):
    rating: str  # 'up' | 'down'
    reason: str | None = None


@router.post("/{job_id}/feedback", status_code=200)
async def submit_job_feedback(
    job_id: uuid.UUID,
    body: JobFeedbackBody,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> dict:
    """Daumen hoch/runter auf einen Agent-Job -- fliesst als Lernsignal ein."""
    if body.rating not in ("up", "down"):
        raise HTTPException(status_code=400, detail="rating muss 'up' oder 'down' sein")
    result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")

    meta = job.metadata_json or {}
    await record_feedback(
        db,
        feedback_type="thumbs_up" if body.rating == "up" else "thumbs_down",
        agent_job_id=job.id,
        sender_email=meta.get("from_address"),
        source="cockpit",
        reason=body.reason,
    )
    if body.rating == "down":
        await mark_episode_corrected(
            db, agent_job_id=job.id, lesson=body.reason or "Daumen runter",
        )
    return {"ok": True, "rating": body.rating}


_DELETABLE_STATUSES = {"completed", "failed", "awaiting_approval", "planned", "blocked"}


class BulkDeleteResult(BaseModel):
    deleted: int


@router.delete("/bulk", response_model=BulkDeleteResult)
async def bulk_delete_agent_jobs(
    status: str = Query(..., description="Status der zu löschenden Jobs"),
    older_than_days: int | None = Query(None, ge=0, description="Nur Jobs älter als X Tage löschen"),
    job_type: str | None = Query(None, description="Nur Jobs dieses Typs (z.B. email_triage, chat_triage, chat_agent)"),
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> BulkDeleteResult:
    """Bulk-Löschung von abgeschlossenen/fehlgeschlagenen Jobs.
    
    status=stale setzt running-Jobs > 30 Min auf failed und gibt die Anzahl zurück.
    Optional: job_type schränkt auf einen bestimmten Typ ein.
    """
    if status == "stale":
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        count_result = await db.execute(
            select(func.count())
            .where(and_(AgentJob.status == "running", AgentJob.started_at < cutoff))
            .select_from(AgentJob)
        )
        count = count_result.scalar_one()
        if count > 0:
            await db.execute(
                update(AgentJob)
                .where(and_(AgentJob.status == "running", AgentJob.started_at < cutoff))
                .values(
                    status="failed",
                    error_message="Manuell als hängend markiert",
                    completed_at=datetime.now(timezone.utc),
                )
            )
        return BulkDeleteResult(deleted=count)

    if status == "both":
        status_filter = AgentJob.status.in_(list(_DELETABLE_STATUSES))
    elif status in _DELETABLE_STATUSES:
        status_filter = AgentJob.status == status
    else:
        raise HTTPException(status_code=400, detail="status muss 'completed', 'failed', 'both' oder 'stale' sein")

    conditions = [status_filter]
    if job_type:
        # 'briefing' ist ein Sammel-Alias für die drei Briefing-Job-Typen (UI-Filter).
        if job_type == "briefing":
            conditions.append(AgentJob.job_type.in_(["daily_briefing", "weekly_briefing", "monthly_briefing"]))
        else:
            conditions.append(AgentJob.job_type == job_type)
    if older_than_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)
        conditions.append(AgentJob.created_at < cutoff)

    count_result = await db.execute(
        select(func.count()).where(and_(*conditions)).select_from(AgentJob)
    )
    count = count_result.scalar_one()

    if count > 0:
        job_ids_result = await db.execute(
            select(AgentJob.id).where(and_(*conditions))
        )
        job_ids = [row[0] for row in job_ids_result.all()]
        if job_ids:
            await db.execute(
                update(EmailTriage)
                .where(EmailTriage.agent_job_id.in_(job_ids))
                .values(agent_job_id=None)
            )
            await db.execute(
                update(ChatTriage)
                .where(ChatTriage.agent_job_id.in_(job_ids))
                .values(agent_job_id=None)
            )
        await db.execute(delete(AgentJob).where(and_(*conditions)))

    return BulkDeleteResult(deleted=count)


@router.delete("/{job_id}", status_code=204)
async def delete_agent_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> None:
    """Einzelnen Job löschen (completed/failed) oder abbrechen (running/queued)."""
    result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")
    if job.status in _DELETABLE_STATUSES:
        await db.execute(
            update(EmailTriage)
            .where(EmailTriage.agent_job_id == job_id)
            .values(agent_job_id=None)
        )
        await db.execute(
            update(ChatTriage)
            .where(ChatTriage.agent_job_id == job_id)
            .values(agent_job_id=None)
        )
        await db.delete(job)
    elif job.status in ("running", "queued"):
        job.status = "failed"
        job.error_message = "Manuell abgebrochen"
        job.completed_at = datetime.now(timezone.utc)
    else:
        raise HTTPException(
            status_code=409,
            detail=f"Job im Status '{job.status}' kann nicht gelöscht/abgebrochen werden",
        )


@router.post("/{job_id}/run", response_model=AgentJobOut)
async def run_agent_job(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> AgentJobOut:
    """Gibt einen geplanten Agent-Job frei: ``planned`` -> ``queued``.

    Erst hierdurch greift der Worker den Job auf -- die Zuweisung allein startet
    nichts. Die Steuerungs-Felder (Autonomie/LLM/Datenklasse) werden aus der
    aktuellen Task neu übernommen, damit zwischenzeitliche Konfiguration greift.
    L0 (Block) wird nicht ausgeführt.
    """
    result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")
    if job.status not in ("planned", "blocked"):
        raise HTTPException(
            status_code=409,
            detail=f"Job im Status '{job.status}' kann nicht gestartet werden",
        )

    meta = dict(job.metadata_json or {})
    if job.task_id:
        task = (
            await db.execute(select(Task).where(Task.id == job.task_id))
        ).scalar_one_or_none()
        if task is not None:
            meta["autonomy_level"] = task.autonomy_level
            meta["data_class"] = task.data_class
            meta["llm_override"] = task.llm_override
            job.llm_model = task.llm_override

    if meta.get("autonomy_level") == "L0":
        # L0 = Block: bleibt 'planned', wird nicht ausgeführt. Klare Rückmeldung.
        raise HTTPException(
            status_code=409,
            detail="Autonomie L0 (Block): Ausführung ist gesperrt. Hebe die Autonomie an, um zu starten.",
        )

    job.metadata_json = meta
    job.status = "queued"
    job.error_message = None
    await db.flush()
    await db.refresh(job)
    return AgentJobOut.model_validate(job)


@router.get("/{job_id}/trace")
async def get_agent_job_trace(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_role("owner")),
) -> dict:
    """Trace eines Agent-Jobs: Reasoning (Thinking), Tool-Aufrufe, Ergebnisse.

    Quelle ist der vom Hermes-Worker in ``metadata_json['trace']`` gespeicherte
    Event-Stream (Typen: ``thinking``, ``tool_start``, ``tool_complete``). So
    bleibt die volle Transparenz erhalten, ohne externe Session-Dateien.
    """
    result = await db.execute(select(AgentJob).where(AgentJob.id == job_id))
    job = result.scalar_one_or_none()
    if job is None:
        raise HTTPException(status_code=404, detail="Agent job not found")

    meta = job.metadata_json or {}
    trace = meta.get("trace") or []

    if not trace:
        return {
            "job_id": str(job_id),
            "status": job.status,
            "session_found": False,
            "steps": [],
        }

    steps: list[dict] = []
    for ev in trace:
        etype = ev.get("type")
        # Der Pass ordnet das Event einem der Laeufe zu (classify/gather/draft).
        # Ohne ihn steht im Cockpit eine flache Event-Liste, in der die Recherche
        # nicht vom Schreiben zu unterscheiden ist.
        step_pass = ev.get("pass")
        if etype == "thinking":
            steps.append({"type": "reasoning", "text": str(ev.get("text", ""))[:1000], "pass": step_pass})
        elif etype == "tool_start":
            steps.append({"type": "tool_call", "tool": ev.get("name", "?"), "pass": step_pass})
        elif etype == "tool_complete":
            preview = str(ev.get("result", ""))
            is_error = "Fehler" in preview[:80] or "Error" in preview[:80]
            steps.append({
                "type": "tool_result",
                "tool_name": ev.get("name", "?"),
                "chars": len(preview),
                "is_error": is_error,
                "preview": preview[:300],
                "pass": step_pass,
            })

    if job.output:
        steps.append({"type": "assistant_message", "text": str(job.output)[:800]})

    duration_s = None
    if job.started_at and job.completed_at:
        try:
            duration_s = round((job.completed_at - job.started_at).total_seconds(), 1)
        except Exception:
            pass

    tool_calls = [s for s in steps if s["type"] == "tool_call"]
    tool_results = [s for s in steps if s["type"] == "tool_result"]
    errors = [s for s in tool_results if s.get("is_error")]

    return {
        "job_id": str(job_id),
        "status": job.status,
        "session_found": True,
        "duration_seconds": duration_s,
        "summary": {
            "total_tool_calls": len(tool_calls),
            "total_tool_results": len(tool_results),
            "errors": len(errors),
            "tools_used": list(dict.fromkeys(tc["tool"] for tc in tool_calls)),
        },
        "steps": steps,
    }

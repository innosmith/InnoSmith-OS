"""FastAPI-Router für E-Mail-Triage: Triage-Vorschläge anzeigen, Aktionen ausführen."""

import sys
import os
import uuid
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func, case, and_
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.deps import get_current_user
from app.config import get_settings
from app.database import get_db
from app.models import AgentJob, EmailTriage, User
from app.schemas import EmailTriageOut, EmailTriageUpdate
from app.services.learning import mark_episode_corrected, record_feedback
from app.services.triage import run_triage_now
from app.services.triage_labels import TRIAGE_LABELS, normalize_label

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logger = logging.getLogger("taskpilot.triage.router")
router = APIRouter(prefix="/api/triage", tags=["triage"])

_graph_client: GraphClient | None = None


def _get_graph_client() -> GraphClient:
    global _graph_client
    if _graph_client is None:
        settings = get_settings()
        config = GraphConfig(
            tenant_id=settings.graph_tenant_id,
            client_id=settings.graph_client_id,
            client_secret=settings.graph_client_secret,
            user_email=settings.graph_user_email,
        )
        _graph_client = GraphClient(config)
    return _graph_client


def _require_owner(user: User) -> None:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner dürfen Triage-Daten sehen")


@router.get("", response_model=list[EmailTriageOut])
async def list_triage_items(
    status_filter: str | None = Query(None, alias="status"),
    triage_class: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[EmailTriageOut]:
    _require_owner(user)
    query = (
        select(EmailTriage)
        .where(EmailTriage.user_id == user.id)
        .order_by(EmailTriage.created_at.desc())
    )
    if status_filter:
        query = query.where(EmailTriage.status == status_filter)
    if triage_class:
        query = query.where(EmailTriage.triage_class == triage_class)
    query = query.limit(limit).offset(offset)
    result = await db.execute(query)
    return list(result.scalars().all())


@router.get("/stats")
async def triage_stats(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    _require_owner(user)
    result = await db.execute(
        select(EmailTriage.status, func.count(EmailTriage.id))
        .where(EmailTriage.user_id == user.id)
        .group_by(EmailTriage.status)
    )
    by_status = {row[0]: row[1] for row in result.all()}

    result2 = await db.execute(
        select(EmailTriage.triage_class, func.count(EmailTriage.id))
        .where(EmailTriage.status == "pending", EmailTriage.user_id == user.id)
        .group_by(EmailTriage.triage_class)
    )
    by_class = {row[0] or "unclassified": row[1] for row in result2.all()}

    # Fällige Follow-ups: Vorschläge, deren needs_review-Task noch offen ist.
    # Ist die Erkennung abgeschaltet, wird nicht gezählt — der Zähler in der Inbox
    # würde sonst Altbestand melden, zu dem nichts mehr nachkommt.
    from app.models import FollowupSuggestion, Task
    from app.services.followup import is_followup_enabled

    followups_due = 0
    if await is_followup_enabled(db):
        followups_due = (
            await db.execute(
                select(func.count(Task.id))
                .join(FollowupSuggestion, FollowupSuggestion.task_id == Task.id)
                .where(Task.needs_review == True)  # noqa: E712
            )
        ).scalar_one()

    return {
        "by_status": by_status,
        "by_class": by_class,
        "total_pending": by_status.get("pending", 0),
        "followups_due": followups_due,
    }


@router.post("/run", status_code=200)
async def trigger_triage(
    top: int = Query(50, ge=1, le=100),
    user: User = Depends(get_current_user),
) -> dict:
    """Manuell Triage für die letzten E-Mails auslösen."""
    _require_owner(user)
    count = await run_triage_now(top=top)
    return {"status": "completed", "classified": count}


@router.get("/{triage_id}", response_model=EmailTriageOut)
async def get_triage_item(
    triage_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailTriageOut:
    _require_owner(user)
    result = await db.execute(select(EmailTriage).where(EmailTriage.id == triage_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Triage-Eintrag nicht gefunden")
    return item


@router.patch("/{triage_id}", response_model=EmailTriageOut)
async def update_triage_item(
    triage_id: uuid.UUID,
    body: EmailTriageUpdate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailTriageOut:
    _require_owner(user)
    result = await db.execute(select(EmailTriage).where(EmailTriage.id == triage_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Triage-Eintrag nicht gefunden")

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(item, field, value)

    return item


class ReclassifyRequest(BaseModel):
    # Beide Felder sind optional: der Berater kann die Klasse korrigieren, das Label,
    # oder beides. Bisher war nur die Klasse korrigierbar -- entsprechend gab es NULL
    # Label-Korrekturen als Lernsignal, und 'Finanzen' konnte das System nie lernen.
    triage_class: str | None = None
    label: str | None = None
    reason: str | None = None


def _build_corrective_meta(
    orig_meta: dict | None,
    item: EmailTriage,
    forced_class: str,
    reason: str | None,
    forced_label: str | None = None,
) -> dict:
    """Baut die metadata_json fuer einen Korrektur-Job (rein, ohne DB/IO).

    Klont -- falls vorhanden -- die Metadaten des Original-Jobs, stellt die
    Pflichtfelder fuer den Worker sicher (Fallback aus dem EmailTriage-Eintrag),
    entfernt irrefuehrende Reste des letzten Laufs und setzt ``forced_class``.

    ``forced_label`` haelt eine gleichzeitige Label-Korrektur fest. Ohne das setzte
    ``_apply_label_correction`` die Kategorie in Outlook, und der unmittelbar danach
    laufende Korrektur-Job schrieb sie mit dem frisch geratenen LLM-Label wieder
    zurueck -- die Menschenkorrektur hielt nur Sekunden.
    """
    meta: dict = dict(orig_meta or {})
    meta["email_message_id"] = meta.get("email_message_id") or item.message_id
    meta["message_id"] = meta.get("message_id") or item.message_id
    meta.setdefault("subject", item.subject or "")
    meta.setdefault("from_address", item.from_address or "")
    meta.setdefault("from_name", item.from_name or "")
    for stale in ("draft_id", "original_draft_html", "trace", "tools_used",
                  "self_grade", "feedback_captured"):
        meta.pop(stale, None)
    meta["forced_class"] = forced_class
    if forced_label:
        meta["forced_label"] = forced_label
    meta["correction_reason"] = reason or ""
    meta["is_correction"] = True
    return meta


async def _enqueue_corrective_triage_job(
    db: AsyncSession,
    item: EmailTriage,
    forced_class: str,
    reason: str | None,
    forced_label: str | None = None,
) -> uuid.UUID | None:
    """Reiht einen email_triage-Job ein, der die korrigierte Klasse erzwingt.

    Wiederverwendet die bewaehrte Worker-Maschinerie (_post_process_triage legt
    Tasks an bzw. snapshottet Entwuerfe). Das EmailTriage-Item wird auf den neuen
    Job umgehaengt, damit das Post-Processing dieselbe Zeile aktualisiert.
    Best-effort.
    """
    try:
        orig_meta: dict | None = None
        if item.agent_job_id:
            orig = await db.execute(
                select(AgentJob.metadata_json).where(AgentJob.id == item.agent_job_id)
            )
            orig_meta = orig.scalar_one_or_none()

        meta = _build_corrective_meta(orig_meta, item, forced_class, reason, forced_label)

        job = AgentJob(
            user_id=item.user_id,
            task_id=None,
            job_type="email_triage",
            status="queued",
            metadata_json=meta,
        )
        db.add(job)
        await db.flush()
        # Item auf den neuen Job umhaengen, damit _post_process_triage es aktualisiert.
        item.agent_job_id = job.id
        item.status = "processing"
        logger.info(
            "Korrektur-Job %s eingereiht (forced_class=%s, forced_label=%s, triage=%s)",
            job.id, forced_class, forced_label or "-", item.id,
        )
        return job.id
    except Exception:  # noqa: BLE001 - best-effort, Korrektur darf nie scheitern
        logger.exception("Korrektur-Job konnte nicht eingereiht werden (triage=%s)", item.id)
        return None


async def _apply_label_correction(
    db: AsyncSession, item: EmailTriage, body: ReclassifyRequest
) -> None:
    """Uebernimmt eine Label-Korrektur: Lernsignal, DB und Outlook-Kategorie.

    Ohne diesen Pfad war ein Kategoriefehler nicht korrigierbar und damit auch nicht
    lernbar -- es gab NULL ``triage_reclass``-Feedbacks mit Label-Bezug. Erst damit
    kann die Reflexion eine Regel wie «Post von t-r.ch ist Finanzen» ueberhaupt
    vorschlagen.
    """
    if body.label is None:
        return
    new_label = normalize_label(body.label)
    action = dict(item.suggested_action or {})
    old_label = action.get("label")
    if new_label == old_label:
        return

    await record_feedback(
        db,
        feedback_type="triage_reclass",
        agent_job_id=item.agent_job_id,
        sender_email=item.from_address,
        source="cockpit",
        original={"label": old_label},
        corrected={"label": new_label},
        reason=body.reason,
    )
    action["label"] = new_label
    action["label_corrected_by_human"] = True
    action.pop("needs_review", None)
    item.suggested_action = action

    if item.message_id:
        from app.services.hermes_worker import apply_label_category

        await apply_label_category(item.message_id, new_label)
    logger.info(
        "Label korrigiert: %s -> %s (triage=%s)", old_label, new_label, item.id,
    )


@router.post("/{triage_id}/reclassify", response_model=EmailTriageOut)
async def reclassify_triage_item(
    triage_id: uuid.UUID,
    body: ReclassifyRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> EmailTriageOut:
    """Berater korrigiert Triage-Klasse und/oder Label -- lernen UND ausfuehren.

    Schreibt ein ``triage_reclass``-Feedback, markiert die zugehoerige Episode als
    korrigiert und reiht -- bei Zielklasse ``task``/``auto_reply`` -- einen
    Korrektur-Job ein, der den richtigen Artefakt (Task bzw. Antwort-Entwurf)
    tatsaechlich erzeugt. ``fyi`` wird nur gelernt (Item verworfen).

    Eine **Label-Korrektur** setzt zusaetzlich die Outlook-Kategorie neu -- ohne Move,
    damit eine Menschenkorrektur nie etwas aus dem Blick raeumt. Beide Korrekturen
    sind unabhaengig: nur Label, nur Klasse oder beides.
    """
    _require_owner(user)
    if body.triage_class is not None and body.triage_class not in ("auto_reply", "task", "fyi"):
        raise HTTPException(status_code=400, detail="Ungueltige Triage-Klasse")
    if body.label is not None and normalize_label(body.label) is None:
        raise HTTPException(
            status_code=400,
            detail=f"Ungueltiges Label. Erlaubt: {', '.join(TRIAGE_LABELS)}",
        )
    if body.triage_class is None and body.label is None:
        raise HTTPException(status_code=400, detail="Weder Klasse noch Label angegeben")

    result = await db.execute(select(EmailTriage).where(EmailTriage.id == triage_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Triage-Eintrag nicht gefunden")

    await _apply_label_correction(db, item, body)

    old_class = item.triage_class
    if body.triage_class is not None and body.triage_class != old_class:
        # 1) Lernsignal + Episode-Korrektur (mit Original-Job-Bezug, vor dem Umhaengen).
        await record_feedback(
            db,
            feedback_type="triage_reclass",
            agent_job_id=item.agent_job_id,
            sender_email=item.from_address,
            source="cockpit",
            original={"triage_class": old_class},
            corrected={"triage_class": body.triage_class},
            reason=body.reason,
        )
        if item.agent_job_id:
            await mark_episode_corrected(
                db,
                agent_job_id=item.agent_job_id,
                lesson=body.reason or f"Reklassifiziert: {old_class} -> {body.triage_class}",
            )

        item.triage_class = body.triage_class

        # 2) Ausfuehren: korrigierten Artefakt erzeugen (Task bzw. Entwurf).
        if body.triage_class in ("task", "auto_reply"):
            await _enqueue_corrective_triage_job(
                db, item,
                forced_class=body.triage_class,
                reason=body.reason,
                forced_label=normalize_label(body.label) if body.label else None,
            )
        else:  # fyi -> nur lernen, Item verwerfen
            item.status = "dismissed"
    return item


@router.post("/{triage_id}/dismiss", status_code=200)
async def dismiss_triage_item(
    triage_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    _require_owner(user)
    result = await db.execute(select(EmailTriage).where(EmailTriage.id == triage_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Triage-Eintrag nicht gefunden")
    item.status = "dismissed"
    return {"status": "dismissed", "id": str(triage_id)}


@router.post("/{triage_id}/act", status_code=200)
async def act_on_triage_item(
    triage_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Triage-Vorschlag annehmen und Aktion ausführen."""
    _require_owner(user)
    result = await db.execute(select(EmailTriage).where(EmailTriage.id == triage_id))
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Triage-Eintrag nicht gefunden")

    item.status = "acted"
    action = item.suggested_action or {}
    action_type = action.get("type", "human_review")

    return {
        "status": "acted",
        "id": str(triage_id),
        "action_type": action_type,
        "triage_class": item.triage_class,
        "message_id": item.message_id,
    }


@router.get("/activity/feed")
async def triage_activity_feed(
    limit: int = Query(30, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Activity-Feed: Was hat der Agent getan? Letzte Triage-Aktionen + AgentJob-Ergebnisse."""
    _require_owner(user)

    since = datetime.now(timezone.utc) - timedelta(days=7)

    jobs_q = (
        select(AgentJob)
        .where(
            AgentJob.job_type.in_(["email_triage", "draft_email_reply", "create_task_from_email"]),
            AgentJob.created_at >= since,
        )
        .order_by(AgentJob.created_at.desc())
        .limit(limit)
    )
    jobs_result = await db.execute(jobs_q)
    jobs = list(jobs_result.scalars().all())

    activities = []
    for job in jobs:
        meta = job.metadata_json or {}
        activities.append({
            "id": str(job.id),
            "job_type": job.job_type,
            "status": job.status,
            "subject": meta.get("subject", ""),
            "from_address": meta.get("from_address", ""),
            "output": job.output,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        })

    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    stats_q = await db.execute(
        select(
            func.count(AgentJob.id).filter(
                and_(AgentJob.status == "awaiting_approval", AgentJob.created_at >= today)
            ).label("drafts_pending"),
            func.count(AgentJob.id).filter(
                and_(AgentJob.status == "completed", AgentJob.job_type == "email_triage", AgentJob.created_at >= today)
            ).label("classified_today"),
        )
    )
    stats_row = stats_q.one()

    return {
        "activities": activities,
        "summary": {
            "drafts_pending": stats_row.drafts_pending,
            "classified_today": stats_row.classified_today,
        },
    }


# ── Replay-Endpoints ─────────────────────────────────────────

class ReplayRequest(BaseModel):
    message_id: str


class ReplayBatchRequest(BaseModel):
    count: int = 5
    triage_class: str | None = None


@router.post("/replay", status_code=201)
async def replay_single(
    body: ReplayRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """E-Mail erneut durch die Triage-Pipeline schicken (Replay/Test-Modus)."""
    _require_owner(user)

    settings = get_settings()
    if not settings.graph_tenant_id or not settings.graph_client_id:
        raise HTTPException(status_code=503, detail="Graph API nicht konfiguriert")

    client = _get_graph_client()
    try:
        msg = await client.get_email(body.message_id)
    except Exception as e:
        logger.exception("E-Mail %s konnte nicht geladen werden", body.message_id)
        raise HTTPException(status_code=404, detail="E-Mail konnte nicht geladen werden")

    from_obj = msg.get("from", {}).get("emailAddress", {})
    subject = msg.get("subject", "(kein Betreff)")
    from_address = from_obj.get("address", "")
    from_name = from_obj.get("name", "")
    received_at_str = msg.get("receivedDateTime")
    conversation_id = msg.get("conversationId", "")

    job = AgentJob(
        user_id=user.id,
        task_id=None,
        job_type="email_triage",
        status="queued",
        metadata_json={
            "email_message_id": body.message_id,
            "message_id": body.message_id,
            "subject": subject,
            "from_address": from_address,
            "from_name": from_name,
            "conversation_id": conversation_id,
            "body_preview": msg.get("bodyPreview", "")[:300],
            "inference_classification": msg.get("inferenceClassification", ""),
            "is_replay": True,
        },
    )
    db.add(job)
    await db.flush()

    triage = EmailTriage(
        user_id=user.id,
        message_id=f"replay_{job.id}_{body.message_id[:40]}",
        subject=subject,
        from_address=from_address,
        from_name=from_name,
        received_at=datetime.fromisoformat(received_at_str.replace("Z", "+00:00")) if received_at_str else None,
        inference_class=msg.get("inferenceClassification"),
        status="pending",
        agent_job_id=job.id,
    )
    db.add(triage)
    await db.commit()

    logger.info("Replay-Job erstellt: job=%s, message_id=%s", job.id, body.message_id)

    return {
        "status": "queued",
        "job_id": str(job.id),
        "triage_id": str(triage.id),
        "subject": subject,
        "from": from_address,
        "is_replay": True,
    }


@router.post("/replay-batch", status_code=201)
async def replay_batch(
    body: ReplayBatchRequest,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict:
    """Letzte N E-Mails erneut triagieren (optional nach triage_class filtern)."""
    _require_owner(user)

    query = (
        select(EmailTriage)
        .where(
            EmailTriage.message_id.not_like("replay_%"),
            EmailTriage.user_id == user.id,
        )
        .order_by(EmailTriage.created_at.desc())
    )
    if body.triage_class:
        query = query.where(EmailTriage.triage_class == body.triage_class)
    query = query.limit(body.count)

    result = await db.execute(query)
    items = list(result.scalars().all())

    if not items:
        return {"status": "no_items", "replayed": 0, "jobs": []}

    jobs_created = []
    for item in items:
        job = AgentJob(
            user_id=user.id,
            task_id=None,
            job_type="email_triage",
            status="queued",
            metadata_json={
                "email_message_id": item.message_id,
                "message_id": item.message_id,
                "subject": item.subject or "",
                "from_address": item.from_address or "",
                "from_name": item.from_name or "",
                "is_replay": True,
                "original_triage_class": item.triage_class,
            },
        )
        db.add(job)
        await db.flush()
        jobs_created.append({
            "job_id": str(job.id),
            "message_id": item.message_id,
            "subject": item.subject,
        })

    await db.commit()
    logger.info("Batch-Replay: %d Jobs erstellt", len(jobs_created))

    return {
        "status": "queued",
        "replayed": len(jobs_created),
        "jobs": jobs_created,
    }

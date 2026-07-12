"""FastAPI-Router für Meeting-Transkripte und -Protokolle (Owner-only).

Liste, Detail, Original-VTT-/Klartext-Download, Anonymisierung und Re-Analyse
(mit optionalem LLM-Override, z. B. Cloud-Modell für wichtige Meetings).
"""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel
from sqlalchemy import select

from app.auth.deps import require_role
from app.database import async_session
from app.models import AgentJob, MeetingTranscript, User

logger = logging.getLogger("taskpilot.meetings_api")

router = APIRouter(prefix="/api/meetings", tags=["meetings"])


class MeetingListItem(BaseModel):
    id: str
    subject: str | None
    organizer: str | None
    started_at: str | None
    ended_at: str | None
    status: str
    has_protocol: bool
    has_anonymized: bool
    transcript_chars: int
    created_at: str


class MeetingDetail(MeetingListItem):
    transcript_text: str | None
    protocol_md: str | None
    anonymized_text: str | None
    anonymized_protocol_md: str | None
    agent_job_id: str | None
    error_message: str | None


def _to_list_item(m: MeetingTranscript) -> MeetingListItem:
    return MeetingListItem(
        id=str(m.id),
        subject=m.subject,
        organizer=m.organizer,
        started_at=m.started_at.isoformat() if m.started_at else None,
        ended_at=m.ended_at.isoformat() if m.ended_at else None,
        status=m.status,
        has_protocol=bool(m.protocol_md),
        has_anonymized=bool(m.anonymized_text or m.anonymized_protocol_md),
        transcript_chars=len(m.transcript_text or ""),
        created_at=m.created_at.isoformat(),
    )


async def _load_meeting(meeting_id: str) -> MeetingTranscript:
    try:
        mid = uuid.UUID(meeting_id)
    except ValueError:
        raise HTTPException(status_code=422, detail="Ungültige Meeting-ID")
    async with async_session() as db:
        record = await db.get(MeetingTranscript, mid)
    if record is None:
        raise HTTPException(status_code=404, detail="Meeting nicht gefunden")
    return record


@router.get("", response_model=list[MeetingListItem])
async def list_meetings(
    limit: int = 50,
    _user: User = Depends(require_role("owner")),
) -> list[MeetingListItem]:
    async with async_session() as db:
        rows = (
            await db.execute(
                select(MeetingTranscript)
                .order_by(MeetingTranscript.started_at.desc().nulls_last())
                .limit(min(limit, 200))
            )
        ).scalars().all()
    return [_to_list_item(m) for m in rows]


@router.get("/{meeting_id}", response_model=MeetingDetail)
async def get_meeting(
    meeting_id: str,
    _user: User = Depends(require_role("owner")),
) -> MeetingDetail:
    m = await _load_meeting(meeting_id)
    base = _to_list_item(m)
    return MeetingDetail(
        **base.model_dump(),
        transcript_text=m.transcript_text,
        protocol_md=m.protocol_md,
        anonymized_text=m.anonymized_text,
        anonymized_protocol_md=m.anonymized_protocol_md,
        agent_job_id=str(m.agent_job_id) if m.agent_job_id else None,
        error_message=m.error_message,
    )


def _safe_filename(subject: str | None, suffix: str) -> str:
    base = "".join(c if c.isalnum() or c in " -_" else "_" for c in (subject or "meeting")).strip()
    return f"{(base or 'meeting')[:60]}{suffix}"


@router.get("/{meeting_id}/transcript.vtt")
async def download_transcript_vtt(
    meeting_id: str,
    _user: User = Depends(require_role("owner")),
) -> Response:
    """Original-Transkript (WebVTT) herunterladen — unverändert wie von Graph geliefert."""
    m = await _load_meeting(meeting_id)
    if not m.raw_vtt:
        raise HTTPException(status_code=404, detail="Kein Original-VTT vorhanden")
    return Response(
        content=m.raw_vtt,
        media_type="text/vtt",
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_filename(m.subject, ".vtt")}"'
        },
    )


@router.get("/{meeting_id}/transcript.txt")
async def download_transcript_text(
    meeting_id: str,
    anonymized: bool = False,
    _user: User = Depends(require_role("owner")),
) -> Response:
    """Geparstes Transkript als Klartext (optional anonymisierte Fassung)."""
    m = await _load_meeting(meeting_id)
    text = m.anonymized_text if anonymized else m.transcript_text
    if not text:
        raise HTTPException(
            status_code=404,
            detail="Anonymisierte Fassung noch nicht erstellt" if anonymized else "Kein Transkript-Text vorhanden",
        )
    suffix = "_anonymisiert.txt" if anonymized else ".txt"
    return Response(
        content=text,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{_safe_filename(m.subject, suffix)}"'
        },
    )


@router.post("/{meeting_id}/anonymize", response_model=MeetingDetail)
async def anonymize_meeting_endpoint(
    meeting_id: str,
    _user: User = Depends(require_role("owner")),
) -> MeetingDetail:
    """Erzeugt (oder erneuert) die anonymisierte Fassung von Transkript und Protokoll.

    Zweistufig (Regex + lokales LLM); die Mapping-Tabelle bleibt lokal in der DB
    und wird nie mit ausgeliefert.
    """
    from app.services.meetings import anonymize_meeting

    m = await _load_meeting(meeting_id)
    if not (m.transcript_text or m.protocol_md):
        raise HTTPException(status_code=409, detail="Noch kein Inhalt zum Anonymisieren vorhanden")

    try:
        result = await anonymize_meeting(m)
    except Exception as e:  # noqa: BLE001
        logger.exception("Anonymisierung fehlgeschlagen (Meeting %s)", meeting_id)
        raise HTTPException(status_code=502, detail=f"Anonymisierung fehlgeschlagen: {e}")

    async with async_session() as db:
        record = await db.get(MeetingTranscript, m.id)
        record.anonymized_text = result["anonymized_text"]
        record.anonymized_protocol_md = result["anonymized_protocol_md"]
        record.anonymization_map = result["anonymization_map"]
        await db.commit()
        await db.refresh(record)
        base = _to_list_item(record)
        return MeetingDetail(
            **base.model_dump(),
            transcript_text=record.transcript_text,
            protocol_md=record.protocol_md,
            anonymized_text=record.anonymized_text,
            anonymized_protocol_md=record.anonymized_protocol_md,
            agent_job_id=str(record.agent_job_id) if record.agent_job_id else None,
            error_message=record.error_message,
        )


class ReanalyzeBody(BaseModel):
    llm_override: str | None = None  # z. B. "ollama/qwen3.6:35b" oder Cloud-Modell


@router.post("/{meeting_id}/reanalyze", status_code=202)
async def reanalyze_meeting(
    meeting_id: str,
    body: ReanalyzeBody | None = None,
    _user: User = Depends(require_role("owner")),
) -> dict:
    """Erstellt das Protokoll neu (optional mit anderem LLM) — Original bleibt erhalten."""
    m = await _load_meeting(meeting_id)
    if not (m.transcript_text or "").strip():
        raise HTTPException(status_code=409, detail="Kein Transkript-Text für die Analyse vorhanden")

    override = (body.llm_override if body else None) or None
    async with async_session() as db:
        job = AgentJob(
            user_id=m.user_id,
            job_type="meeting_summary",
            status="queued",
            metadata_json={
                "meeting_transcript_id": str(m.id),
                "subject": m.subject,
                "description": f"Meeting-Protokoll (Re-Analyse): {m.subject}",
                "autonomy_level": "L2",
                **({"llm_override": override} if override else {}),
            },
            llm_model=override,
        )
        db.add(job)
        await db.flush()
        record = await db.get(MeetingTranscript, m.id)
        record.status = "processing"
        record.agent_job_id = job.id
        await db.commit()
        return {"status": "queued", "agent_job_id": str(job.id)}

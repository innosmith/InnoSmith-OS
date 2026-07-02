"""Teams-Chat-Endpoints -- Graph API für Teams-Nachrichten und Meeting-Transkripte."""

import logging
import os
import sys

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import get_current_user
from app.config import get_settings
from app.models import User

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "email-graph"))
from graph_client import GraphClient, GraphConfig  # noqa: E402

logger = logging.getLogger("taskpilot.teams")
router = APIRouter(prefix="/api/teams", tags=["teams"])

_graph_client: GraphClient | None = None


def _get_graph_client() -> GraphClient:
    global _graph_client
    if _graph_client is None:
        s = get_settings()
        config = GraphConfig(
            tenant_id=s.graph_tenant_id,
            client_id=s.graph_client_id,
            client_secret=s.graph_client_secret,
            user_email=s.graph_user_email,
        )
        _graph_client = GraphClient(config)
    return _graph_client


def _require_owner(user: User) -> None:
    if user.role != "owner":
        raise HTTPException(status_code=403, detail="Nur Owner dürfen auf Teams zugreifen")


def _check_configured() -> None:
    s = get_settings()
    if not all([s.graph_tenant_id, s.graph_client_id, s.graph_client_secret, s.graph_user_email]):
        raise HTTPException(
            status_code=503,
            detail="Graph API nicht konfiguriert. Setze TP_GRAPH_* in der Umgebung.",
        )


@router.get("/chats")
async def list_chats(
    top: int = Query(default=20, ge=1, le=50),
    user: User = Depends(get_current_user),
):
    """Alle Teams-Chats mit letzter Nachricht."""
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        chats = await client.list_chats(top=top)
        result = []
        for c in chats:
            preview = c.get("lastMessagePreview") or {}
            sender = (preview.get("from") or {}).get("user", {})
            result.append({
                "id": c.get("id"),
                "topic": c.get("topic"),
                "chatType": c.get("chatType"),
                "createdDateTime": c.get("createdDateTime"),
                "lastMessage": {
                    "body": (preview.get("body") or {}).get("content", "")[:300],
                    "from": sender.get("displayName"),
                    "createdDateTime": preview.get("createdDateTime"),
                } if preview else None,
            })
        return result
    except PermissionError as e:
        logger.warning("Teams list_chats: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf Teams-Chats verweigert")
    except Exception as e:
        logger.exception("list_chats fehlgeschlagen")
        raise HTTPException(status_code=502, detail="Teams-Chats konnten nicht geladen werden")


@router.get("/chats/{chat_id}/messages")
async def list_chat_messages(
    chat_id: str,
    top: int = Query(default=20, ge=1, le=50),
    user: User = Depends(get_current_user),
):
    """Nachrichten eines Teams-Chats."""
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        msgs = await client.list_chat_messages(chat_id=chat_id, top=top)
        result = []
        for m in msgs:
            body = (m.get("body") or {}).get("content", "")
            sender = (m.get("from") or {}).get("user", {})
            result.append({
                "id": m.get("id"),
                "from": sender.get("displayName"),
                "fromId": sender.get("id"),
                "body": body[:2000],
                "bodyContentType": (m.get("body") or {}).get("contentType"),
                "createdDateTime": m.get("createdDateTime"),
                "messageType": m.get("messageType"),
            })
        return result
    except PermissionError as e:
        logger.warning("Teams list_chat_messages: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf Teams-Nachrichten verweigert")
    except Exception as e:
        logger.exception("list_chat_messages fehlgeschlagen")
        raise HTTPException(status_code=502, detail="Teams-Nachrichten konnten nicht geladen werden")


@router.get("/chats/{chat_id}/members")
async def list_chat_members(
    chat_id: str,
    user: User = Depends(get_current_user),
):
    """Teilnehmer eines Teams-Chats."""
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        members = await client.list_chat_members(chat_id=chat_id)
        return [
            {
                "id": m.get("id"),
                "displayName": m.get("displayName"),
                "email": m.get("email"),
            }
            for m in members
        ]
    except Exception as e:
        logger.exception("list_chat_members fehlgeschlagen")
        raise HTTPException(status_code=502, detail="Teams-Mitglieder konnten nicht geladen werden")


@router.get("/meetings/recent")
async def list_recent_meetings(
    hours: int = Query(default=48, ge=1, le=168, description="Meetings der letzten N Stunden"),
    user: User = Depends(get_current_user),
):
    """Kürzliche Online-Meetings mit Transkript-Verfügbarkeit.

    Basiert auf ``getAllTranscripts`` (nur vom User organisierte Meetings) und
    gruppiert die Transkripte pro Meeting.
    """
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        transcripts = await client.list_recent_transcripts(start, end)

        by_meeting: dict[str, int] = {}
        for tr in transcripts:
            mid = tr.get("meetingId")
            if mid:
                by_meeting[mid] = by_meeting.get(mid, 0) + 1

        result = []
        for meeting_id, count in by_meeting.items():
            meeting: dict = {}
            try:
                meeting = await client.get_online_meeting(meeting_id)
            except Exception:
                pass
            result.append({
                "id": meeting_id,
                "subject": meeting.get("subject"),
                "startDateTime": meeting.get("startDateTime"),
                "endDateTime": meeting.get("endDateTime"),
                "hasTranscript": True,
                "transcriptCount": count,
            })
        return result
    except PermissionError as e:
        logger.warning("Teams list_recent_meetings: Zugriff verweigert: %s", e)
        raise HTTPException(status_code=403, detail="Zugriff auf Teams-Meetings verweigert")
    except Exception as e:
        logger.exception("list_recent_meetings fehlgeschlagen")
        raise HTTPException(status_code=502, detail="Teams-Meetings konnten nicht geladen werden")


@router.get("/meetings/{meeting_id}/transcript")
async def get_meeting_transcript(
    meeting_id: str,
    user: User = Depends(get_current_user),
):
    """Transkript eines Meetings als Text."""
    _require_owner(user)
    _check_configured()
    client = _get_graph_client()
    try:
        transcripts = await client.list_meeting_transcripts(meeting_id)
        if not transcripts:
            raise HTTPException(status_code=404, detail="Kein Transkript verfügbar")
        transcript_id = transcripts[0]["id"]
        content = await client.get_meeting_transcript_content(meeting_id, transcript_id)
        return {"meeting_id": meeting_id, "transcript_id": transcript_id, "content": content}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("get_meeting_transcript fehlgeschlagen")
        raise HTTPException(status_code=502, detail="Meeting-Transkript konnte nicht geladen werden")

import asyncio

import asyncpg
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sse_starlette.sse import EventSourceResponse

from app.auth.security import decode_access_token
from app.config import get_settings
from app.database import get_db
from app.models import User

router = APIRouter(prefix="/api/sse", tags=["sse"])

OWNER_ONLY_EVENTS = {"agent_jobs_changed", "email_triage_changed", "chat_triage_changed", "mindmaps_changed", "learned_rules_changed"}
MEMBER_EVENTS = {"tasks_changed", "notifications_changed"}


async def _get_user_from_token(
    token: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> User:
    """EventSource kann keinen Authorization-Header senden, daher Token per Query."""
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token required")
    payload = decode_access_token(token)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token payload")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user is None or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    return user


async def _listen_pg(channels: list[str]):
    settings = get_settings()
    conn = await asyncpg.connect(
        host=settings.db_host,
        port=settings.db_port,
        user=settings.db_user,
        password=settings.db_password,
        database=settings.db_name,
    )
    queue: asyncio.Queue = asyncio.Queue()

    def _callback(conn, pid, channel, payload):
        queue.put_nowait({"channel": channel, "data": payload})

    for ch in channels:
        await conn.add_listener(ch, _callback)

    try:
        while True:
            msg = await queue.get()
            yield msg
    finally:
        for ch in channels:
            await conn.remove_listener(ch, _callback)
        await conn.close()


@router.get("/events")
async def event_stream(user: User = Depends(_get_user_from_token)):
    is_owner = user.role == "owner"
    channels = list(OWNER_ONLY_EVENTS | MEMBER_EVENTS) if is_owner else list(MEMBER_EVENTS)

    async def generate():
        async for msg in _listen_pg(channels):
            channel = msg["channel"]
            if not is_owner and channel in OWNER_ONLY_EVENTS:
                continue
            yield {
                "event": channel,
                "data": msg["data"],
            }

    return EventSourceResponse(generate())

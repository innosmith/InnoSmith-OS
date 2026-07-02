"""Test: Meeting-Quelle in /api/tasks/pending-review.

Legt ein MeetingTranscript + einen needs_review-Task mit meeting_transcript_id
an und prüft, dass der Endpoint die strukturierte Quelle (source='meeting',
meeting_subject, meeting_transcript_id) zurückliefert. DB-gestützt.
"""

import uuid

import pytest

from conftest import TEST_PROJECT_ID, TEST_COLUMN_BACKLOG_ID

pytestmark = [pytest.mark.asyncio, pytest.mark.db]


@pytest.mark.db
async def test_pending_review_exposes_meeting_source(client_as_owner):
    from app.database import async_session
    from app.models import MeetingTranscript, Task

    subject = f"Test-Meeting {uuid.uuid4().hex[:8]}"
    async with async_session() as db:
        transcript = MeetingTranscript(
            meeting_id=f"m-{uuid.uuid4().hex}",
            transcript_id=f"t-{uuid.uuid4().hex}",
            subject=subject,
            protocol_md="## Protokoll\n- Punkt 1",
            status="completed",
        )
        db.add(transcript)
        await db.flush()
        task = Task(
            title=f"Aus Meeting {uuid.uuid4().hex[:8]}",
            project_id=TEST_PROJECT_ID,
            board_column_id=TEST_COLUMN_BACKLOG_ID,
            board_position=1.0,
            meeting_transcript_id=transcript.id,
            needs_review=True,
            assignee="me",
        )
        db.add(task)
        await db.commit()
        task_id = str(task.id)
        transcript_id = str(transcript.id)

    try:
        resp = await client_as_owner.get("/api/tasks/pending-review")
        assert resp.status_code == 200
        rows = {r["id"]: r for r in resp.json()}
        assert task_id in rows, "angelegter Meeting-Task fehlt in pending-review"
        row = rows[task_id]
        assert row["source"] == "meeting"
        assert row["meeting_subject"] == subject
        assert row["meeting_transcript_id"] == transcript_id
    finally:
        async with async_session() as db:
            t = await db.get(Task, uuid.UUID(task_id))
            if t is not None:
                await db.delete(t)
            mt = await db.get(MeetingTranscript, uuid.UUID(transcript_id))
            if mt is not None:
                await db.delete(mt)
            await db.commit()

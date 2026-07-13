"""Isolationstests fuer das Mehrbenutzer-Fundament (user_id-Scope).

Verifiziert direkt gegen die Test-DB, dass persoenliche Tabellen pro Principal
getrennt sind:

1. Der Composite-Unique ``(user_id, message_id)`` erlaubt zwei verschiedenen Usern
   denselben natuerlichen Schluessel (dieselbe E-Mail), verbietet ihn aber innerhalb
   desselben Users.
2. Lesepfade, die nach ``user_id`` filtern, liefern ausschliesslich die Daten des
   jeweiligen Principals.

Alle Tests rollen ihre Schreibvorgaenge zurueck -- es bleibt nichts in der DB.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.database import async_session
from app.models.models import EmailTriage, User


async def _make_user(db, tag: str) -> User:
    user = User(
        email=f"iso-{tag}-{uuid.uuid4()}@example.ch",
        password_hash="not-a-real-hash",
        display_name=f"Iso {tag}",
    )
    db.add(user)
    await db.flush()
    return user


@pytest.mark.db
@pytest.mark.asyncio
async def test_same_message_id_allowed_across_users_but_not_within():
    """Composite-Unique: gleiche message_id fuer zwei User ok, fuer denselben nicht."""
    async with async_session() as db:
        user_a = await _make_user(db, "a")
        user_b = await _make_user(db, "b")
        shared_message = f"shared-{uuid.uuid4()}"

        db.add(EmailTriage(user_id=user_a.id, message_id=shared_message))
        db.add(EmailTriage(user_id=user_b.id, message_id=shared_message))
        await db.flush()  # zwei Principals, gleiche message_id -> erlaubt

        # Derselbe Principal + dieselbe message_id -> Unique-Verletzung
        db.add(EmailTriage(user_id=user_a.id, message_id=shared_message))
        with pytest.raises(IntegrityError):
            await db.flush()

        await db.rollback()


@pytest.mark.db
@pytest.mark.asyncio
async def test_email_triage_read_is_scoped_by_user():
    """Ein nach user_id gefilterter Read liefert nur die Daten des Principals."""
    async with async_session() as db:
        user_a = await _make_user(db, "a")
        user_b = await _make_user(db, "b")
        db.add(EmailTriage(user_id=user_a.id, message_id=f"a-{uuid.uuid4()}"))
        db.add(EmailTriage(user_id=user_a.id, message_id=f"a-{uuid.uuid4()}"))
        db.add(EmailTriage(user_id=user_b.id, message_id=f"b-{uuid.uuid4()}"))
        await db.flush()

        rows_a = (
            await db.execute(select(EmailTriage).where(EmailTriage.user_id == user_a.id))
        ).scalars().all()
        rows_b = (
            await db.execute(select(EmailTriage).where(EmailTriage.user_id == user_b.id))
        ).scalars().all()

        assert len(rows_a) == 2
        assert len(rows_b) == 1
        assert all(r.user_id == user_a.id for r in rows_a)
        assert rows_b[0].user_id == user_b.id

        await db.rollback()

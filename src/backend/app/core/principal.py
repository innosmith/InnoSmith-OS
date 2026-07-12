"""Zentrale Aufloesung des handelnden Prinzipals (Acting Principal).

Historisch nahm TaskPilot genau EINEN Owner an und loeste ihn an vielen Stellen
dupliziert ueber ``select(User).where(User.role == "owner")`` auf. Fuer die
AI9-Core-Extraktion und den spaeteren Mehrbenutzer-Betrieb wird diese Aufloesung
hier zentralisiert: EINE Single Source of Truth.

Heute liefert das Modul weiterhin den einzigen Owner zurueck -- das Verhalten
bleibt also unveraendert. Der Mehrbenutzer-Umbau (Phase D) ersetzt spaeter die
Owner-Annahme durch den tatsaechlich handelnden User, ohne dass die Call-Sites
erneut angefasst werden muessen.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import User


async def get_owner(db: AsyncSession, *, active_only: bool = False) -> User | None:
    """Liefert den Owner-User (oder ``None``, falls keiner existiert).

    ``active_only=True`` beschraenkt auf aktive Accounts (fuer Kontexte, die einen
    handlungsfaehigen Empfaenger brauchen, z. B. Notifications).
    """
    stmt = select(User).where(User.role == "owner")
    if active_only:
        stmt = stmt.where(User.is_active.is_(True))
    result = await db.execute(stmt.limit(1))
    return result.scalar_one_or_none()


async def get_owner_id(db: AsyncSession) -> uuid.UUID | None:
    """Liefert nur die Owner-UUID (leichtgewichtiger als den ganzen User)."""
    result = await db.execute(select(User.id).where(User.role == "owner").limit(1))
    return result.scalar_one_or_none()


async def get_owner_settings(db: AsyncSession) -> dict:
    """Liefert das Settings-JSONB des Owners (leeres Dict, falls nicht vorhanden)."""
    result = await db.execute(
        select(User.settings).where(User.role == "owner").limit(1)
    )
    return result.scalar_one_or_none() or {}

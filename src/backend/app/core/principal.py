"""Zentrale Aufloesung des handelnden Prinzipals (Acting Principal).

Historisch nahm TaskPilot genau EINEN Owner an und loeste ihn an vielen Stellen
dupliziert ueber ``select(User).where(User.role == "owner")`` auf. Fuer die
AI9-Core-Extraktion und den spaeteren Mehrbenutzer-Betrieb wird diese Aufloesung
hier zentralisiert: EINE Single Source of Truth.

Heute liefert das Modul weiterhin den einzigen Owner zurueck -- das Verhalten
bleibt also unveraendert. Der Mehrbenutzer-Umbau (Phase D) ersetzt spaeter die
Owner-Annahme durch den tatsaechlich handelnden User, ohne dass die Call-Sites
erneut angefasst werden muessen.

Regel fuer das Stempeln/Lesen von ``user_id`` in den persoenlichen Tabellen:
- **Request-Kontext** (Router): der authentifizierte User (``current_user.id``).
- **Background-Kontext** (Triage-Loop, Indexer, Followup, Worker): der System-
  Principal (:func:`system_principal_id`, heute == Owner).
- **Settings/Credentials** immer ueber :func:`get_principal_settings` ``(db, user_id)``.
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


async def system_principal_id(db: AsyncSession) -> uuid.UUID | None:
    """Der Principal fuer Hintergrund-Arbeit ohne Request-Kontext.

    Heute identisch mit dem Owner (Ein-Personen-System). Dies ist die einzige
    Stelle, an der der Mehrbenutzer-Umbau (Phase D) ansetzt: Sobald Hintergrund-
    Loops pro Principal iterieren, wird hier bzw. an den Aufrufern die konkrete
    ``user_id`` durchgereicht -- die Schreib- und Lesepfade bleiben unveraendert,
    weil sie bereits ``user_id``-parametrisiert sind.
    """
    return await get_owner_id(db)


async def get_principal_settings(db: AsyncSession, user_id: uuid.UUID | None) -> dict:
    """Liefert das Settings-JSONB eines konkreten Principals (leeres Dict, falls keiner).

    Das ist die generalisierte Settings-/Credential-Naht: Statt hart ``role='owner'``
    aufzuloesen, liest sie die Settings des uebergebenen ``user_id``. Hintergrund-
    Dienste uebergeben heute ``system_principal_id`` (== Owner); Request-Pfade den
    tatsaechlich handelnden User. Verhalten heute unveraendert.
    """
    if user_id is None:
        return {}
    result = await db.execute(select(User.settings).where(User.id == user_id).limit(1))
    return result.scalar_one_or_none() or {}


async def get_owner_settings(db: AsyncSession) -> dict:
    """Liefert das Settings-JSONB des Owners (leeres Dict, falls nicht vorhanden).

    Duenne Huelle ueber :func:`get_principal_settings` mit dem System-Principal.
    Bleibt aus Rueckwaerts-Kompatibilitaet erhalten; neue Aufrufer sollten direkt
    ``get_principal_settings(db, user_id)`` verwenden.
    """
    return await get_principal_settings(db, await system_principal_id(db))

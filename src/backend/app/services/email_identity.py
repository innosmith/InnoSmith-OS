"""Identität einer E-Mail gegen ihr Graph-Handle -- der eine Ort für diese Unterscheidung.

Die Graph-``id`` einer Nachricht ist ein **Handle**: sie zeigt auf die Nachricht in
einem bestimmten Ordner und wird bei jedem Move durch eine neue ersetzt. Die
``internetMessageId`` (RFC 5322) ist die **Identität**: sie wird vom sendenden System
vergeben und bleibt gleich, wohin die Mail auch wandert.

Diese Unterscheidung war bis zum 24.08.2026 nirgends abgebildet. Gespeichert wurde
nur das Handle, und kein Codepfad schrieb nach einem Move das neue zurück -- obwohl
``_finalize_email_state`` es kannte und innerhalb des Aufrufs benutzte. Sobald ein
Task bestätigt und die Quell-Mail archiviert wurde, zeigte ``email_message_id``
darum ins Leere; der Outlook-Link im Task-Detail war tot, und genau deshalb musste
man die Mail im Archiv von Hand suchen.

Regel, die dieses Modul durchsetzt: **Das Handle ist ein Zwischenspeicher, die
Identität ist die Wahrheit.** Wer eine Mail wiederfinden muss, fragt über die
Identität. Wer sie verschiebt, schreibt das neue Handle zurück.

Alle Funktionen sind best-effort: Ein fehlgeschlagener Graph-Zugriff darf keinen
Request und keinen Agentenlauf sprengen. ``None`` ist ein zulässiges Ergebnis und
allemal besser als ein geratenes Handle.
"""

import logging
from typing import NamedTuple

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import EmailTriage, Task

logger = logging.getLogger(__name__)


class MailFacts(NamedTuple):
    """Die zwei move-sicheren Angaben einer Mail.

    ``conversation_id`` steht hier mit dabei, weil beide aus derselben Graph-Antwort
    kommen und der Aufrufer sonst zwei Anfragen für eine Auskunft bräuchte. Auch die
    ``conversationId`` übersteht Moves -- an ihr hängt das Thread-Panel am Task.
    """

    internet_message_id: str | None
    conversation_id: str | None


async def fetch_mail_facts(client, message_id: str | None) -> MailFacts:
    """Liest Identität und Konversation zu einem bekannten Graph-Handle."""
    if not message_id:
        return MailFacts(None, None)
    try:
        data = await client.get_email(message_id)
    except Exception:  # noqa: BLE001 - best-effort, siehe Modul-Docstring
        logger.info(
            "Mail-Angaben nicht lesbar (mid=%s) -- Handle bleibt ohne Identität",
            str(message_id)[:40],
        )
        return MailFacts(None, None)
    data = data or {}
    return MailFacts(
        data.get("internetMessageId") or None,
        data.get("conversationId") or None,
    )


async def fetch_internet_message_id(client, message_id: str | None) -> str | None:
    """Nur die RFC-5322-Identität, für Aufrufer ohne Interesse am Thread."""
    return (await fetch_mail_facts(client, message_id)).internet_message_id


async def resolve_message_id(client, internet_message_id: str | None) -> str | None:
    """Sucht das aktuelle Graph-Handle zu einer Identität, ordnerunabhängig.

    Das ist die **einzige** Stelle, die ein veraltetes Handle reparieren darf. Wer
    einen 404 von Graph auffängt, ruft diese Funktion und arbeitet mit dem Ergebnis
    weiter -- oder gibt auf, wenn nichts gefunden wird.
    """
    if not internet_message_id:
        return None
    try:
        msg = await client.find_by_internet_message_id(internet_message_id)
    except Exception:  # noqa: BLE001 - best-effort
        logger.info(
            "Auflösung über internetMessageId fehlgeschlagen (%s)",
            str(internet_message_id)[:60],
        )
        return None
    if not msg:
        logger.info(
            "Keine Nachricht zu internetMessageId %s gefunden (gelöscht?)",
            str(internet_message_id)[:60],
        )
        return None
    return msg.get("id") or None


async def sync_message_id(
    db: AsyncSession,
    *,
    internet_message_id: str | None,
    new_message_id: str | None,
) -> None:
    """Schreibt ein frisches Handle in alle Zeilen mit dieser Identität zurück.

    Aufzurufen unmittelbar nach jedem Move, den TaskPilot selbst auslöst. Ohne
    diesen Schritt bleibt die Datenbank auf dem Handle des alten Ordners stehen --
    das war die Ursache der toten Task-Links.

    Betrifft nur einzelne Zeilen, der ``tasks_notify``-Trigger ist deshalb
    unproblematisch.
    """
    if not internet_message_id or not new_message_id:
        return
    await db.execute(
        update(Task)
        .where(Task.internet_message_id == internet_message_id)
        .values(email_message_id=new_message_id)
    )
    # ``email_triage`` traegt UNIQUE (user_id, message_id). Belegt eine andere Zeile
    # das neue Handle schon (die Mail wurde zwischenzeitlich erneut triagiert), wuerde
    # das Update die Transaktion des Aufrufers abbrechen. Darum vorher pruefen statt
    # den Fehler abfangen: eine gescheiterte Anweisung macht die Session unbrauchbar.
    taken = await db.scalar(
        select(EmailTriage.id).where(EmailTriage.message_id == new_message_id).limit(1)
    )
    if taken is None:
        await db.execute(
            update(EmailTriage)
            .where(EmailTriage.internet_message_id == internet_message_id)
            .values(message_id=new_message_id)
        )
    logger.info(
        "Handle nachgeführt: %s -> %s",
        str(internet_message_id)[:60], str(new_message_id)[:40],
    )


async def backfill_identities(db: AsyncSession, client, limit: int = 50) -> int:
    """Holt die Identität für Altdaten nach, die nur ein Handle tragen.

    Betrifft offene Tasks mit Quell-Mail: bei ihnen entscheidet die Identität, ob der
    Outlook-Link nach dem nächsten Move noch trägt. Erledigte Tasks bleiben aussen
    vor -- an ihnen ist nichts mehr zu reparieren, und jeder Aufruf kostet eine
    Graph-Anfrage.

    ``limit`` deckelt bewusst pro Lauf: Der Bestand wird über mehrere Zyklen
    abgearbeitet, statt in einem Rutsch gegen die Graph-Drosselung zu laufen. Gibt
    die Zahl der ergänzten Zeilen zurück; 0 heisst «fertig oder nichts zu tun».
    """
    rows = (
        await db.execute(
            select(Task)
            .where(
                Task.internet_message_id.is_(None),
                Task.email_message_id.is_not(None),
                Task.is_completed.is_(False),
            )
            .limit(limit)
        )
    ).scalars().all()
    if not rows:
        return 0

    filled = 0
    for task in rows:
        identity = await fetch_internet_message_id(client, task.email_message_id)
        if not identity:
            # Handle ist tot (Mail gelöscht oder längst verschoben). Ohne Identität
            # gibt es keinen Weg zurück -- das Feld bleibt leer, und der Task
            # behält sein wirkungsloses Handle. Ein leeres Feld ist ehrlicher als
            # ein geratener Wert.
            continue
        task.internet_message_id = identity
        filled += 1
    if filled:
        logger.info("Identität für %d Alt-Tasks nachgeholt", filled)
    return filled

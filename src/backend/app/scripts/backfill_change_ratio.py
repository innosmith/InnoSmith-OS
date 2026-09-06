"""Traegt das Aenderungsmass in die bereits erfassten Entwurfs-Rueckmeldungen nach.

Die Migration ``c1a5e37b90d4`` legt ``agent_feedback.change_ratio`` an, fuellt sie
aber nicht -- ein Datenumbau gehoert nicht in eine Migration. Ohne diesen Nachtrag
begaenne die Messreihe bei null, obwohl der ganze bisherige Bestand aus Entwurf und
versendeter Fassung berechenbar ist: beide liegen als ``original->>'body_html'``
und ``corrected->>'body_html'`` in derselben Zeile.

Das ist kein Raten. Es ist dieselbe Rechnung wie zur Laufzeit, nur spaeter
ausgefuehrt -- deshalb ruft das Skript ``compute_draft_diff`` auf und baut die
Formel nicht nach.

Idempotent: Zeilen mit gesetztem Wert werden uebersprungen. ``--neu-rechnen``
ueberschreibt sie, noetig nach einer Aenderung an der Messart.

Ausfuehren (Produktion):

    docker exec taskpilot-backend-prod python -m app.scripts.backfill_change_ratio

Ausgegeben werden ausschliesslich Zahlen -- Mailtext verlaesst den Container nicht.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from sqlalchemy import select

from app.database import async_session
from app.models import AgentFeedback
from app.services.learning import compute_draft_diff

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backfill_change_ratio")

# Nur diese beiden Arten vergleichen zwei Fassungen desselben Entwurfs. Eine
# geloeschte Aufgabe oder ein Daumen haben kein Aenderungsmass, und eine 0 dort
# waere keine Messung, sondern eine Behauptung.
GEMESSENE_ARTEN = ("draft_edit", "approved_clean")


async def backfill(neu_rechnen: bool = False) -> int:
    async with async_session() as db:
        bedingungen = [AgentFeedback.feedback_type.in_(GEMESSENE_ARTEN)]
        if not neu_rechnen:
            bedingungen.append(AgentFeedback.change_ratio.is_(None))

        zeilen = (
            await db.execute(select(AgentFeedback).where(*bedingungen))
        ).scalars().all()

        geschrieben = 0
        ohne_grundlage = 0
        for zeile in zeilen:
            entwurf = (zeile.original or {}).get("body_html")
            versendet = (zeile.corrected or {}).get("body_html")
            if not entwurf or not versendet:
                ohne_grundlage += 1
                continue
            zeile.change_ratio = compute_draft_diff(entwurf, versendet).change_ratio
            geschrieben += 1

        await db.commit()

    logger.info(
        "Nachtrag: %d Zeilen gemessen, %d ohne Vergleichsgrundlage uebersprungen",
        geschrieben, ohne_grundlage,
    )
    return geschrieben


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--neu-rechnen",
        action="store_true",
        help="Bereits gesetzte Werte ueberschreiben (nach Aenderung der Messart)",
    )
    args = parser.parse_args()
    asyncio.run(backfill(neu_rechnen=args.neu_rechnen))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Backtest des Move-Gates gegen echte Triage-Laeufe.

Antwortet auf zwei Fragen, die man vor dem Deploy kennen muss:

1. **Nutzen:** Welche Mails haette das neue Gate in der Inbox gehalten, die vorher
   in einen Unterordner verschwunden sind?
2. **Preis:** Welche Moves entfallen kuenftig, die richtig waren? Jeder davon ist
   eine Mail mehr in der Inbox -- das ist der Aufwand, den die Sicherheit kostet.

Das Gate haengt an einem Fakt aus dem Postfach ("hat Anthony dieser Adresse je
geschrieben?"), darum braucht der Backtest Graph-Zugriff und laeuft im Backend-
Container -- dort liegen DB- und Graph-Zugangsdaten schon bereit. Rein lesend:

    docker exec -i taskpilot-backend-prod python - 30 < scripts/eval/check_move_gate.py

Der Korrespondenz-Nachweis wird pro Adresse nur einmal geholt und danach
zwischengespeichert; ein Lauf ueber 30 Tage kostet damit einige Dutzend
Graph-Suchen, nicht eine pro Mail.
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.database import async_session
from app.services.hermes_worker import _build_graph_client, _is_known_correspondent
from app.services.triage_labels import folder_for_label, move_suppressed_reason

QUERY = text(
    """
    SELECT et.created_at::date AS tag,
           coalesce(et.from_address, '') AS absender,
           coalesce(et.subject, '') AS betreff,
           coalesce(et.triage_class, '') AS klasse,
           coalesce(et.suggested_action->>'label', '') AS label,
           coalesce(et.suggested_action->>'needs_review', '') AS review
    FROM email_triage et
    WHERE et.created_at > now() - make_interval(days => :days)
      AND et.suggested_action->>'label' IS NOT NULL
    ORDER BY et.created_at
    """
)


def _moved_before(row) -> bool:
    """Politik VOR der Aenderung: Label mit Zielordner + fyi + kein needs_review."""
    if folder_for_label(row.label) is None:
        return False
    if row.klasse != "fyi":
        return False
    return row.review != "true"


async def main() -> int:
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30

    async with async_session() as db:
        rows = (await db.execute(QUERY, {"days": days})).all()

    client = await _build_graph_client()
    if client is None:
        print("Kein Graph-Client -- Zugangsdaten fehlen.")
        return 1

    bekannt: dict[str, bool | None] = {}
    try:
        for row in rows:
            adresse = row.absender.strip().lower()
            if adresse not in bekannt:
                bekannt[adresse] = await _is_known_correspondent(client, adresse)
    finally:
        await client.close()

    verschoben = [r for r in rows if _moved_before(r)]
    gruende = {
        id(r): move_suppressed_reason(
            r.label,
            r.klasse,
            known_correspondent=bekannt.get(r.absender.strip().lower()),
        )
        for r in verschoben
    }
    blockiert = [r for r in verschoben if gruende[id(r)]]

    print(f"{len(rows)} kategorisierte Mails in {days} Tagen, {len(bekannt)} Absender")
    print(f"Bisher verschoben: {len(verschoben)}")
    print(f"Kuenftig in der Inbox gehalten: {len(blockiert)}\n")

    print("--- Moves, die entfallen ---")
    for r in blockiert:
        print(
            f"{r.tag} | {r.label:<10} | {r.absender[:38]:<38} | "
            f"{gruende[id(r)]:<38} | {r.betreff[:44]}"
        )

    print("\n--- Moves, die weiterhin laufen, nach Absender ---")
    absender: dict[str, int] = {}
    for r in verschoben:
        if not gruende[id(r)]:
            absender[r.absender or "?"] = absender.get(r.absender or "?", 0) + 1
    for adresse, anzahl in sorted(absender.items(), key=lambda kv: -kv[1]):
        print(f"{anzahl:>3}x {adresse}")

    kontakte = sum(1 for v in bekannt.values() if v is True)
    ungeprueft = sum(1 for v in bekannt.values() if v is None)
    print(
        f"\nAbsender mit eigener gesendeter Post: {kontakte} von {len(bekannt)}"
        f" ({ungeprueft} nicht pruefbar -- diese blockieren den Move fail-closed)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

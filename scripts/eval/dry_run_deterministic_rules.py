"""Trockenlauf der deterministischen Triage-Regeln gegen den echten Mailbestand.

Beantwortet die einzige Frage, die vor dem Aktivieren einer Regel zählt: **Welche
Mails hätte sie gegriffen, und war das jedes Mal richtig?** Eine Absenderregel greift
auf jede Mail dieser Adresse -- eine Regel, die den Bestand zu 90 % richtig einordnet,
ist keine Regel, sondern eine Vermutung mit Nebenwirkung.

Der Trockenlauf nutzt bewusst ``evaluate_conditions`` aus ``app.services.rules``,
also genau den Code, der später in ``apply_deterministic_rules`` entscheidet. Eine
nachgebaute SQL-Abfrage würde die Regel prüfen, die sie selbst formuliert, nicht die,
die läuft.

Ausführen (liest nur, schreibt nichts) -- ohne Zugangsdaten, weil ``psql`` im
Container die Daten holt und das Skript nur rechnet:

    docker exec -i taskpilot-postgres-prod psql -U taskpilot -d taskpilot_prod \\
        -tAf scripts/eval/dry_run_export.sql \\
      | .venv/bin/python scripts/eval/dry_run_deterministic_rules.py

Alternativ mit eigener Verbindung, wenn ein DSN vorliegt:

    .venv/bin/python scripts/eval/dry_run_deterministic_rules.py --dsn postgresql://...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
sys.path.insert(0, str(BACKEND))

from app.services.rules import evaluate_conditions  # noqa: E402

GELD_WOERTER = (
    "rechnung", "invoice", "mahnung", "zahlung", "payment", "receipt",
    "quittung", "abrechnung", "beleg", "kreditor",
)


def _als_graph_mail(zeile: dict) -> dict:
    """Baut aus einer ``email_triage``-Zeile die Form, die der Auswerter erwartet.

    ``evaluate_conditions`` liest Graph-Struktur (``from.emailAddress.address``),
    nicht Datenbankspalten. Die Umformung hier ist die einzige Stelle, an der der
    Trockenlauf von der Produktion abweicht -- und sie betrifft nur die Verpackung.
    """
    return {
        "id": zeile["message_id"],
        "subject": zeile["subject"] or "",
        "from": {"emailAddress": {"address": zeile["from_address"] or ""}},
    }


REGEL_ABFRAGE = """
    SELECT id, priority, rule_text, match_conditions, action
    FROM learned_rules
    WHERE rule_type = 'deterministic' AND status = 'active'
    ORDER BY priority, created_at
"""
MAIL_ABFRAGE = """
    SELECT message_id, subject, from_address, received_at,
           suggested_action->>'label' AS bisheriges_label
    FROM email_triage
    ORDER BY received_at
"""


async def _aus_datenbank(dsn: str) -> tuple[list[dict], list[dict]]:
    import asyncpg

    conn = await asyncpg.connect(dsn)
    try:
        regeln = [dict(r) for r in await conn.fetch(REGEL_ABFRAGE)]
        mails = [dict(m) for m in await conn.fetch(MAIL_ABFRAGE)]
    finally:
        await conn.close()
    return regeln, mails


def _von_stdin() -> tuple[list[dict], list[dict]]:
    """Liest das JSON, das ``dry_run_export.sql`` auf stdout schreibt."""
    daten = json.load(sys.stdin)
    return daten["regeln"], daten["mails"]


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dsn",
        default=os.environ.get("TP_DRY_RUN_DSN"),
        help="PostgreSQL-DSN; ohne Angabe wird JSON von stdin gelesen",
    )
    args = parser.parse_args()

    regeln, mails = (
        await _aus_datenbank(args.dsn) if args.dsn else _von_stdin()
    )

    treffer: dict[str, list[dict]] = defaultdict(list)
    ungetroffen = 0
    for mail in mails:
        graph_mail = _als_graph_mail(mail)
        for regel in regeln:
            bedingungen = regel["match_conditions"]
            if isinstance(bedingungen, str):
                bedingungen = json.loads(bedingungen)
            if evaluate_conditions(bedingungen, graph_mail):
                treffer[regel["rule_text"]].append(mail)
                break
        else:
            ungetroffen += 1

    print(f"{len(mails)} Mails im Bestand, {len(regeln)} aktive Regeln\n")
    fehlgriffe = 0
    for regel in regeln:
        getroffen = treffer.get(regel["rule_text"], [])
        aktion = regel["action"]
        if isinstance(aktion, str):
            aktion = json.loads(aktion)
        kategorie = aktion.get("category")
        print(f"[{regel['priority']:>3}] {regel['rule_text']}")
        if not getroffen:
            print("      keine Treffer im Bestand\n")
            continue

        # Plausibilisierung, kein Beweis: Bei einer Finanzregel muss der Betreff ein
        # Geldthema erkennen lassen. Wo nicht, ist die Regel zu breit -- genau der
        # Fehler, den die gestrichene Domain-Regel t-r.ch gemacht hat.
        verdaechtig = [
            m for m in getroffen
            if kategorie == "Finanzen"
            and not any(w in (m["subject"] or "").lower() for w in GELD_WOERTER)
        ]
        vorher = defaultdict(int)
        for m in getroffen:
            vorher[m["bisheriges_label"] or "(leer)"] += 1
        print(f"      {len(getroffen)} Treffer -> {kategorie}")
        print(f"      bisher: {dict(vorher)}")
        if verdaechtig:
            fehlgriffe += len(verdaechtig)
            print(f"      ACHTUNG {len(verdaechtig)} Treffer ohne Geldbezug im Betreff:")
            for m in verdaechtig[:5]:
                print(f"        - {m['from_address']}: {(m['subject'] or '')[:70]}")
        print()

    print(f"{ungetroffen} Mails von keiner Regel getroffen (gehen ans Modell)")
    if fehlgriffe:
        print(f"\n{fehlgriffe} fragliche Treffer -- Regeln prüfen, bevor sie laufen")
        return 1
    print("\nAlle Regeltreffer plausibel.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

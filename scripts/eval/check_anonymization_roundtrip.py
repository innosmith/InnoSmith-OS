#!/usr/bin/env python3
"""Prueft, ob die Cloud-Anonymisierung technische Texte unbeschadet uebersteht.

Vor dem ersten Cloud-Entwurf muss belegt sein, dass die Maskierung (a) Personen-
und Firmenbezuege wirklich entfernt und (b) technische Bezeichner nicht zerstoert.
Beides ist im E-Mail-Alltag entscheidend: eine Antwort an den IT-Partner eines
Kunden besteht zur Haelfte aus Hostnames, DNS-Records und Ports. Werden die als
``ORG`` maskiert und danach falsch zurueckgesetzt, entsteht genau die Sorte
Falschangabe, die am 04.08.2026 schon einmal in einen Entwurf geriet -- nur diesmal
mit Systemursache statt Modellphantasie.

Gemessen wird pro Testtext:

1. **Leck** -- welche echten Namen/Adressen stehen noch im maskierten Text? (Muss
   leer sein, sonst darf der Cloud-Pfad nicht aktiviert werden.)
2. **Round-Trip** -- ist ``deanonymize(anonymize(t)) == t``?
3. **Technischer Schaden** -- welche Hostnames, IPs und Mengen aus dem Original
   fehlen nach dem Round-Trip? (Nutzt ``text_style.factual_tokens``, also genau die
   Pruefung, die spaeter auch den Entwurf bewertet.)

Laeuft nur dort, wo ``ai9.content_converter`` konfiguriert ist -- in der Regel im
Backend-Container:

    docker exec -it taskpilot-backend python /app/scripts/eval/check_anonymization_roundtrip.py

Exit-Code 0 = alle Texte dicht und unbeschadet, 1 = mindestens ein Befund.
"""

from __future__ import annotations

import asyncio
import re
import sys
from pathlib import Path

# Im Container liegt das Backend unter /app, im Repo unter src/backend.
_here = Path(__file__).resolve()
for _candidate in (
    *(p / "src" / "backend" for p in _here.parents),
    Path("/app"),
):
    if (_candidate / "app" / "services" / "text_style.py").exists():
        if str(_candidate) not in sys.path:
            sys.path.insert(0, str(_candidate))
        break

from app.services.text_style import factual_tokens  # noqa: E402

# Testtexte mit dem, was in Anthonys Mails wirklich vorkommt. Der erste ist der
# echte Fall vom 30.07.2026 (Thread «Client-Zugang auf GX10 Server»), gekuerzt.
CASES: list[dict] = [
    {
        "name": "DNS/Zertifikate an IT-Partner",
        "text": (
            "Hoi Gabriel\n\n"
            "Besten Dank für die Rückmeldung. Von dir bräuchte ich pro App einen "
            "statischen Eintrag:\n"
            "_acme-challenge.app1.gsw.ch CNAME app1-gsw.challenge.innosmith.cloud\n"
            "Dazu pro App einen A-Record auf 192.168.230.60, Port 8443. "
            "Die Zertifikate laufen aktuell 200 Tage, ab März 100 Tage.\n\n"
            "Liebe Grüsse\nAnthony Smith, InnoSmith GmbH"
        ),
        "secrets": ["Gabriel", "Anthony Smith", "InnoSmith"],
    },
    {
        "name": "Rechnung mit Betrag und IBAN",
        "text": (
            "Guten Tag Frau Meier\n\n"
            "Die Rechnung 2026-0184 über CHF 4'850.00 ist am 15.08.2026 fällig. "
            "Zahlung auf CH93 0076 2011 6238 5295 7, Referenz 21 00000 00003 "
            "13947 14300 09017.\n\n"
            "Freundliche Grüsse\nAnthony Smith"
        ),
        "secrets": ["Meier", "Anthony Smith"],
    },
    {
        "name": "Server-Zugang mit Hostnames",
        "text": (
            "Hallo Team\n\n"
            "Der Zugang läuft über gx10.innosmith.ch (SSH Port 22022), die "
            "Datenbank hört auf 127.0.0.1:5435. Das Backend erreicht ihr unter "
            "https://taskpilot.innosmith.cloud/api/health.\n\n"
            "LG Anthony"
        ),
        "secrets": ["Anthony"],
    },
]


async def probe(case: dict) -> dict:
    from app.services.hermes_worker import (
        _anonymize_for_cloud,
        _deanonymize_from_cloud,
    )

    original = case["text"]
    anon, session_id = await _anonymize_for_cloud(original)
    restored = await _deanonymize_from_cloud(anon, session_id)

    # Wortgrenzen, und nicht innerhalb eines Hostnamens (``.innosmith.cloud``):
    # der Firmenname in der Domain ist beabsichtigt unmaskiert, damit technische
    # Bezeichner den Round-Trip ueberstehen. Geprueft wird der Klartext in
    # Anrede/Signatur/Fliesstext -- nicht der Domain-Bestandteil.
    leaks = [
        s for s in case["secrets"]
        if re.search(
            rf"(?<![A-Za-z0-9.]){re.escape(s)}(?![A-Za-z0-9.])", anon, re.I
        )
    ]
    before = factual_tokens(original)
    after = factual_tokens(restored)
    lost = [t for t in before if t not in after]

    return {
        "name": case["name"],
        "anon": anon,
        "restored": restored,
        "leaks": leaks,
        "roundtrip_exact": restored.strip() == original.strip(),
        "tokens_before": before,
        "lost_tokens": lost,
    }


async def main() -> int:
    findings = 0
    for case in CASES:
        print("=" * 72)
        print(f"FALL: {case['name']}")
        try:
            r = await probe(case)
        except Exception as exc:  # noqa: BLE001 - Diagnose, kein Produktionspfad
            print(f"  FEHLER: {type(exc).__name__}: {exc}")
            print("  -> Cloud-Entwuerfe duerfen so nicht aktiviert werden.")
            findings += 1
            continue

        print("\n--- maskiert ---")
        print(r["anon"])
        print("\n--- zurueckgesetzt ---")
        print(r["restored"])
        print()

        if r["leaks"]:
            print(f"  LECK: unmaskiert geblieben: {r['leaks']}")
            findings += 1
        else:
            print("  Maskierung: keine der erwarteten Bezuege im Klartext")

        if r["roundtrip_exact"]:
            print("  Round-Trip: identisch")
        else:
            print("  Round-Trip: WEICHT AB (Diff unten pruefen)")
            findings += 1

        if r["lost_tokens"]:
            print(f"  SCHADEN an technischen Angaben: {r['lost_tokens']}")
            findings += 1
        else:
            print(f"  Technische Angaben unversehrt ({len(r['tokens_before'])} geprueft)")

    print("=" * 72)
    print(f"Befunde: {findings}")
    return 0 if findings == 0 else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

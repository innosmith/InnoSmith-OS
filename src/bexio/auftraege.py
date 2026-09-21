"""Aufträge aus Bexio als normalisierte Tabelle -- geteilt zwischen Backend und MCP.

Das Gegenstück zu ``rechnungen.py`` für ``/2.0/kb_order``. Es existiert aus
demselben Grund: die Rohantwort trägt dieselben drei Fallen, und jede davon
erzeugt eine plausible Zahl statt einer Fehlermeldung.

1. ``GET /kb_order?contact_id=X`` filtert nicht -- derselbe ignorierte Parameter
   wie bei den Rechnungen. Die Einschränkung läuft über ``POST /kb_order/search``.
2. Ohne Blätterschleife kommen die ersten 50 Datensätze. Dass es heute nur 36
   Aufträge gibt, ist kein Schutz, sondern ein Zufall des Bestands.
3. ``kb_item_status_id`` ist eine Zahl ohne Bedeutung, die man ihr ansähe.
4. Alle Beträge kommen als Zeichenkette.

Anders als bei den Rechnungen ist der **Offset hier wirksam**: am 21.09.2026
gemessen mit ``limit=2&offset=0`` (Kennungen 2, 3) gegen ``offset=2``
(Kennungen 4, 5). Bei den Kreditoren war genau das nicht der Fall, dort blättert
nur ``page``. Geprüft statt angenommen.

## Wozu der Auftrag im Rechnungslauf dient

Er ist die dritte Blickrichtung auf dieselbe Lücke. Toggl gegen Entwurf zeigt
vergessene und überzählige Rechnungen; der Auftrag zeigt, ob überhaupt eine
Vereinbarung dahintersteht. Ein wiederkehrender Auftrag ohne Rechnung im Monat
ist ein Verdacht auf eine vergessene Fakturierung, ein abgeschlossener Auftrag
mit laufenden Stunden ein Verdacht auf eine fehlende Verlängerung.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger("taskpilot.bexio.auftraege")

SEITENGROESSE = 500

# Status eines Auftrags: Kennung -> (Beschriftung, gilt als laufend).
#
# Deklariert und nicht erschlossen -- aber anders als bei den Rechnungen gibt es
# für diese Zuordnung **keine** Auskunft der Schnittstelle: ``/kb_item_status``
# und ``/kb_order_status`` antworten mit 404. Die Lesart stützt sich allein auf
# den Bestand vom 21.09.2026, und dort ist sie eindeutig: alle dreizehn Aufträge
# mit Kennung 5 stammen aus der laufenden Kundschaft (Cheetah, deinklima,
# OnboardingQS, COflow …), alle dreiundzwanzig mit Kennung 6 sind Altbestand aus
# 2020 bis 2025 oder ausgelaufene Mandate.
#
# Eine Ausnahme widerspricht dem und ist deshalb hier vermerkt statt geglättet:
# AU-00030 «Administrative Unterstützung» trägt 6, obwohl 2026 noch 144 Stunden
# darauf gebucht wurden. Genau solche Fälle soll die Lückenprüfung melden.
AUFTRAGSSTATUS: dict[int, tuple[str, bool]] = {
    5: ("offen", True),
    6: ("abgeschlossen", False),
}


def status_beschriften(status_id: object) -> tuple[str, bool]:
    """Statuskennung in Beschriftung und «läuft noch» übersetzen.

    Eine unbekannte Kennung gilt **nicht** als abgeschlossen. Das ist die
    vorsichtige Richtung: ein laufender Auftrag, den niemand sieht, kostet Geld;
    ein abgeschlossener, der gemeldet wird, kostet einen Blick.
    """
    try:
        kennung = int(status_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unbekannt", True
    treffer = AUFTRAGSSTATUS.get(kennung)
    if treffer is None:
        return f"unbekannt_{kennung}", True
    return treffer


def betrag(wert: object) -> float:
    """Einen Bexio-Betrag in eine Zahl wandeln. Unlesbares ergibt 0.0."""
    try:
        return float(wert)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Auftrag:
    """Eine Auftragszeile, alle Entscheidungen bereits getroffen."""

    auftrag_id: int | None
    nummer: str
    titel: str
    kunden_id: int | None
    datum: str | None
    status: str
    laeuft: bool
    wiederkehrend: bool
    total: float
    waehrung_id: int | None


def auftragszeilen(rohdaten: list[dict]) -> tuple[list[Auftrag], dict]:
    """Rohantworten in Auftragszeilen wandeln, samt Befund über das Verworfene.

    Der Befund zählt die unbekannten Statuskennungen. Ohne ihn sähe eine neue
    Kennung -- Bexio kann sie jederzeit einführen -- wie ein normaler Auftrag
    aus, und die Lückenprüfung meldete lautlos das Falsche.
    """
    zeilen: list[Auftrag] = []
    unbekannt: dict[str, int] = {}

    for roh in rohdaten:
        beschriftung, laeuft = status_beschriften(roh.get("kb_item_status_id"))
        if beschriftung.startswith("unbekannt"):
            unbekannt[beschriftung] = unbekannt.get(beschriftung, 0) + 1
        zeilen.append(Auftrag(
            auftrag_id=roh.get("id"),
            nummer=roh.get("document_nr") or "",
            titel=roh.get("title") or "",
            kunden_id=roh.get("contact_id"),
            datum=roh.get("is_valid_from"),
            status=beschriftung,
            laeuft=laeuft,
            wiederkehrend=bool(roh.get("is_recurring")),
            total=betrag(roh.get("total")),
            waehrung_id=roh.get("currency_id"),
        ))

    befund: dict = {"auftraege": len(zeilen), "laufend": sum(1 for z in zeilen if z.laeuft)}
    if unbekannt:
        befund["unbekannte_status"] = unbekannt
        logger.warning("Aufträge mit unbekanntem Status: %s", unbekannt)
    return zeilen, befund

"""Lieferantenrechnungen aus Bexio als normalisierte Tabelle.

Das Gegenstueck zu ``rechnungen.py``: dort was hereinkommt, hier was hinausgeht.
Die Rohantwort von ``/4.0/purchase/bills`` hat dieselbe Gattung Fallen wie
``kb_invoice``, nur an anderen Stellen. Alle vier wurden am 03.09.2026 gegen den
Echtbestand (435 Rechnungen, 2019--2026) gemessen:

1. **``offset`` wird stillschweigend ignoriert.** ``offset=50`` liefert exakt
   dieselben Zeilen wie ``offset=0``. Geblaettert wird ueber ``page``. Wer es nicht
   weiss, laedt fuenfmal die erste Seite und haelt 100 Zeilen fuer 435.
2. **``gross`` ist nicht der Bruttobetrag.** Bei allen 435 Rechnungen ist ``gross``
   identisch mit ``net``, und ``tax_id`` ist durchgehend leer: dieses Konto verbucht
   Kreditoren ohne Vorsteuer. Es gibt genau **einen** Betrag. Siehe ``BETRAGSLESART``.
3. **Die Liste kennt den Lieferanten nur als Text.** ``supplier_id`` steht
   ausschliesslich im Einzelabruf. Der freie Name ist als Gruppierungsschluessel
   untauglich -- dieselbe Lehre, die bei Pipedrive den Umsatz eines Amts vervierfacht
   hat.
4. **Fremdwaehrung hat in der Liste keinen CHF-Gegenwert.** ``base_currency_amount``
   steht nur im Einzelabruf. Drei EUR-Rechnungen ohne Umrechnung sind eine kleine
   Zahl -- und ein Fehler, der mit jedem Auslandsabo waechst.

Daraus folgt die Bauform: Liste blaettern, jede Zeile einzeln nachladen, und das
Ergebnis so zurueckgeben, dass jede Entscheidung schon als Spalte festgehalten ist.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("taskpilot.bexio.kreditoren")

# Status einer Lieferantenrechnung: Kennung -> (Beschriftung, gilt als offen).
#
# Anders als bei den Debitoren ist der Status hier eine Zeichenkette, keine Zahl.
# Im Bestand vom 03.09.2026 kamen genau zwei Werte vor: PAID (432) und SENT (3).
# ``DRAFT`` ist deklariert, weil Bexio ihn kennt -- er kommt hier nur nicht vor,
# und eine Kennung, die eines Tages auftaucht, soll nicht als unbekannt gelten.
KREDITORENSTATUS: dict[str, tuple[str, bool]] = {
    "DRAFT": ("entwurf", False),
    "SENT": ("offen", True),
    "PAID": ("bezahlt", False),
}

# Wie der Betrag zu lesen ist -- als Text, damit er im Katalog erscheint und nicht
# in einem Kommentar verschwindet, den kein Modell je sieht.
BETRAGSLESART = (
    "Kreditoren dieses Kontos werden ohne Vorsteuer verbucht: 'net' und 'gross' sind "
    "bei allen Rechnungen identisch, 'tax_id' ist leer. Es gibt deshalb genau einen "
    "Betrag und keine Steuerspalte. Fuer Summen ueber mehrere Waehrungen ist "
    "'betrag_chf' zu nehmen, nie 'betrag'."
)


def status_beschriften(status: object) -> tuple[str, bool]:
    """Statuszeichenkette in Beschriftung und Offenheit uebersetzen.

    Unbekannte Werte werden weder verworfen noch stillschweigend einsortiert: die
    Zeile bleibt erhalten und traegt ``unbekannt_<wert>``. Der Aufrufer zaehlt sie
    und meldet sie.
    """
    if not status:
        return "unbekannt", False
    schluessel = str(status).strip().upper()
    treffer = KREDITORENSTATUS.get(schluessel)
    if treffer is None:
        return f"unbekannt_{schluessel.lower()}", False
    return treffer


def betrag(wert: object) -> float:
    """Betrag in eine Zahl wandeln; leer und fehlerhaft als 0.0."""
    if wert is None or wert == "":
        return 0.0
    try:
        return float(wert)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Kreditorenbetrag nicht lesbar, als 0.0 gewertet: %r", wert)
        return 0.0


@dataclass
class Kreditorenbestand:
    """Ergebnis eines Abgleichs: die Zeilen und was dabei auffiel."""

    zeilen: list[dict] = field(default_factory=list)
    gemeldet: int = 0
    """Was Bexio als ``paging.item_count`` nennt -- die Sollzahl."""
    ohne_detail: list[str] = field(default_factory=list)
    """Rechnungen, deren Einzelabruf misslang. Sie fehlen mit Lieferant und Konto."""
    unbekannte_status: dict[str, int] = field(default_factory=dict)
    waehrungen: list[str] = field(default_factory=list)
    ohne_umrechnung: int = 0
    """Fremdwaehrungszeilen ohne CHF-Gegenwert -- dort ist jede Summe falsch."""

    @property
    def vollstaendig(self) -> bool:
        return bool(self.gemeldet) and len(self.zeilen) == self.gemeldet


async def kontonamen(client) -> dict[int, dict]:
    """Kontenverzeichnis holen, wenn der Aufrufer keines mitbringt.

    Ohne diese Zuordnung bleibt ``booking_account_id`` eine nackte Zahl, und die
    Frage «wofuer geben wir Geld aus» ist aus Bexio heraus nicht beantwortbar.
    Faellt der Abruf aus, bleibt die Kennung roh stehen -- sichtbar unbrauchbar
    statt unsichtbar falsch.
    """
    from stammdaten import kontenplan_laden

    try:
        _, verzeichnis = await kontenplan_laden(client)
    except Exception as exc:  # noqa: BLE001 -- Ausfall ist eingeplant
        logger.warning("Kontenplan nicht abrufbar (%s), Kontonummern bleiben roh", exc)
        return {}
    return verzeichnis


def _kontierung(einzeln: dict, namen: dict[int, dict]) -> tuple[str, str, int]:
    """Konto der groessten Position, dessen Name und die Anzahl Positionen.

    Fast alle Rechnungen haben genau eine Position (428 von 435 am 03.09.2026).
    Bei mehreren waere jede Wahl willkuerlich, also wird die betragsmaessig
    groesste genommen **und die Anzahl mitgefuehrt** -- damit eine Auswertung nach
    Konto erkennen kann, wo sie vereinfacht.
    """
    from stammdaten import konto_beschriftung

    positionen = einzeln.get("line_items") or []
    if not positionen:
        return "", "", 0
    groesste = max(positionen, key=lambda p: abs(betrag(p.get("amount"))))
    kennung = groesste.get("booking_account_id")
    if kennung is None:
        return "", "", len(positionen)
    eintrag = namen.get(int(kennung))
    if not eintrag:
        return "", f"id {kennung}", len(positionen)
    return str(eintrag.get("konto_nr") or ""), konto_beschriftung(eintrag), len(positionen)


async def lieferantenrechnungen_laden(client, konten: dict[int, dict] | None = None) -> Kreditorenbestand:
    """Alle Lieferantenrechnungen als normalisierte Zeilen laden.

    Zwei Durchgaenge, weil die Liste allein nicht genuegt: erst blaettern, dann
    jede Zeile einzeln nachladen. Der Bestand ist klein (Hunderte), der Vollabruf
    dauerte am 03.09.2026 rund acht Sekunden -- fuer einen taeglichen Abgleich
    unerheblich, und er macht jede spaetere Frage ohne weiteren Abruf beantwortbar.

    ``konten`` kommt vom Aufrufer, wenn er den Kontenplan ohnehin schon geladen
    hat -- der Datenraum tut das fuers Journal. Ihn ein zweites Mal zu holen waere
    nicht nur Verschwendung: zwei Abrufe koennen auseinanderlaufen, und dann traegt
    dieselbe Kontokennung in zwei Tabellen zwei Namen.
    """
    roh, gemeldet = await client.alle_lieferantenrechnungen()
    kennungen = [str(r.get("id")) for r in roh if r.get("id")]
    details, misslungen = await client.alle_lieferantenrechnungen_detailliert(kennungen)
    namen = konten if konten is not None else await kontonamen(client)

    bestand = Kreditorenbestand(gemeldet=gemeldet, ohne_detail=misslungen)
    gesehene_waehrungen: set[str] = set()

    for r in roh:
        kennung = str(r.get("id") or "")
        einzeln = details.get(kennung, {})

        status, ist_offen = status_beschriften(r.get("status"))
        if status.startswith("unbekannt"):
            bestand.unbekannte_status[status] = bestand.unbekannte_status.get(status, 0) + 1

        waehrung = str(r.get("currency_code") or "unbekannt")
        gesehene_waehrungen.add(waehrung)

        # Ein Betrag, kein Brutto/Netto-Paar -- siehe BETRAGSLESART.
        wert = betrag(r.get("net"))
        if waehrung == "CHF":
            wert_chf = wert
        else:
            wert_chf = betrag(einzeln.get("base_currency_amount"))
            if not wert_chf:
                bestand.ohne_umrechnung += 1
                wert_chf = 0.0

        konto_nr, konto, positionen = _kontierung(einzeln, namen)

        bestand.zeilen.append({
            "rechnung_id": kennung,
            "nummer": r.get("document_no") or "",
            "datum": r.get("bill_date") or None,
            "faellig_am": r.get("due_date") or None,
            "lieferant_id": einzeln.get("supplier_id"),
            "lieferant": r.get("vendor") or "ohne Lieferant",
            "titel": r.get("title") or "",
            "betrag": wert,
            "betrag_chf": wert_chf,
            "waehrung": waehrung,
            "kurs": betrag(einzeln.get("exchange_rate")) or None,
            "offen_betrag": betrag(r.get("pending_amount")),
            "status": status,
            "ist_offen": ist_offen,
            "ueberfaellig": bool(r.get("overdue")),
            "konto_nr": konto_nr,
            "konto": konto,
            "positionen": positionen,
            "erfasst_am": r.get("created_at") or None,
        })

    bestand.waehrungen = sorted(gesehene_waehrungen)

    if not bestand.vollstaendig:
        logger.warning(
            "Kreditoren unvollstaendig: %d Zeilen geladen, %d gemeldet",
            len(bestand.zeilen), bestand.gemeldet,
        )
    if bestand.ohne_detail:
        logger.warning("%d Lieferantenrechnungen ohne Einzelabruf", len(bestand.ohne_detail))
    if bestand.unbekannte_status:
        logger.warning("Kreditoren mit unbekanntem Status: %s", bestand.unbekannte_status)
    if bestand.ohne_umrechnung:
        logger.warning(
            "%d Fremdwaehrungszeilen ohne CHF-Gegenwert -- betrag_chf ist dort 0",
            bestand.ohne_umrechnung,
        )
    return bestand

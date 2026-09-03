"""Das Buchungsjournal als normalisierte Tabelle -- das vollstaendige Ausgabenbild.

Warum es diese Tabelle braucht, obwohl es ``kreditoren.py`` schon gibt: weil
``/4.0/purchase/bills`` **nicht** das Aufwandsjournal ist, sondern nur der Teil
davon, der als Lieferantenrechnung erfasst wurde. Am 03.09.2026 gegen den
Echtbestand gemessen:

* 2025: Aufwand insgesamt 401'459 CHF, davon ueber den Kreditorenweg (Haben 2000)
  88'177 CHF -- **22 Prozent**. 2026 bis September: 266'476 gegen 56'056 CHF,
  21 Prozent. Alle Zahlen aus ``betrag_chf``; ``betrag`` steht in
  Buchungswaehrung und ergibt bei 250 Fremdwaehrungsbuchungen zu viel.
* Von 299 Aufwandsbuchungen des Jahres 2026 tragen 34 die Herkunft
  ``lieferantenrechnung``; 245 sind manuell erfasst.
* Mit Karte bezahlte Abonnements erreichen die Buchhaltung sehr wohl, nur anders:
  ``Soll 6570 Software / Haben 2120 Kontokorrent Gesellschafter``, dazu eine
  zweite Zeile ``Haben 2203 Bezugsteuer`` fuer die Steuer auf der
  Auslandsleistung. Cursor steht 2026 mit 31 Rechnungen und 12'924 CHF im
  Journal und mit **null** in ``bexio_kreditoren``.

Wer also Ausgaben aus den Lieferantenrechnungen summiert, bekommt ein Fuenftel --
und keine Fehlermeldung. Das Journal ist die vollstaendige Antwort.

Drei Fallen, die beim Bauen zu beachten sind:

1. **Nur die Sollseite summieren.** Jede Buchung nennt zwei Konten. ``1021
   Geschaeftskonto`` als Sollkonto bedeutet Geldeingang, nicht Aufwand. Wer beide
   Seiten addiert, zaehlt jeden Betrag doppelt.
2. **Blaettern ist zu pruefen, nicht anzunehmen.** ``offset`` wird von
   ``/4.0/purchase/bills`` stillschweigend ignoriert. Der groesste Jahrgang hier
   hat 809 Buchungen bei einem Limit von 2000 -- es wurde also nie geblaettert und
   die Annahme nie geprueft. ``_blaettern_pruefen`` fordert deshalb einmal zwei
   kleine Seiten an und vergleicht sie.
3. **Die Bezugsteuer verdoppelt die Anzahl, nicht den Betrag.** Eine Leistung aus
   dem Ausland erzeugt zwei Aufwandsbuchungen auf demselben Sollkonto: den
   Rechnungsbetrag gegen den Zahlweg und die Bezugsteuer gegen ``2203``. Cursor
   2026 steht deshalb mit 62 Buchungen da und hat 31 Rechnungen: 11'956 CHF plus
   968 CHF Steuer. Fuer Summen ist beides richtig -- dieses Konto zieht keine
   Vorsteuer ab, die Steuer ist echter Aufwand --, fuer Anzahlen nicht.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from stammdaten import AUFWANDSKLASSEN, konto_beschriftung

logger = logging.getLogger("taskpilot.bexio.journal")

# Woher eine Buchung stammt: Bexios ``ref_class`` -> lesbare Kennung.
#
# Uebersetzt, weil «KbClientAccountEntry» einem Modell nichts sagt und ein Filter
# darauf geraten waere. Die Menge ist geschlossen; ein neuer Wert wird als
# ``unbekannt_<wert>`` gefuehrt statt stillschweigend einsortiert.
HERKUNFT: dict[str, str] = {
    "KbBill": "lieferantenrechnung",
    "KbInvoice": "kundenrechnung",
    "KbClientAccountEntry": "zahlung",
}
HERKUNFT_MANUELL = "manuell"
"""``ref_class`` ist leer: von Hand erfasste Buchung ohne Beleg im System.
Das ist der Normalfall, nicht die Ausnahme -- 245 von 299 Aufwandsbuchungen 2026."""


def betrag(wert: object) -> float:
    """Betrag in eine Zahl wandeln; leer und fehlerhaft als 0.0."""
    if wert is None or wert == "":
        return 0.0
    try:
        return float(wert)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Journalbetrag nicht lesbar, als 0.0 gewertet: %r", wert)
        return 0.0


def herkunft_beschriften(ref_class: object) -> str:
    """``ref_class`` in eine lesbare Kennung uebersetzen."""
    if not ref_class:
        return HERKUNFT_MANUELL
    schluessel = str(ref_class).strip()
    return HERKUNFT.get(schluessel, f"unbekannt_{schluessel.lower()}")


@dataclass
class Journalbestand:
    """Ergebnis eines Journalabgleichs: die Zeilen und was dabei auffiel."""

    zeilen: list[dict] = field(default_factory=list)
    jahre: list[int] = field(default_factory=list)
    blaettern_geprueft: bool = False
    """Ob ``offset`` nachweislich wirkt. Ungeprueft ist nicht dasselbe wie kaputt --
    aber es ist auch nicht dasselbe wie in Ordnung."""
    blaettern_wirkungslos: bool = False
    jahre_am_limit: list[int] = field(default_factory=list)
    """Jahrgaenge, deren Zeilenzahl genau dem Abrufslimit entspricht. Dort ist ein
    abgeschnittener Bestand nicht von einem vollstaendigen zu unterscheiden."""
    ohne_konto: int = 0
    """Buchungen, deren Konto sich nicht aufloesen liess -- dort ist jede
    Gruppierung nach Konto unvollstaendig."""
    unbekannte_herkunft: dict[str, int] = field(default_factory=dict)


async def _blaettern_pruefen(client, von: str, bis: str) -> tuple[bool, bool]:
    """Einmal pruefen, ob ``offset`` im Journal ueberhaupt wirkt.

    Zwei kleine Seiten anfordern und die Kennungen vergleichen. Ueberschneiden
    sie sich vollstaendig, wird ``offset`` ignoriert -- dann ist jeder Jahrgang,
    der das Limit erreicht, abgeschnitten und nicht vollstaendig.

    Gibt ``(geprueft, wirkungslos)`` zurueck. Bei zu wenigen Buchungen im Zeitraum
    ist die Probe nicht aussagekraeftig und meldet ``(False, False)``: eine
    unbeantwortete Frage als beantwortet auszugeben, waere schlimmer als offen zu
    lassen, dass sie offen ist.
    """
    try:
        erste = await client.list_journal(von, bis, limit=5, offset=0)
        zweite = await client.list_journal(von, bis, limit=5, offset=5)
    except Exception as exc:  # noqa: BLE001 -- die Probe darf den Abgleich nicht kippen
        logger.warning("Blaetter-Probe im Journal misslungen: %s", exc)
        return False, False

    if len(erste) < 5 or not zweite:
        return False, False

    kennungen = {e.get("id") for e in erste}
    return True, all(e.get("id") in kennungen for e in zweite)


async def journal_laden(client, konten: dict[int, dict], jahre: list[dict]) -> Journalbestand:
    """Alle Buchungen der bekannten Geschaeftsjahre als normalisierte Zeilen.

    Abgerufen wird **je Geschaeftsjahr** statt ueber einen Gesamtzeitraum. Das
    haelt jeden einzelnen Abruf klein genug, dass er ohne Blaettern auskommt, und
    macht eine Luecke am Jahr festmachbar statt am Gesamtbestand.
    """
    grenze = 2000
    bestand = Journalbestand()

    if jahre:
        neuestes = max(jahre, key=lambda j: j.get("von") or "")
        bestand.blaettern_geprueft, bestand.blaettern_wirkungslos = await _blaettern_pruefen(
            client, neuestes.get("von") or "", neuestes.get("bis") or ""
        )

    for jahr in jahre:
        von, bis = jahr.get("von"), jahr.get("bis")
        if not von or not bis:
            continue
        try:
            eintraege = await client.get_journal(von, bis, limit=grenze)
        except Exception as exc:  # noqa: BLE001 -- ein Jahr darf die anderen nicht mitreissen
            logger.warning("Journal %s nicht abrufbar: %s", jahr.get("jahr"), exc)
            continue

        if len(eintraege) >= grenze:
            bestand.jahre_am_limit.append(jahr.get("jahr"))

        bestand.jahre.append(jahr.get("jahr"))
        for e in eintraege:
            soll = konten.get(e.get("debit_account_id")) or {}
            haben = konten.get(e.get("credit_account_id")) or {}
            if not soll or not haben:
                bestand.ohne_konto += 1

            herkunft = herkunft_beschriften(e.get("ref_class"))
            if herkunft.startswith("unbekannt"):
                bestand.unbekannte_herkunft[herkunft] = (
                    bestand.unbekannte_herkunft.get(herkunft, 0) + 1
                )

            bestand.zeilen.append({
                "buchung_id": e.get("id"),
                "datum": str(e.get("date") or "")[:10] or None,
                "betrag": betrag(e.get("amount")),
                "betrag_chf": betrag(e.get("base_currency_amount")),
                "soll_konto_nr": soll.get("konto_nr", ""),
                "soll_konto": konto_beschriftung(soll),
                "haben_konto_nr": haben.get("konto_nr", ""),
                "haben_konto": konto_beschriftung(haben),
                "beschreibung": str(e.get("description") or ""),
                "herkunft": herkunft,
                # Aufwand ist eine Eigenschaft der SOLLSEITE, nicht der Buchung:
                # dasselbe Konto steht auf der Habenseite fuer das Gegenteil.
                "ist_aufwand": soll.get("klasse") in AUFWANDSKLASSEN,
            })

    bestand.zeilen.sort(key=lambda z: (z["datum"] or "", z["buchung_id"] or 0))

    if bestand.blaettern_wirkungslos and bestand.jahre_am_limit:
        logger.warning(
            "Journal: 'offset' wirkt nicht und die Jahrgaenge %s erreichen das "
            "Limit -- diese Jahre sind abgeschnitten",
            bestand.jahre_am_limit,
        )
    if bestand.ohne_konto:
        logger.warning(
            "%d Journalbuchungen ohne aufloesbares Konto", bestand.ohne_konto
        )
    if bestand.unbekannte_herkunft:
        logger.warning("Journal mit unbekannter Herkunft: %s", bestand.unbekannte_herkunft)
    return bestand

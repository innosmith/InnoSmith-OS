"""Rechnungen aus Bexio als normalisierte Tabelle -- geteilt zwischen Backend und MCP.

Warum diese Bibliothek existiert: Die Rohantwort von ``/2.0/kb_invoice`` ist für eine
Auswertung unbrauchbar, und zwar auf eine Weise, die nicht auffällt.

1. ``GET /kb_invoice?contact_id=X`` **filtert nicht.** Bexio nimmt den Parameter
   entgegen und ignoriert ihn. Am 02.09.2026 gemessen: 652 Rechnungen ungefiltert,
   652 mit Filter, während ``POST /kb_invoice/search`` korrekt 159 lieferte. Wer sich
   auf den GET-Filter verlässt, weist den Umsatz aller Kunden einem einzigen zu --
   ohne Fehlermeldung, mit plausibler Zahl.
2. Ohne Paginierungsschleife liefert die Liste die ersten 50 Datensätze von 652.
3. ``kb_item_status_id`` ist eine Zahl. Wer sie nicht entschlüsselt, zählt Entwürfe
   als Umsatz mit.
4. Alle Beträge kommen als **Zeichenkette**. Eine Summe über Zeichenketten ist in
   Python eine Verkettung, kein Fehler.
5. ``total_gross`` heisst brutto und ist es nicht -- es ist die Positionssumme vor
   Rabatt, nicht der Betrag inklusive Mehrwertsteuer. Siehe ``betraege()``.

Die Bibliothek beantwortet daher keine Fragen, sondern liefert eine Tabelle, in der
jede Entscheidung schon getroffen und als Spalte festgehalten ist -- insbesondere
``ist_umsatz``. Damit steht die Definition in den Daten und nicht in der Formulierung
einer Abfrage, und dieselbe Frage ergibt nächste Woche dieselbe Zahl.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger("taskpilot.bexio.rechnungen")

# Bexio liefert je Seite höchstens 2000 Datensätze; 500 ist ein ruhiger Kompromiss
# zwischen Anzahl Aufrufen und Antwortgrösse.
SEITENGROESSE = 500

# Status einer Rechnung: Kennung -> (Beschriftung, zählt als Umsatz).
#
# Bewusst deklariert und nicht erschlossen: die Zahlen tragen keine Bedeutung, die
# man ihnen ansehen könnte. Bestätigt am 02.09.2026 gegen den Echtbestand (7 einmal,
# 8 elfmal, 9 sechshundertvierzigmal).
#
# Ein Entwurf ist kein Umsatz -- er wurde nie gestellt. Eine offene Rechnung ist
# Umsatz: sie ist fakturiert, nur noch nicht bezahlt. Wer nach Geldeingang fragt,
# nutzt die Spalte ``bezahlt``.
RECHNUNGSSTATUS: dict[int, tuple[str, bool]] = {
    7: ("entwurf", False),
    8: ("offen", True),
    9: ("bezahlt", True),
}


def status_beschriften(status_id: object) -> tuple[str, bool]:
    """Statuskennung in Beschriftung und Umsatzrelevanz übersetzen.

    Unbekannte Kennungen werden **nicht** stillschweigend als Umsatz verbucht und
    auch nicht verworfen: die Zeile bleibt erhalten, trägt aber ``unbekannt_<id>``
    und zählt nicht mit. Der Aufrufer zählt diese Fälle und meldet sie -- ein
    stiller Verlust sähe aus wie «es gibt nichts».
    """
    try:
        kennung = int(status_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unbekannt", False
    treffer = RECHNUNGSSTATUS.get(kennung)
    if treffer is None:
        return f"unbekannt_{kennung}", False
    return treffer


def betrag(wert: object) -> float:
    """Bexio-Betrag (Zeichenkette) in eine Zahl wandeln, leer und fehlerhaft als 0.0."""
    if wert is None or wert == "":
        return 0.0
    try:
        return float(wert)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Betrag nicht lesbar, als 0.0 gewertet: %r", wert)
        return 0.0


def betraege(r: dict) -> tuple[float, float, float]:
    """Netto, Mehrwertsteuer und Bruttobetrag einer Rechnung.

    Hier liegt die vierte Falle, und sie ist die heimtückischste, weil der
    naheliegende Feldname der falsche ist: **``total_gross`` ist nicht der Betrag
    inklusive Mehrwertsteuer.** Es ist die Positionssumme vor Rabatt und stimmt
    deshalb bei jeder rabattfreien Rechnung exakt mit ``total_net`` überein --
    bei 565 von 651 Rechnungen des Bestands. Wer es als Brutto ausweist, meldet
    einen Bruttobetrag ohne Steuer, und niemand stutzt, weil die Zahl zum
    Nettobetrag passt.

    Aufgefallen ist es an einer Rechnung über netto 5625.00, auf die 6080.65
    bezahlt wurden -- genau 8.1 % mehr. Der Betrag inklusive Steuer steht in
    ``total``, die Steuer selbst in ``total_taxes``.

    Der Rückfallweg ``netto + mwst`` greift, falls ``total`` einmal fehlt; er ist
    hier ungefährlich, weil beide Summanden aus derselben Rechnung stammen.
    """
    netto = betrag(r.get("total_net"))
    mwst = betrag(r.get("total_taxes"))
    brutto = betrag(r.get("total")) or round(netto + mwst, 2)
    return netto, mwst, brutto


@dataclass
class Rechnungsbestand:
    """Ergebnis eines Abgleichs: die Zeilen und was dabei auffiel."""

    zeilen: list[dict] = field(default_factory=list)
    unbekannte_status: dict[str, int] = field(default_factory=dict)
    kunden_ohne_namen: int = 0
    waehrungen: list[str] = field(default_factory=list)
    ohne_mwst: int = 0
    """Rechnungen ohne ausgewiesene Steuer.

    Wird gezählt und gemeldet, damit «keine Mehrwertsteuer» eine Aussage über die
    Daten bleibt und nicht die Ausrede eines Modells für eine falsche Spalte wird.
    """


async def _alle_seiten(holen, **kwargs) -> list[dict]:
    """Paginiert, bis eine Seite kürzer als die Seitengrösse zurückkommt.

    Dasselbe Muster wie ``BexioClient.get_journal``. Ohne diese Schleife liefert
    jede Listenabfrage stillschweigend nur den Anfang.
    """
    alle: list[dict] = []
    offset = 0
    while True:
        seite = await holen(limit=SEITENGROESSE, offset=offset, **kwargs)
        alle.extend(seite)
        if len(seite) < SEITENGROESSE:
            return alle
        offset += SEITENGROESSE


async def kontaktnamen(client) -> dict[int, str]:
    """Kontakt-Kennung auf Anzeigename abbilden.

    Ein Durchgang über alle Kontakte statt eines Aufrufs je Rechnung: bei 652
    Rechnungen auf 50 Kunden wäre das der Unterschied zwischen zwei Abrufen und
    sechshundert.
    """
    namen: dict[int, str] = {}
    for k in await _alle_seiten(client.list_contacts):
        kennung = k.get("id")
        if kennung is None:
            continue
        teile = [str(k.get("name_1") or "").strip(), str(k.get("name_2") or "").strip()]
        namen[int(kennung)] = " ".join(t for t in teile if t) or f"Kontakt {kennung}"
    return namen


async def waehrungsnamen(client) -> dict[int, str]:
    """Währungskennung auf Kürzel abbilden; bei Fehlschlag bleibt die Kennung stehen.

    Zwei Wege, weil Bexio den Bestand unter ``/3.0/currencies`` führt und der
    naheliegende v2-Pfad mit einem Statusfehler antwortet -- was am 02.09.2026 dazu
    führte, dass in der Währungsspalte ``id:1`` stand statt ``CHF``.

    Fällt auch der zweite Weg aus, bleibt die Kennung roh stehen. Das ist sichtbar
    falsch und damit harmlos, anders als eine stillschweigend auf CHF gesetzte
    Fremdwährung.
    """
    fehler: list[str] = []
    for holen, pfad in ((client._get_v3, "/currencies"), (client._get_v2, "/currency")):
        try:
            daten = await holen(pfad, {"limit": "500"})
        except Exception as exc:  # noqa: BLE001 -- Ausfall ist eingeplant
            fehler.append(f"{pfad}: {type(exc).__name__}")
            continue
        if not isinstance(daten, list):
            fehler.append(f"{pfad}: unerwartete Antwort")
            continue
        namen: dict[int, str] = {}
        for w in daten:
            kennung, kuerzel = w.get("id"), w.get("name")
            if kennung is not None and kuerzel:
                namen[int(kennung)] = str(kuerzel)
        if namen:
            return namen
        fehler.append(f"{pfad}: leer")
    logger.warning("Währungen nicht abrufbar (%s), Kennungen bleiben roh", "; ".join(fehler))
    return {}


async def rechnungen_laden(client) -> Rechnungsbestand:
    """Alle Rechnungen als normalisierte Zeilen laden.

    Bewusst ohne Zeitraumfilter: der Bestand ist klein (Hunderte bis Tausende
    Zeilen), und ein vollständiger Abzug macht jede spätere Frage nach Vorjahr,
    Verlauf oder Gesamtsumme ohne weiteren Abruf beantwortbar.
    """
    roh = await _alle_seiten(client.list_invoices)
    namen = await kontaktnamen(client)
    waehrungen = await waehrungsnamen(client)

    bestand = Rechnungsbestand()
    gesehene_waehrungen: set[str] = set()

    for r in roh:
        status, ist_umsatz = status_beschriften(r.get("kb_item_status_id"))
        if status.startswith("unbekannt"):
            bestand.unbekannte_status[status] = bestand.unbekannte_status.get(status, 0) + 1

        kunden_id = r.get("contact_id")
        kunde = namen.get(int(kunden_id)) if kunden_id is not None else None
        if kunde is None:
            bestand.kunden_ohne_namen += 1
            kunde = f"Kontakt {kunden_id}" if kunden_id is not None else "ohne Kunde"

        waehrung_id = r.get("currency_id")
        waehrung = waehrungen.get(int(waehrung_id), f"id:{waehrung_id}") if waehrung_id is not None else "unbekannt"
        gesehene_waehrungen.add(waehrung)

        netto, mwst, brutto = betraege(r)
        if brutto and abs(brutto - netto) < 0.005 and mwst == 0.0:
            bestand.ohne_mwst += 1

        bestand.zeilen.append({
            "rechnung_id": r.get("id"),
            "nummer": r.get("document_nr") or "",
            "datum": r.get("is_valid_from") or None,
            "faellig_am": r.get("is_valid_to") or None,
            "kunden_id": kunden_id,
            "kunde": kunde,
            "titel": r.get("title") or "",
            "netto": netto,
            "mwst": mwst,
            "brutto": brutto,
            "bezahlt": betrag(r.get("total_received_payments")),
            "offen": betrag(r.get("total_remaining_payments")),
            "waehrung": waehrung,
            "status": status,
            "ist_umsatz": ist_umsatz,
            "geaendert_am": r.get("updated_at") or None,
        })

    bestand.waehrungen = sorted(gesehene_waehrungen)
    if bestand.unbekannte_status:
        logger.warning(
            "Rechnungen mit unbekanntem Status (zählen nicht als Umsatz): %s",
            bestand.unbekannte_status,
        )
    return bestand


async def kontakte_laden(client) -> list[dict]:
    """Kontakte als normalisierte Zeilen -- Stammdaten für Auswertungen."""
    zeilen = []
    for k in await _alle_seiten(client.list_contacts):
        teile = [str(k.get("name_1") or "").strip(), str(k.get("name_2") or "").strip()]
        zeilen.append({
            "kunden_id": k.get("id"),
            "name": " ".join(t for t in teile if t) or f"Kontakt {k.get('id')}",
            "nummer": k.get("nr") or "",
            "typ": "firma" if k.get("contact_type_id") == 1 else "person",
            "mail": k.get("mail") or "",
            "telefon": k.get("phone_fixed") or k.get("phone_mobile") or "",
            "ort": k.get("city") or "",
            "plz": k.get("postcode") or "",
            "land_id": k.get("country_id"),
            "ist_lead": bool(k.get("is_lead")),
            "geaendert_am": k.get("updated_at") or None,
        })
    return zeilen

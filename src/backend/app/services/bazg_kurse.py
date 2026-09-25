"""Monatsmittelkurse des BAZG -- der Kurs, mit dem eine Fremdwährung gebucht wird.

Die MWST verlangt für Fremdwährungen den Monatsmittelkurs des BAZG. Bis zum
25.09.2026 rechnete Bexio ihn selbst (Einstellung «Monatsmittelkurs»), und er
stimmte mit dem BAZG überein: Juli 0.8024, August 0.8175. Die Einstellung ist
seither aus, weil TaskPilot den Kurs mitschickt -- ohne mitgeschickten Kurs
nähme Bexio jetzt seine Handtabelle. Dass das kein Randfall ist, zeigen zwei
Rapid-API-Buchungen 2026: USD-Rechnungen, in CHF zum Kurs 1.0 gebucht.

Gibt es keinen Kurs, gibt es keine Buchung. Ein geschätzter Kurs hat in einer
MWST-Abrechnung nichts verloren, und er sähe aus wie ein richtiger.
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree

import httpx

logger = logging.getLogger(__name__)

URL = "https://www.backend-rates.bazg.admin.ch/api/xmlavgmonth"
_NS = {"b": "https://www.backend-rates.bazg.admin.ch/avgratesmonth"}

# Abgeschlossene Monate ändern sich nicht mehr; der laufende schon, solange er
# läuft. Deshalb nur vergangene Monate im Speicher.
_ABGESCHLOSSEN: dict[tuple[str, int, int], Decimal] = {}


class KursFehlt(RuntimeError):
    """Kein offizieller Kurs -- der Aufrufer bucht nicht."""


def kurs_lesen(xml: bytes, waehrung: str) -> Decimal | None:
    """Der Kurs für **eine** Einheit. Das BAZG nennt Yen je hundert."""
    try:
        wurzel = ElementTree.fromstring(xml)
    except ElementTree.ParseError as fehler:
        logger.error("BAZG-Antwort nicht lesbar: %s", fehler)
        return None
    for devise in wurzel.findall("b:devise", _NS):
        if devise.get("code", "").upper() != waehrung.upper():
            continue
        kurs_text = devise.findtext("b:kurs", default="", namespaces=_NS).strip()
        bezug = devise.findtext("b:waehrung", default="", namespaces=_NS).split()
        try:
            kurs = Decimal(kurs_text.replace(",", "."))
            einheit = Decimal(bezug[0]) if len(bezug) >= 2 else Decimal(1)
        except (InvalidOperation, IndexError):
            return None
        return kurs / einheit if einheit > 0 else None
    return None


async def monatsmittel(waehrung: str, tag: date, *, client: httpx.AsyncClient | None = None) -> Decimal:
    """Der Monatsmittelkurs des Monats, in dem ``tag`` liegt.

    ``KursFehlt``, wenn das BAZG nicht antwortet oder die Währung nicht kennt.
    """
    waehrung = waehrung.upper()
    if waehrung == "CHF":
        return Decimal(1)
    schluessel = (waehrung, tag.year, tag.month)
    if schluessel in _ABGESCHLOSSEN:
        return _ABGESCHLOSSEN[schluessel]

    eigener = client is None
    client = client or httpx.AsyncClient(timeout=20.0)
    try:
        antwort = await client.get(URL, params={"j": str(tag.year), "m": str(tag.month)})
        antwort.raise_for_status()
    except httpx.HTTPError as fehler:
        raise KursFehlt(f"BAZG nicht erreichbar ({fehler}) — ohne Kurs keine Buchung") from fehler
    finally:
        if eigener:
            await client.aclose()

    kurs = kurs_lesen(antwort.content, waehrung)
    if kurs is None:
        raise KursFehlt(f"Das BAZG nennt für {tag:%m.%Y} keinen {waehrung}-Kurs")
    heute = date.today()
    if (tag.year, tag.month) < (heute.year, heute.month):
        _ABGESCHLOSSEN[schluessel] = kurs
    return kurs

"""Rechnung und Leistungsrapport zu einem Dokument — und was dabei zu prüfen ist.

## Die Reihenfolge, die in der Vorlage eine Falle war

`D3_prepareInvoices.py` las die IBAN in einem eigenen Durchgang **vor** dem
Zusammenführen und trug sie in einen Zwischenspeicher, den es später wieder
einsetzte. Der Grund steht im Docstring der Vorlage: nach dem Merge ist die
letzte Seite der Leistungsrapport und nicht mehr der Einzahlungsschein.

Eine Reihenfolge, die man einhalten muss, wird irgendwann nicht eingehalten —
und der Fehler ist hier still: die IBAN-Prüfung fände einfach nichts und
meldete «keine IBAN erkannt», was wie ein Darstellungsproblem aussieht und
nicht wie eine übersprungene Prüfung.

Deshalb gibt es hier **eine** Funktion. ``zusammenfuehren`` liest die IBAN aus
der Rechnung und legt den Rapport an, in dieser Reihenfolge, und gibt beides
zurück. Die falsche Reihenfolge ist damit nicht mehr wählbar.

## Warum die IBAN überhaupt geprüft wird

Auf dem Einzahlungsschein steht, wohin gezahlt wird. Steht dort das falsche
Konto, merkt es niemand — die Rechnung sieht richtig aus, die Kundschaft zahlt,
und das Geld liegt woanders. Geprüft werden nur die ersten acht Zeichen
(Ländercode und Bankclearing, ``CH60 3080``); die vollständige Kontonummer
gehört weder in ein Protokoll noch in eine Fehlermeldung.
"""

from __future__ import annotations

import io
import logging
import re
from dataclasses import dataclass, field

logger = logging.getLogger("taskpilot.debitoren.dokumente")

# Schweizer IBAN auf dem Einzahlungsschein, mit oder ohne Gruppierung.
_IBAN = re.compile(r"CH\d{2}\s*\d{4}\s*\d{4}\s*\d{4}\s*\d{4}\s*\d")


@dataclass
class Dokument:
    """Das versandfertige PDF samt dem, was beim Zusammenbauen auffiel."""

    inhalt: bytes
    seiten: int
    iban_praefix: str | None = None
    """``CH60 3080`` — Ländercode und Bankclearing, nie mehr. ``None`` heisst
    «auf dem Einzahlungsschein nicht gefunden», nicht «stimmt nicht»."""
    hindernisse: list[str] = field(default_factory=list)


def iban_praefix(rechnung: bytes) -> str | None:
    """Die ersten acht Stellen der IBAN vom Einzahlungsschein.

    Gelesen wird die **letzte** Seite der Rechnung — dort steht der
    Einzahlungsschein. Diese Funktion darf nie ein zusammengeführtes Dokument
    sehen; dort wäre die letzte Seite der Leistungsrapport. ``zusammenfuehren``
    sorgt dafür, dass das nicht passieren kann.
    """
    import pymupdf

    try:
        with pymupdf.open(stream=rechnung, filetype="pdf") as pdf:
            if pdf.page_count == 0:
                return None
            text = pdf[pdf.page_count - 1].get_text()
    except Exception as exc:  # noqa: BLE001 - ein unlesbares PDF ist ein Befund, kein Absturz
        logger.warning("Einzahlungsschein nicht lesbar: %s", exc)
        return None

    treffer = _IBAN.search(text or "")
    if not treffer:
        return None
    roh = treffer.group(0).replace(" ", "")
    return f"{roh[:4]} {roh[4:8]}"


def zusammenfuehren(
    rechnung: bytes, rapport: bytes | None = None, *, erwartete_iban: str | None = None
) -> Dokument:
    """Rechnung und Leistungsrapport zu einem PDF — Rechnung zuerst.

    Der Rapport ist **optional**: bei einem Festpreis gibt es keinen, und das
    ist kein Mangel. Fehlt er, kommt die Rechnung unverändert zurück.

    ``erwartete_iban`` kommt aus den Stammdaten (``vorgaben.iban_praefix``).
    Weicht die gelesene ab, ist das ein Hindernis und kein Abbruch: die
    Entscheidung, ob eine Rechnung mit unerwartetem Konto trotzdem rausgeht,
    trifft ein Mensch. Ein stiller Abbruch mitten im Lauf wäre schlechter — er
    sähe aus wie ein technischer Fehler.
    """
    from pypdf import PdfReader, PdfWriter

    hindernisse: list[str] = []

    # Zuerst lesen, dann anbauen. Andersherum stünde auf der letzten Seite der
    # Rapport, und die Prüfung liefe ins Leere.
    gelesen = iban_praefix(rechnung)
    if gelesen is None:
        hindernisse.append(
            "Auf dem Einzahlungsschein wurde keine IBAN gefunden — "
            "das Konto ist damit ungeprüft."
        )
    elif erwartete_iban and gelesen != erwartete_iban:
        hindernisse.append(
            f"Der Einzahlungsschein zeigt {gelesen}, erwartet ist "
            f"{erwartete_iban}. Die Zahlung ginge auf ein anderes Konto."
        )

    if rapport is None:
        seiten = len(PdfReader(io.BytesIO(rechnung)).pages)
        return Dokument(
            inhalt=rechnung, seiten=seiten,
            iban_praefix=gelesen, hindernisse=hindernisse,
        )

    schreiber = PdfWriter()
    for teil in (rechnung, rapport):
        for seite in PdfReader(io.BytesIO(teil)).pages:
            schreiber.add_page(seite)

    puffer = io.BytesIO()
    schreiber.write(puffer)
    inhalt = puffer.getvalue()

    return Dokument(
        inhalt=inhalt,
        seiten=len(schreiber.pages),
        iban_praefix=gelesen,
        hindernisse=hindernisse,
    )

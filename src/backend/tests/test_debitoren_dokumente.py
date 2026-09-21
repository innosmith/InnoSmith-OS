"""Tests für das Zusammenführen von Rechnung und Leistungsrapport.

Der eigentliche Prüfgegenstand ist die **Reihenfolge**: die IBAN steht auf der
letzten Seite der Rechnung, und nach dem Anbauen des Rapports ist die letzte
Seite der Rapport. In der Vorlage war das eine Regel, die man einhalten musste.
Hier soll es strukturell nicht mehr schiefgehen können — genau das wird geprüft.
"""

from __future__ import annotations

import io

import pytest
from fpdf import FPDF
from pypdf import PdfReader

from app.services import debitoren_dokumente as dok


def pdf_mit(*seiten: str) -> bytes:
    """Ein schlichtes PDF mit einer Textzeile je Seite."""
    doc = FPDF()
    for text in seiten:
        doc.add_page()
        doc.set_font("Helvetica", size=12)
        doc.cell(0, 10, text)
    return bytes(doc.output())


RECHNUNG = pdf_mit("Rechnung RE-00706", "Einzahlungsschein CH60 3080 8001 2345 6789 0")
RAPPORT = pdf_mit("Leistungsrapport August 2026")


def test_iban_kommt_von_der_letzten_seite():
    assert dok.iban_praefix(RECHNUNG) == "CH60 3080"


def test_zusammengefuehrt_steht_die_rechnung_vorn():
    d = dok.zusammenfuehren(RECHNUNG, RAPPORT)

    assert d.seiten == 3
    seiten = PdfReader(io.BytesIO(d.inhalt)).pages
    assert "RE-00706" in seiten[0].extract_text()
    assert "Leistungsrapport" in seiten[2].extract_text()


def test_die_iban_wird_trotz_angebautem_rapport_gelesen():
    """Der Kern der Sache.

    In der Vorlage war die letzte Seite nach dem Merge der Rapport, und die
    IBAN-Prüfung fand nichts — was wie ein Darstellungsproblem aussieht und
    nicht wie eine übersprungene Prüfung. Hier liest ``zusammenfuehren``
    zuerst und baut dann an.
    """
    d = dok.zusammenfuehren(RECHNUNG, RAPPORT)

    assert d.iban_praefix == "CH60 3080"
    # Gegenprobe: am fertigen Dokument wäre sie nicht mehr zu finden.
    assert dok.iban_praefix(d.inhalt) is None


def test_abweichendes_konto_ist_ein_hindernis_kein_abbruch():
    """Ob eine Rechnung mit unerwartetem Konto rausgeht, entscheidet ein
    Mensch. Ein stiller Abbruch sähe aus wie ein technischer Fehler."""
    d = dok.zusammenfuehren(RECHNUNG, RAPPORT, erwartete_iban="CH99 1111")

    assert d.inhalt                       # das Dokument entsteht trotzdem
    assert len(d.hindernisse) == 1
    assert "CH60 3080" in d.hindernisse[0]
    assert "CH99 1111" in d.hindernisse[0]


def test_passendes_konto_meldet_nichts():
    d = dok.zusammenfuehren(RECHNUNG, RAPPORT, erwartete_iban="CH60 3080")

    assert d.hindernisse == []


def test_fehlende_iban_wird_benannt_statt_verschwiegen():
    """«Nichts gefunden» und «stimmt nicht» sind zwei verschiedene Aussagen —
    aber beide dürfen nicht unbemerkt bleiben."""
    ohne = pdf_mit("Rechnung RE-00707", "Diese Rechnung hat keinen Schein")

    d = dok.zusammenfuehren(ohne, RAPPORT, erwartete_iban="CH60 3080")

    assert d.iban_praefix is None
    assert "keine IBAN" in d.hindernisse[0]


def test_ohne_rapport_bleibt_die_rechnung_unveraendert():
    """Bei einem Festpreis gibt es keinen Rapport, und das ist kein Mangel."""
    d = dok.zusammenfuehren(RECHNUNG)

    assert d.inhalt == RECHNUNG
    assert d.seiten == 2
    assert d.iban_praefix == "CH60 3080"


def test_iban_wird_auch_ohne_gruppierung_erkannt():
    eng = pdf_mit("Rechnung", "Konto CH6030808001234567890 InnoSmith GmbH")

    assert dok.iban_praefix(eng) == "CH60 3080"


def test_unlesbares_pdf_ergibt_ein_hindernis_und_keinen_absturz():
    """Ein kaputtes PDF darf den Lauf nicht reissen — die anderen Rechnungen
    sind davon nicht betroffen."""
    assert dok.iban_praefix(b"kein PDF") is None


def test_kaputte_rechnung_beim_zusammenfuehren_fliegt_auf():
    """Hier allerdings schon: ein Dokument, das nicht gebaut werden kann,
    darf nicht als leeres Dokument weiterlaufen."""
    with pytest.raises(Exception):
        dok.zusammenfuehren(b"kein PDF", RAPPORT)

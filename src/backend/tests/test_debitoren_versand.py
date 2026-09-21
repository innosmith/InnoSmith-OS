"""Tests für das versandfertige Dokument und die Mailentwürfe dazu.

Zwei Dinge müssen stimmen, bevor etwas an die Kundschaft geht: **welche
Stunden** auf dem Rapport stehen, und **welches Datum** sie tragen. Alles
andere — Zusammenführen, IBAN, fehlender Vertrag — ist schon in den
Bausteinen geprüft. Hier zählt die Verkettung.
"""

from __future__ import annotations

from datetime import date

import pytest
from fpdf import FPDF
from pypdf import PdfReader
from io import BytesIO

from app.services import debitoren_versand as versand
from app.services.debitorenvertraege import Kunde, Vertragsdaten


def pdf_mit(*seiten: str) -> bytes:
    doc = FPDF()
    for text in seiten:
        doc.add_page()
        doc.set_font("Helvetica", size=12)
        doc.cell(0, 10, text)
    return bytes(doc.output())


RECHNUNG_PDF = pdf_mit(
    "Rechnung RE-00703",
    "Einzahlungsschein CH60 3080 8001 2345 6789 0",
)


def vertrag(**rest) -> Vertragsdaten:
    grund = dict(
        schluessel="onboardingqs",
        bezeichnung="OnboardingQS",
        kunde=Kunde(schluessel="tr", empfaenger="martin.roethlisberger@t-r.ch"),
        mwst=8.1,
        leistungsrapport=True,
    )
    return Vertragsdaten(**{**grund, **rest})


class FakeBestand:
    def __init__(self, *vertraege: Vertragsdaten, iban: str = "CH60 3080"):
        self.vertraege = {v.schluessel: v for v in vertraege}
        self.vorgaben = {"iban_praefix": iban}

    def finden(self, bezeichnung: str):
        for v in self.vertraege.values():
            if v.bezeichnung.casefold() in (bezeichnung or "").casefold():
                return v
        return None


class FakeBexio:
    def __init__(self, pdfs: dict[int, bytes] | None = None, scheitert: set[int] | None = None):
        self._pdfs = pdfs or {}
        self._scheitert = scheitert or set()
        self.abgerufen: list[int] = []

    async def get_invoice_pdf(self, invoice_id: int) -> bytes:
        self.abgerufen.append(invoice_id)
        if invoice_id in self._scheitert:
            raise RuntimeError("PDF nicht lesbar")
        return self._pdfs.get(invoice_id, RECHNUNG_PDF)


def buchung(tag: int, stunden: float, *, projekt="OnboardingQS",
            verrechenbar=True, beschreibung="Umsetzung", **rest) -> dict:
    return {
        "datum": f"2026-08-{tag:02d}",
        "beginn": f"2026-08-{tag:02d}T09:00:00+02:00",
        "projekt": projekt,
        "stunden": stunden,
        "verrechenbar": verrechenbar,
        "beschreibung": beschreibung,
        **rest,
    }


# ── Rapport ────────────────────────────────────────────────────────────────


def test_rapport_zeigt_nur_verrechenbare_stunden():
    """Die Prüfung zählt alle, der Rapport nur die verrechenbaren.

    Ein Fixvertrag verbraucht auch nicht verrechenbare Zeit — die gehört
    aber nicht auf das Dokument, das die Kundschaft sieht.
    """
    bestand = FakeBestand(vertrag())
    _pdf, rapport = versand.rapport_bauen(
        [
            buchung(3, 4.0, verrechenbar=True),
            buchung(4, 3.0, verrechenbar=False),
        ],
        bestand, "onboardingqs", jahr=2026, monat=8,
    )

    assert rapport is not None
    assert rapport.summe == 4.0
    assert [z.datum for z in rapport.zeilen] == ["03.08.2026"]


def test_iso_datum_wird_schweizerisch():
    """Ohne diese Umwandlung stünde «2026-08-14» auf einem Kundendokument."""
    _pdf, rapport = versand.rapport_bauen(
        [buchung(14, 2.0)], FakeBestand(vertrag()),
        "onboardingqs", jahr=2026, monat=8,
    )

    assert rapport.zeilen[0].datum == "14.08.2026"


def test_zeilen_stehen_in_kalenderreihenfolge():
    """Die Reports-API gruppiert, sie sortiert nicht nach Tag."""
    _pdf, rapport = versand.rapport_bauen(
        [buchung(19, 1.0, beschreibung="spaeter"),
         buchung(3, 2.0, beschreibung="frueher")],
        FakeBestand(vertrag()), "onboardingqs", jahr=2026, monat=8,
    )

    assert [z.taetigkeit for z in rapport.zeilen] == ["frueher", "spaeter"]


def test_ohne_verrechenbare_stunden_entsteht_kein_leerer_rapport():
    """Ein leerer Rapport behauptet, es sei nichts geleistet worden."""
    pdf, rapport = versand.rapport_bauen(
        [buchung(3, 2.0, verrechenbar=False)],
        FakeBestand(vertrag()), "onboardingqs", jahr=2026, monat=8,
    )

    assert pdf is None and rapport is None


def test_buchungen_eines_anderen_vertrags_bleiben_draussen():
    _pdf, rapport = versand.rapport_bauen(
        [buchung(3, 5.0, projekt="COflow"),
         buchung(4, 2.0, projekt="OnboardingQS")],
        FakeBestand(vertrag(), vertrag(schluessel="coflow", bezeichnung="COflow")),
        "onboardingqs", jahr=2026, monat=8,
    )

    assert rapport.summe == 2.0


# ── Dokumente ──────────────────────────────────────────────────────────────


ENTWURF = {
    "rechnung_id": 77, "nummer": "RE-00703", "titel": "OnboardingQS August 2026",
}


@pytest.mark.asyncio
async def test_dokument_haengt_den_rapport_an():
    bexio = FakeBexio()
    ergebnis = await versand.dokumente_bauen(
        bexio, entwuerfe=[ENTWURF],
        buchungen=[buchung(12, 2.5)],
        bestand=FakeBestand(vertrag()),
        jahr=2026, monat=8,
    )

    assert len(ergebnis.beilagen) == 1
    beilage = ergebnis.beilagen[0]
    assert beilage.mit_rapport is True
    assert beilage.rapport_stunden == 2.5
    assert beilage.seiten >= 3   # Rechnung 2 Seiten + Rapport
    seiten = PdfReader(BytesIO(beilage.dokument)).pages
    assert "RE-00703" in seiten[0].extract_text()
    assert "Leistungsrapport August 2026" in seiten[-1].extract_text()


@pytest.mark.asyncio
async def test_festpreis_ohne_rapport_bleibt_die_rechnung_allein():
    ergebnis = await versand.dokumente_bauen(
        FakeBexio(), entwuerfe=[ENTWURF],
        buchungen=[buchung(12, 2.5)],
        bestand=FakeBestand(vertrag(leistungsrapport=False)),
        jahr=2026, monat=8,
    )

    beilage = ergebnis.beilagen[0]
    assert beilage.mit_rapport is False
    assert beilage.seiten == 2
    assert beilage.dokument == RECHNUNG_PDF


@pytest.mark.asyncio
async def test_fehlender_vertrag_erzeugt_kein_dokument_auf_verdacht():
    ergebnis = await versand.dokumente_bauen(
        FakeBexio(),
        entwuerfe=[{**ENTWURF, "titel": "Unbekanntes Mandat"}],
        buchungen=[], bestand=FakeBestand(vertrag()),
        jahr=2026, monat=8,
    )

    assert ergebnis.beilagen == []
    assert ergebnis.uebergangen[0]["grund"].startswith("kein Vertrag")


@pytest.mark.asyncio
async def test_ein_kaputtes_pdf_reisst_die_anderen_nicht():
    ergebnis = await versand.dokumente_bauen(
        FakeBexio(scheitert={77}),
        entwuerfe=[
            ENTWURF,
            {"rechnung_id": 78, "nummer": "RE-00704", "titel": "OnboardingQS"},
        ],
        buchungen=[], bestand=FakeBestand(vertrag(leistungsrapport=False)),
        jahr=2026, monat=8,
    )

    assert [b.nummer for b in ergebnis.beilagen] == ["RE-00704"]
    assert any("RE-00703" in f for f in ergebnis.fehler)


@pytest.mark.asyncio
async def test_nur_beschraenkt_auf_genannte_kennungen():
    ergebnis = await versand.dokumente_bauen(
        FakeBexio(),
        entwuerfe=[
            ENTWURF,
            {"rechnung_id": 78, "nummer": "RE-00704", "titel": "OnboardingQS"},
        ],
        buchungen=[], bestand=FakeBestand(vertrag(leistungsrapport=False)),
        jahr=2026, monat=8, nur={78},
    )

    assert [b.nummer for b in ergebnis.beilagen] == ["RE-00704"]


@pytest.mark.asyncio
async def test_rapportpflicht_ohne_stunden_ist_ein_hindernis_kein_abbruch():
    ergebnis = await versand.dokumente_bauen(
        FakeBexio(), entwuerfe=[ENTWURF],
        buchungen=[], bestand=FakeBestand(vertrag()),
        jahr=2026, monat=8,
    )

    beilage = ergebnis.beilagen[0]
    assert beilage.mit_rapport is False
    assert any("keine verrechenbaren Stunden" in h for h in beilage.hindernisse)


# ── Mailentwürfe ───────────────────────────────────────────────────────────


class FakeGraph:
    def __init__(self, scheitert=False):
        self.entwuerfe: list[dict] = []
        self.anhaenge: list[tuple] = []
        self._scheitert = scheitert
        self._zaehler = 0

    async def create_draft(self, **kwargs):
        if self._scheitert:
            raise RuntimeError("Graph nicht erreichbar")
        self._zaehler += 1
        self.entwuerfe.append(kwargs)
        return {"id": f"AAMk-{self._zaehler}"}

    async def add_attachment(self, kennung, name, inhalt, mime="application/pdf"):
        self.anhaenge.append((kennung, name, inhalt))
        return "anhang"

    async def delete_message(self, kennung):
        pass


def beilage(nummer="RE-00703", kennung=77, **rest) -> versand.Beilage:
    grund = dict(
        rechnung_id=kennung, nummer=nummer,
        vertrag_schluessel="onboardingqs", bezeichnung="OnboardingQS",
        dokument=b"%PDF-rechnung", seiten=3, mit_rapport=True,
    )
    return versand.Beilage(**{**grund, **rest})


@pytest.mark.asyncio
async def test_mail_traegt_empfaenger_und_dokument():
    graph = FakeGraph()
    ergebnis = await versand.mails_anlegen(
        graph, [beilage()], FakeBestand(vertrag()), jahr=2026, monat=8,
    )

    assert [m.mailentwurf_id for m in ergebnis.angelegt] == ["AAMk-1"]
    assert graph.entwuerfe[0]["to_recipients"] == ["martin.roethlisberger@t-r.ch"]
    assert graph.anhaenge[0][2] == b"%PDF-rechnung"


@pytest.mark.asyncio
async def test_eine_gescheiterte_mail_laesst_die_andere_weiterlaufen():
    class Halb:
        def __init__(self):
            self.innen = FakeGraph()
            self.auferufe = 0

        async def create_draft(self, **kwargs):
            self.auferufe += 1
            if self.auferufe == 1:
                raise RuntimeError("erste scheitert")
            return await self.innen.create_draft(**kwargs)

        async def add_attachment(self, *a, **k):
            return await self.innen.add_attachment(*a, **k)

        async def delete_message(self, kennung):
            pass

    graph = Halb()
    ergebnis = await versand.mails_anlegen(
        graph,
        [beilage(nummer="RE-00703", kennung=77),
         beilage(nummer="RE-00704", kennung=78)],
        FakeBestand(vertrag()), jahr=2026, monat=8,
    )

    assert [m.nummer for m in ergebnis.fehler] == ["RE-00703"]
    assert [m.nummer for m in ergebnis.angelegt] == ["RE-00704"]


@pytest.mark.asyncio
async def test_es_wird_nie_versendet():
    graph = FakeGraph()
    gesendet: list = []
    graph.send_draft = lambda k: gesendet.append(k)  # type: ignore[attr-defined]

    await versand.mails_anlegen(
        graph, [beilage()], FakeBestand(vertrag()), jahr=2026, monat=8,
    )

    assert gesendet == []

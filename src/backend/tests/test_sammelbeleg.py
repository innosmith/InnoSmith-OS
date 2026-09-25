"""Der Monatssammelbeleg: wer hineingehört, was gebucht wird, und wann nicht.

Die Beträge im Rundungstest sind die des Augusts 2026 aus dem Modul: 45
Rechnungen, 4'493.60 USD, BAZG-Monatsmittel 0.8175. Gerundet wird wie Bexio --
einmal auf das Total, nicht je Zeile.
"""

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from io import BytesIO
from types import SimpleNamespace

import pytest
from app.services import kreditorenbuchung as kb
from app.services import kreditorenlieferanten as decl
from app.services import kreditorennorm as norm
from app.services import sammelbeleg as sb
from fpdf import FPDF
from pypdf import PdfReader

AUGUST = date(2026, 8, 1)
NACH_DEM_MONAT = date(2026, 9, 10)


def _cursor(**felder) -> decl.Lieferant:
    return replace(
        decl.Lieferant(
            schluessel="cursor",
            ordner="Cursor",
            sollkonto="6570",
            zahlweg=("karte",),
            steuer=(decl.Steuerzeile(None, "bezugssteuer"),),
            aktiv=True,
            bestaetigt=True,
            leistung="Usage",
            buchung="sammelbeleg",
            einzeln_bei_zyklus=("YEARLY", "MONTHLY"),
        ),
        **felder,
    )


def _stand(journal=()) -> kb.Bexiostand:
    return kb.Bexiostand(
        konto_id={"6570": 1, "2120": 2, "2203": 3},
        steuer_id={"BZB81": 9},
        waehrung_id={"CHF": 1, "USD": 5},
        journal=list(journal),
    )


def _beleg(**felder):
    return SimpleNamespace(**{
        "id": uuid.uuid4(), "modul_dokument_id": 1, "dateiname": "Invoice.pdf",
        "rechnungsnummer": None, "belegart": "rechnung", "zurueckgestellt": False,
        "freigegeben_am": None, "sammelbeleg_id": None, **felder,
    })


def _eintrag(nummer: str, tag: str, betrag, waehrung="USD", **zeile):
    b = _beleg(rechnungsnummer=nummer, dateiname=f"Invoice-{nummer}.pdf")
    return b, {"datum": tag, "betrag": betrag, "waehrung": waehrung, "rechnungsnummer": nummer, **zeile}


def _pruefen(eintraege, *, journal=(), heute=NACH_DEM_MONAT, kurs=Decimal("0.8175"), **kw):
    return sb.pruefen(_cursor(), AUGUST, eintraege, _stand(journal), kurs=kurs, heute=heute, **kw)


class TestWerHineingehoert:
    def test_die_abos_und_gutschriften_bleiben_draussen(self):
        """0139, eine Nachbelastung als ONE_TIME gelesen, stand im alten Sammelbeleg."""
        c = _cursor()
        assert c.sammelt("USAGE_BASED")
        assert c.sammelt("ONE_TIME")
        assert c.sammelt(None)
        assert not c.sammelt("YEARLY")
        assert not c.sammelt("MONTHLY")
        assert not c.sammelt("USAGE_BASED", "GUTSCHRIFT")
        assert not replace(c, buchung="einzeln").sammelt("USAGE_BASED")

    def test_nur_offene_gelesene_rechnungen_warten(self):
        from datetime import UTC, datetime

        zeile = {"datum": "2026-08-03", "abrechnungszyklus": "USAGE_BASED"}
        c = _cursor()
        assert sb.wartet(_beleg(), zeile, c)
        assert not sb.wartet(_beleg(belegart="sammelbeleg"), zeile, c)
        assert not sb.wartet(_beleg(zurueckgestellt=True), zeile, c)
        assert not sb.wartet(_beleg(freigegeben_am=datetime.now(UTC)), zeile, c)
        assert not sb.wartet(_beleg(sammelbeleg_id=uuid.uuid4()), zeile, c)
        assert not sb.wartet(_beleg(), {**zeile, "datum": None}, c)
        assert not sb.wartet(_beleg(), zeile, None)

    def test_der_monat_ist_der_des_rechnungsdatums(self):
        eintraege = [_eintrag("A-1", "2026-08-31", 1), _eintrag("A-2", "2026-09-01", 1)]
        assert list(sb.nach_monat(eintraege)) == [date(2026, 8, 1), date(2026, 9, 1)]

    def test_monatsletzter(self):
        assert sb.monatsletzter(date(2026, 2, 1)) == date(2026, 2, 28)
        assert sb.monatsletzter(date(2026, 12, 1)) == date(2026, 12, 31)


class TestBetraege:
    def test_franken_und_bezugsteuer_je_einmal_gerundet(self):
        p = sb.Sammelpruefung(lieferant=_cursor(), monat=AUGUST, kurs=Decimal("0.8175"))
        p.positionen = [sb.Position(uuid.uuid4(), 1, "x.pdf", "N-1", AUGUST, Decimal("4493.60"), "USD")]
        assert (p.bezugsteuer, p.betrag_chf, p.bezugsteuer_chf) == (
            Decimal("363.98"), Decimal("3673.52"), Decimal("297.55")
        )


class TestPlan:
    def test_gebucht_wird_am_monatsletzten_in_usd_mit_bzb81(self):
        p = _pruefen(
            [
                _eintrag("04CDDAC1-0123", "2026-08-03", 100.00),
                _eintrag("04CDDAC1-0122", "2026-08-01", 100.03),
            ],
            bekannte={
                "04CDDAC1-0122": date(2026, 8, 1),
                "04CDDAC1-0123": date(2026, 8, 3),
                "04CDDAC1-0124": date(2026, 9, 1),
            },
        )

        assert p.buchbar, p.verstoesse
        assert p.vollstaendig, p.vollstaendig_grund
        assert p.plan == kb.Buchungsplan(
            datum=date(2026, 8, 31),
            sollkonto="6570",
            habenkonto="2120",
            betrag=Decimal("200.03"),
            waehrung="USD",
            kurs=Decimal("0.8175"),
            steuercode="BZB81",
            text="Cursor, USA, Usage 2026.08",
            referenz="04CDDAC1-0122 bis 04CDDAC1-0123",
            dateiname="Cursor Sammelbeleg August 2026 KK.pdf",
        )
        assert p.ablageziel == "Cursor/2026/Sammelbelege/Cursor Sammelbeleg August 2026 KK.pdf"
        assert p.rechnungsnummer == "Sammelbeleg 2026.08"

    def test_der_laufende_monat_wird_nicht_gebucht_und_meldet_es_einmal(self):
        p = _pruefen([_eintrag("N-1", "2026-08-03", 10)], heute=date(2026, 8, 20), kurs=None)

        assert not p.buchbar
        assert len(p.verstoesse) == 1 and "läuft noch" in p.verstoesse[0]

    def test_ohne_kurs_nach_dem_monat_kein_plan(self):
        p = _pruefen([_eintrag("N-1", "2026-08-03", 10)], kurs=None, kurs_fehler="BAZG nennt nichts.")
        assert p.verstoesse == ["BAZG nennt nichts."] and p.plan is None


class TestWasAnhaelt:
    def test_eine_rechnung_im_archiv_ist_schon_gebucht(self):
        p = _pruefen(
            [_eintrag("04CDDAC1-0047", "2026-08-01", 100)],
            im_archiv={"04CDDAC1-0047": "Cursor/2026/Cursor Usage 01.05.2026 KK.pdf"},
        )
        assert not p.buchbar and "schon im Archiv" in p.verstoesse[0]

    def test_eine_fremde_waehrung_wird_mit_dateiname_genannt(self):
        """Am 25.09.2026 lagen zwei alte CHF-Sammelbelege als Cursor-Rechnungen im Eingang."""
        p = _pruefen([
            _eintrag("N-1", "2026-08-03", 100),
            _eintrag("N-2", "2026-08-04", 100),
            _eintrag("N-3 (Sammelbeleg)", "2026-08-20", 4171.37, waehrung="CHF"),
        ])
        assert not p.buchbar
        assert p.verstoesse == [
            "«Invoice-N-3 (Sammelbeleg).pdf» lautet auf CHF, der Monat auf USD — "
            "gehört diese Datei in den Sammelbeleg?"
        ]

    def test_doppelt_ohne_betrag_und_ausserhalb_des_monats(self):
        p = _pruefen([
            _eintrag("N-1", "2026-08-03", 100),
            _eintrag("N-1", "2026-08-04", 100),
            _eintrag("N-2", "2026-08-05", None),
        ])
        assert any("Doppelt" in v for v in p.verstoesse)
        assert any("kein Betrag" in v for v in p.verstoesse)

        fremd = _pruefen([_eintrag("N-1", "2026-07-31", 100)])
        assert any("gehört nicht in den August 2026" in v for v in fremd.verstoesse)


class TestNachtrag:
    GEBUCHT = [
        {"date": "2026-08-25", "description": "Cursor, USA, Usage 2026.08", "amount": 5599.77,
         "debit_account_id": 1, "credit_account_id": 2},
        {"date": "2026-08-25", "description": "Cursor, USA, Usage 2026.08", "amount": 453.58,
         "debit_account_id": 1, "credit_account_id": 3},
    ]

    def test_ist_der_monat_gebucht_wird_der_rest_zum_nachtrag(self):
        p = _pruefen(
            [_eintrag("04CDDAC1-0203", "2026-08-28", 100.25)],
            journal=self.GEBUCHT,
            bekannte={"04CDDAC1-0202": date(2026, 8, 20), "04CDDAC1-0204": date(2026, 9, 2)},
        )

        assert p.buchbar, p.verstoesse
        assert p.nachtrag
        assert p.plan.text == "Cursor, USA, Usage 2026.08 Nachtrag"
        assert p.plan.dateiname == "Cursor Sammelbeleg August 2026 Nachtrag KK.pdf"
        assert p.plan.datum == date(2026, 8, 31)
        assert p.rechnungsnummer == "Sammelbeleg 2026.08 Nachtrag"
        assert "schon gebucht (25.08.2026" in p.hinweise[0]

    def test_ein_zweiter_nachtrag_wird_nicht_gebucht(self):
        journal = [*self.GEBUCHT, {**self.GEBUCHT[0], "description": "Cursor, USA, Usage 2026.08 Nachtrag",
                                   "amount": 99.0}]
        p = _pruefen([_eintrag("04CDDAC1-0204", "2026-08-29", 100)], journal=journal)
        assert not p.buchbar and "zweiter Nachtrag" in p.verstoesse[-1]

    def test_derselbe_betrag_im_journal_ist_diese_buchung(self):
        """Bexio hat gebucht, das Register erfuhr es nicht -- kein Nachtrag seiner selbst."""
        p = _pruefen(
            [_eintrag("N-1", "2026-08-03", 5000.00), _eintrag("N-2", "2026-08-04", 599.77)],
            journal=self.GEBUCHT,
        )
        assert not p.buchbar
        assert "dieser Beleg ist gebucht" in p.verstoesse[0]

    def test_die_bezugsteuerzeile_macht_keinen_monat_gebucht(self):
        nur_steuer = [self.GEBUCHT[1]]
        assert not _pruefen([_eintrag("N-1", "2026-08-03", 1)], journal=nur_steuer).nachtrag

    def test_ein_anderer_monat_im_journal_zaehlt_nicht(self):
        juli = [{**z, "date": "2026-07-31"} for z in self.GEBUCHT]
        assert not _pruefen([_eintrag("N-1", "2026-08-03", 1)], journal=juli).nachtrag


class TestLuecken:
    def _positionen(self, *nummern):
        return [sb.Position(uuid.uuid4(), 1, "x", n, AUGUST, Decimal(1), "USD") for n in nummern]

    def test_gezaehlt_wird_ab_der_letzten_bekannten_nummer(self):
        bekannte = {"04CDDAC1-0100", "04CDDAC1-0101", "04CDDAC1-0104"}
        assert sb.luecken(self._positionen("04CDDAC1-0103", "04CDDAC1-0106"), bekannte) == [
            "04CDDAC1-0102", "04CDDAC1-0105",
        ]

    def test_ohne_folge_keine_luecke(self):
        assert sb.luecken(self._positionen("ohne-ziffern-x"), set()) == []

    def test_eine_luecke_braucht_die_bestaetigung(self):
        """Cursor hat 0004 und 0007 nie geliefert -- eine Lücke darf nicht für immer sperren."""
        bekannte = {
            "04CDDAC1-0101": date(2026, 7, 30),
            "04CDDAC1-0103": date(2026, 8, 3),
            "04CDDAC1-0104": date(2026, 9, 1),
        }
        eintraege = [_eintrag("04CDDAC1-0103", "2026-08-03", 1)]

        p = _pruefen(eintraege, bekannte=bekannte)
        assert p.bereit and not p.buchbar and not p.vollstaendig
        assert p.luecken == ["04CDDAC1-0102"] and "04CDDAC1-0102" in p.vollstaendig_grund

        bestaetigt = _pruefen(eintraege, bekannte=bekannte, vollstaendig_bestaetigt=True)
        assert bestaetigt.buchbar and "von Hand bestätigt" in bestaetigt.hinweise[0]


class TestVollstaendigkeit:
    """Belegt die Nummernfolge, dass im Monat keine Rechnung mehr fehlt?"""

    EINTRAEGE = [_eintrag("04CDDAC1-0122", "2026-08-01", 1), _eintrag("04CDDAC1-0123", "2026-08-30", 1)]
    BISHER = {"04CDDAC1-0121": date(2026, 7, 31), "04CDDAC1-0122": date(2026, 8, 1),
              "04CDDAC1-0123": date(2026, 8, 30)}

    def test_die_naechste_nummer_aus_dem_folgemonat_belegt_es(self):
        p = _pruefen(self.EINTRAEGE, bekannte={**self.BISHER, "04CDDAC1-0124": date(2026, 9, 1)})
        assert p.vollstaendig and p.buchbar
        assert p.vollstaendig_grund == (
            "Lückenlos bis 04CDDAC1-0123; die nächste Rechnung 04CDDAC1-0124 ist vom 01.09.2026."
        )

    def test_ohne_nachfolger_bucht_erst_die_bestaetigung(self):
        p = _pruefen(self.EINTRAEGE, bekannte=self.BISHER)
        assert p.bereit and not p.vollstaendig and not p.buchbar
        assert "noch keine Rechnung aus dem Folgemonat" in p.vollstaendig_grund
        assert _pruefen(self.EINTRAEGE, bekannte=self.BISHER, vollstaendig_bestaetigt=True).buchbar

    def test_eine_einzeln_gebuchte_nummer_im_monat_ist_keine_luecke(self):
        """Das Monatsabo steht in der Folge, aber nicht im Sammelbeleg."""
        bekannte = {**self.BISHER, "04CDDAC1-0124": date(2026, 8, 31), "04CDDAC1-0125": date(2026, 9, 1)}
        p = _pruefen(self.EINTRAEGE, bekannte=bekannte)
        assert p.vollstaendig and "04CDDAC1-0125" in p.vollstaendig_grund

    def test_fehlt_eine_vor_dem_nachfolger_ist_nichts_belegt(self):
        p = _pruefen(self.EINTRAEGE, bekannte={**self.BISHER, "04CDDAC1-0125": date(2026, 9, 1)})
        assert not p.vollstaendig and p.luecken == []
        assert "fehlt 04CDDAC1-0124" in p.vollstaendig_grund

    def test_ohne_nummernfolge_nicht_belegbar(self):
        p = _pruefen([_eintrag("ohne-ziffern-x", "2026-08-03", 1)], bekannte={})
        assert not p.vollstaendig and "keine Folge" in p.vollstaendig_grund


class TestNorm:
    def test_text_und_dateiname(self):
        sept = date(2026, 9, 1)
        assert norm.sammeltext("Cursor", "Usage", sept) == "Cursor, USA, Usage 2026.09"
        assert norm.sammeldateiname("Cursor", sept, "karte", nachtrag=True) == (
            "Cursor Sammelbeleg September 2026 Nachtrag KK.pdf"
        )

    def test_der_zweite_beleg_des_tages_bekommt_eine_klammer(self):
        name = "Cursor/2026/Cursor Usage 03.08.2026 KK.pdf"
        assert norm.nummeriert(name, 1) == name
        assert norm.nummeriert(name, 3) == "Cursor/2026/Cursor Usage 03.08.2026 KK (3).pdf"


def _rechnung(text: str, seiten: int = 1) -> bytes:
    pdf = FPDF()
    for _ in range(seiten):
        pdf.add_page()
        pdf.set_font("Helvetica", size=12)
        pdf.cell(0, 10, text)
    return bytes(pdf.output())


class TestDokument:
    def test_uebersicht_und_je_rechnung_die_erste_seite(self):
        eintraege = [
            _eintrag("04CDDAC1-0122", "2026-08-01", 100.03),
            _eintrag("04CDDAC1-0124", "2026-08-03", 100.27),
        ]
        p = _pruefen(
            eintraege,
            bekannte={"04CDDAC1-0121": None, "04CDDAC1-0122": None, "04CDDAC1-0124": None},
            vollstaendig_bestaetigt=True,
        )
        dateien = {b.id: _rechnung(f"Rechnung {z['rechnungsnummer']}", seiten=2) for b, z in eintraege}

        inhalt = sb.erzeugen(p, dateien, aussteller="Anysphere, Inc.")

        seiten = PdfReader(BytesIO(inhalt)).pages
        assert len(seiten) == 3
        erste = seiten[0].extract_text()
        assert "Sammelbeleg August 2026" in erste
        assert "200.30" in erste and "0.8175" in erste
        assert "04CDDAC1-0123" in erste  # die Lücke steht auf dem Beleg
        assert "Rechnung 04CDDAC1-0122" in seiten[1].extract_text()

    def test_eine_fehlende_rechnung_bricht_ab(self):
        eintraege = [_eintrag("N-1", "2026-08-01", 1)]
        with pytest.raises(Exception):
            sb.erzeugen(_pruefen(eintraege), {}, aussteller=None)

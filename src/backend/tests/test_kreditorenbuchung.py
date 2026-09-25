"""Die Normprüfung vor jeder Buchung -- an den Fehlern, die 2026 wirklich passiert sind.

Die Journalzeilen unten sind die echten Rapid-API-Buchungen (Kennungen und
Beträge aus Bexio gelesen am 25.09.2026). Kein Netz, keine Datenbank.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest
from app.models.models import Kreditorenbeleg
from app.services import kreditorenbuchung as kb
from app.services import kreditorenlieferanten as decl

RAPID = decl.Lieferant(
    schluessel="rapidapi",
    ordner="RapidAPI",
    sollkonto="6570",
    zahlweg=("karte",),
    steuer=(decl.Steuerzeile(date(2025, 12, 22), "bezugssteuer"),),
    rhythmus="monatlich",
    aktiv=True,
    leistung="Monatsabo",
    schreibweisen=("Rapid API",),
)

KONTEN = {"6570": 240, "2120": 279, "2203": 130, "6510": 238}


def _j(kennung, tag, betrag, text, *, soll=240, haben=279, waehrung=3):
    return {
        "id": kennung, "date": f"{tag}T00:00:00+02:00", "debit_account_id": soll,
        "credit_account_id": haben, "description": text, "amount": betrag,
        "currency_id": waehrung,
    }


AUGUST = [
    _j(5692, "2026-08-22", 10, "Rapid API Monatsabo"),
    _j(5693, "2026-08-22", 0.81, "Rapid API Monatsabo", haben=130),
]


def _stand(journal=None) -> kb.Bexiostand:
    return kb.Bexiostand(
        konto_id=KONTEN, steuer_id={"BZB81": 29}, waehrung_id={"CHF": 1, "USD": 3},
        journal=AUGUST if journal is None else journal,
    )


def _beleg(**felder) -> Kreditorenbeleg:
    werte = dict(
        datei_hash="x" * 64, dateiname="81dc8af26c9b8eca2b70133f309e9c97.pdf",
        quelle="ablage_hand", lieferant_schluessel="rapidapi",
        rechnungsnummer="GIFN3LV6-0010", sollkonto="6570", sollkonto_herkunft="vorschlag",
        steuerbehandlung="bezugssteuer", zahlweg="karte",
        freigegeben_am=datetime(2026, 9, 25, tzinfo=UTC),
    )
    werte.update(felder)
    return Kreditorenbeleg(**werte)


ZEILE = {"datum": "2026-09-22T00:00:00", "betrag": 10.0, "waehrung": "USD", "mwst": 0.0}
HEUTE = date(2026, 9, 25)


def _pruefen(beleg=None, zeile=None, stand=None, kurs=Decimal("0.8184"), **kw):
    return kb.pruefen(
        beleg or _beleg(), zeile or ZEILE, kw.pop("lieferant", RAPID), stand or _stand(),
        kurs=kurs, heute=HEUTE, **kw,
    )


class TestDerNormfall:
    def test_rapid_api_september_ist_buchbar(self):
        p = _pruefen()
        assert p.buchbar, p.verstoesse
        assert p.plan == kb.Buchungsplan(
            datum=date(2026, 9, 22), sollkonto="6570", habenkonto="2120",
            betrag=Decimal("10.0"), waehrung="USD", kurs=Decimal("0.8184"),
            steuercode="BZB81", text="RapidAPI, Monatsabo 2026.09",
            referenz="GIFN3LV6-0010", dateiname="RapidAPI Monatsabo 22.09.2026 KK.pdf",
        )
        assert p.plan.betrag_chf == Decimal("8.18")
        assert p.hinweise == []

    def test_die_nutzlast_hat_das_gemessene_muster(self):
        """Wie Buchung 1388 vom 22.08.2026 -- nur mit Referenz und Normtext."""
        nutz = kb.nutzlast(_pruefen().plan, _stand())
        assert nutz == {
            "type": "manual_single_entry", "date": "2026-09-22",
            "reference_nr": "GIFN3LV6-0010",
            "entries": [{
                "debit_account_id": 240, "credit_account_id": 279,
                "description": "RapidAPI, Monatsabo 2026.09", "amount": 10.0,
                "currency_id": 3, "currency_factor": 0.8184,
                "tax_id": 29, "tax_account_id": 240,
            }],
        }

    def test_inland_mwst_traegt_keinen_steuercode(self):
        """OpenAI, Anthropic, Google: Saldosteuersatz, kein Vorsteuerabzug."""
        p = _pruefen(_beleg(steuerbehandlung="inland_mwst"))
        assert p.plan.steuercode is None
        assert "tax_id" not in kb.nutzlast(p.plan, _stand())["entries"][0]


class TestWasAnhaelt:
    def test_ohne_kurs_keine_buchung(self):
        """April und Juni 2026: USD in CHF zum Kurs 1.0."""
        p = _pruefen(kurs=None, kurs_fehler="BAZG nicht erreichbar")
        assert not p.buchbar
        assert "BAZG nicht erreichbar" in p.verstoesse

    def test_dieselbe_buchung_steht_schon_im_journal(self):
        schon = AUGUST + [_j(5710, "2026-09-22", 10, "irgendein Text")]
        p = _pruefen(stand=_stand(schon))
        assert any("Schon im Journal" in v for v in p.verstoesse)

    def test_gleicher_betrag_im_selben_monat_haelt_an(self):
        """Juni-Rechnung am 03.07. gebucht -- die Juli-Rechnung wäre die zweite."""
        juli = [_j(5545, "2026-07-03", 10, " Rapid API Monatsabo ")]
        p = _pruefen(zeile={**ZEILE, "datum": "2026-07-22"}, stand=_stand(juli))
        assert any("eine Buchung je Periode" in v for v in p.verstoesse)

    def test_eine_andere_leistung_im_monat_ist_nur_ein_hinweis(self):
        andere = AUGUST + [_j(5711, "2026-09-05", 25, "Rapid API Zusatzpaket")]
        p = _pruefen(stand=_stand(andere))
        assert p.buchbar
        assert any("eine andere Leistung?" in h for h in p.hinweise)

    def test_die_gegenzeile_auf_2203_zaehlt_nicht_als_buchung(self):
        """Sonst sähe jede Bezugssteuerbuchung doppelt aus."""
        nur_steuer = [_j(5801, "2026-09-22", 10, "Rapid API Monatsabo", haben=130)]
        p = _pruefen(stand=_stand(AUGUST + nur_steuer))
        assert p.buchbar, p.verstoesse

    def test_ein_fehlender_vormonat_ist_ein_hinweis(self):
        p = _pruefen(stand=_stand([]))
        assert p.buchbar
        assert any("08.2026" in h and "nichts gebucht" in h for h in p.hinweise)

    def test_nur_kartenzahlungen(self):
        p = _pruefen(_beleg(zahlweg="rechnung"))
        assert any("Karte" in v for v in p.verstoesse)

    def test_ein_veralteter_vorschlag_haelt_an(self):
        p = _pruefen(_beleg(sollkonto="6510"))
        assert any("passt nicht mehr zur Deklaration" in v for v in p.verstoesse)

    def test_ein_entschiedenes_anderes_konto_ist_ein_hinweis(self):
        p = _pruefen(_beleg(sollkonto="6510", sollkonto_herkunft="entscheid"))
        assert p.buchbar
        assert any("von Hand entschieden" in h for h in p.hinweise)

    def test_bezugssteuer_und_ausgewiesene_mwst_widersprechen_sich(self):
        p = _pruefen(zeile={**ZEILE, "mwst": 0.81})
        assert any("doppelt" in v for v in p.verstoesse)

    def test_unbekannte_steuer_haelt_an(self):
        p = _pruefen(_beleg(steuerbehandlung="unbekannt"))
        assert any("Steuerbehandlung" in v for v in p.verstoesse)

    @pytest.mark.parametrize(
        ("felder", "teil"),
        [
            ({"freigegeben_am": None}, "nicht freigegeben"),
            ({"gebucht_am": datetime(2026, 9, 25, tzinfo=UTC), "bexio_referenz": "manual_entry:1"}, "Schon gebucht"),
        ],
    )
    def test_der_zustand_des_belegs(self, felder, teil):
        p = _pruefen(_beleg(**felder))
        assert any(teil in v for v in p.verstoesse)

    def test_ein_datum_in_der_zukunft(self):
        p = _pruefen(zeile={**ZEILE, "datum": "2026-10-22"})
        assert any("Zukunft" in v for v in p.verstoesse)

    def test_ohne_leistung_keine_norm(self):
        ohne = decl.Lieferant(**{**RAPID.__dict__, "leistung": None})
        p = _pruefen(lieferant=ohne)
        assert any("keine Leistung" in v for v in p.verstoesse)

    def test_eine_sammelbeleg_rechnung_wird_nicht_einzeln_gebucht(self):
        """Cursor: im Oktober steht noch nichts im Journal -- die Periode fängt es nicht."""
        sammel = decl.Lieferant(**{**RAPID.__dict__, "buchung": "sammelbeleg"})
        p = _pruefen(lieferant=sammel)
        assert not p.buchbar
        assert any("Sammelbeleg" in v for v in p.verstoesse)

    def test_das_jahresabo_eines_sammlers_geht_den_einzelweg(self):
        """Das Cursor-Jahresabo über 1'920 USD hat die Treuhänderin einzeln gebucht."""
        sammel = decl.Lieferant(
            **{**RAPID.__dict__, "buchung": "sammelbeleg", "einzeln_bei_zyklus": ("YEARLY",)}
        )
        zeile = {**ZEILE, "abrechnungszyklus": "YEARLY"}
        p = _pruefen(lieferant=sammel, zeile=zeile)
        assert not any("Sammelbeleg" in v for v in p.verstoesse)

    def test_eine_gutschrift_wird_nicht_als_aufwand_gebucht(self):
        p = _pruefen(zeile={**ZEILE, "dokumenttyp": "GUTSCHRIFT"})
        assert not p.buchbar
        assert any("Gutschrift" in v for v in p.verstoesse)

    def test_alle_maengel_in_einem_durchgang(self):
        p = _pruefen(_beleg(zahlweg="rechnung", steuerbehandlung="unbekannt"), kurs=None)
        assert len(p.verstoesse) >= 3


@pytest.mark.asyncio
class TestBuchen:
    async def test_bucht_haengt_an_und_zaehlt_nach(self):
        client = AsyncMock()
        client.create_manual_entry.return_value = {"id": 1400, "entries": [{"id": 5720}]}
        client.get_journal.return_value = [
            _j(5720, "2026-09-22", 10, "RapidAPI, Monatsabo 2026.09"),
            _j(5721, "2026-09-22", 0.81, "RapidAPI, Monatsabo 2026.09", haben=130),
        ]
        beleg = _beleg()
        p = _pruefen(beleg)

        erg = await kb.buchen(beleg, p.plan, _stand(), client, b"%PDF-1.4")

        client.attach_manual_entry_file.assert_awaited_once_with(
            1400, 5720, "RapidAPI Monatsabo 22.09.2026 KK.pdf", b"%PDF-1.4"
        )
        assert erg.beleg_angehaengt and erg.journal_zeilen == 2 and erg.meldungen == []
        assert beleg.bexio_referenz == "manual_entry:1400"
        assert beleg.gebucht_am is not None

    async def test_ohne_zeilenkennung_bleibt_die_buchung_vermerkt(self):
        """Steht die Buchung in Bexio, muss das Register es erfahren -- sonst bucht der nächste Klick doppelt."""
        client = AsyncMock()
        client.create_manual_entry.return_value = {"id": 1400}
        client.get_journal.side_effect = RuntimeError("503")
        beleg = _beleg()

        erg = await kb.buchen(beleg, _pruefen(beleg).plan, _stand(), client, b"%PDF")

        assert beleg.bexio_referenz == "manual_entry:1400"
        client.attach_manual_entry_file.assert_not_awaited()
        assert len(erg.meldungen) == 2

    async def test_ein_fehlender_anhang_laesst_die_buchung_stehen(self):
        client = AsyncMock()
        client.create_manual_entry.return_value = {"id": 1400, "entries": [{"id": 5720}]}
        client.attach_manual_entry_file.side_effect = RuntimeError("413")
        client.get_journal.return_value = []
        beleg = _beleg()

        erg = await kb.buchen(beleg, _pruefen(beleg).plan, _stand(), client, b"%PDF")

        assert beleg.gebucht_am is not None
        assert any("hängt nicht an" in m for m in erg.meldungen)
        assert any("erwartet 2" in m for m in erg.meldungen)


ZIEL = "RapidAPI/2026/RapidAPI Monatsabo 22.09.2026 KK.pdf"


def _graph(*, quelle=True, ziel_belegt=False, groesse=None):
    graph = AsyncMock()

    async def nach_pfad(pfad):
        if pfad.endswith("81dc8af26c9b8eca2b70133f309e9c97.pdf"):
            return {"id": "Q1", "name": "81dc….pdf", "size": groesse} if quelle else None
        if pfad == f"Finanzen/Kreditoren/{ZIEL}":
            return {"id": "X9"} if ziel_belegt else None
        raise AssertionError(pfad)

    graph.drive_item_by_path.side_effect = nach_pfad
    graph.ensure_drive_folder.return_value = {"id": "O2026", "folder": {}}
    graph.move_drive_item.return_value = {"id": "Q1", "name": ZIEL.rsplit("/", 1)[1]}
    return graph


def test_das_ablageziel_folgt_der_konvention():
    assert kb.ablageziel(RAPID, "RapidAPI Monatsabo 22.09.2026 KK.pdf", date(2026, 9, 22)) == ZIEL


@pytest.mark.asyncio
class TestAblegen:
    async def test_verschiebt_und_benennt_um(self):
        graph = _graph()
        beleg = _beleg(graph_pfad="_OPEN/InnoSmith/81dc8af26c9b8eca2b70133f309e9c97.pdf")

        await kb.ablegen(None, beleg, ZIEL, graph)

        graph.ensure_drive_folder.assert_awaited_once_with("Finanzen/Kreditoren/RapidAPI/2026")
        graph.move_drive_item.assert_awaited_once_with("Q1", "O2026", "RapidAPI Monatsabo 22.09.2026 KK.pdf")
        assert (beleg.archiv_pfad, beleg.graph_pfad, beleg.graph_item_id) == (ZIEL, ZIEL, "Q1")
        assert beleg.abgelegt_am is not None

    async def test_eine_gleichnamige_datei_am_ziel_wird_nicht_ersetzt(self):
        graph = _graph(ziel_belegt=True)
        beleg = _beleg(graph_pfad="_OPEN/InnoSmith/81dc8af26c9b8eca2b70133f309e9c97.pdf")

        with pytest.raises(kb.AblageFehlt, match="schon"):
            await kb.ablegen(None, beleg, ZIEL, graph)
        graph.move_drive_item.assert_not_awaited()
        assert beleg.abgelegt_am is None

    async def test_eine_von_hand_verschobene_datei_wird_gemeldet(self):
        graph = _graph(quelle=False)
        beleg = _beleg(graph_pfad="_OPEN/InnoSmith/81dc8af26c9b8eca2b70133f309e9c97.pdf")

        with pytest.raises(kb.AblageFehlt, match="nicht mehr im Eingang"):
            await kb.ablegen(None, beleg, ZIEL, graph)


@pytest.mark.asyncio
class TestVorDerBuchung:
    """OneDrive wird gefragt, nicht der Spiegel -- der kann einen Takt zurückliegen."""

    PFAD = "_OPEN/InnoSmith/81dc8af26c9b8eca2b70133f309e9c97.pdf"

    async def test_dieselbe_datei_am_selben_ort_darf_gebucht_werden(self):
        await kb.vor_der_buchung_pruefen(_beleg(graph_pfad=self.PFAD), 41_223, ZIEL, _graph(groesse=41_223))

    async def test_eine_inzwischen_geloeschte_rechnung_wird_nicht_gebucht(self):
        with pytest.raises(kb.AblageFehlt, match="Nicht gebucht"):
            await kb.vor_der_buchung_pruefen(_beleg(graph_pfad=self.PFAD), 41_223, ZIEL, _graph(quelle=False))

    async def test_eine_ersetzte_datei_wird_nicht_gebucht(self):
        with pytest.raises(kb.AblageFehlt, match="ersetzt"):
            await kb.vor_der_buchung_pruefen(_beleg(graph_pfad=self.PFAD), 41_223, ZIEL, _graph(groesse=40_000))

    async def test_ein_belegtes_ziel_verhindert_die_buchung_nicht_erst_die_ablage(self):
        with pytest.raises(kb.AblageFehlt, match="nicht gebucht"):
            await kb.vor_der_buchung_pruefen(
                _beleg(graph_pfad=self.PFAD), 41_223, ZIEL, _graph(groesse=41_223, ziel_belegt=True)
            )

    async def test_ohne_registerpfad_wird_nicht_gebucht(self):
        with pytest.raises(kb.AblageFehlt, match="kein Ort"):
            await kb.vor_der_buchung_pruefen(_beleg(graph_pfad=None), 41_223, ZIEL, _graph())

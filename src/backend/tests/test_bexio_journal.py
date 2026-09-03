"""Das Journal ist die einzige vollständige Ausgabenquelle -- geprüft wird genau das.

Vier Fallen, jede am 03.09.2026 gegen den Echtbestand gemessen und hier als Test
festgehalten, damit sie nicht zurückkommen:

1. Aufwand ist eine Eigenschaft der **Sollseite**, nicht der Buchung. Wer beide
   Seiten summiert, zählt jeden Betrag doppelt.
2. Ein Konto ohne Auflösung macht die Gruppierung unvollständig -- das muss gemeldet
   werden, nicht verschluckt.
3. ``offset`` wirkt bei ``/4.0/purchase/bills`` nicht. Ob es im Journal wirkt, ist
   zu prüfen, nicht zu hoffen -- und die Blätterschleife darf daran nicht hängen
   bleiben.
4. ``ref_class`` ist leer bei manuellen Buchungen. Das ist der Normalfall (2026:
   245 von 299 Aufwandsbuchungen) und darf nicht als Fehler gelten.
"""

import os
import sys

import pytest

SRC = os.path.join(os.path.dirname(__file__), "..", "..")
sys.path.insert(0, os.path.join(SRC, "bexio"))

from journal import (  # noqa: E402
    HERKUNFT_MANUELL,
    herkunft_beschriften,
    journal_laden,
)
from stammdaten import geschaeftsjahre_laden, kontenplan_laden, kontoklasse  # noqa: E402

JAHRE = [
    {"jahr": 2026, "von": "2026-01-01", "bis": "2026-12-31",
     "status": "open", "ist_abgeschlossen": False, "abgeschlossen_am": None},
]

KONTEN = {
    227: {"konto_id": 227, "konto_nr": "6570", "konto": "Software",
          "klasse": "betriebsaufwand", "aktiv": True, "gesperrt": False},
    98: {"konto_id": 98, "konto_nr": "2120", "konto": "Kontokorrent Gesellschafter",
         "klasse": "passiven", "aktiv": True, "gesperrt": False},
    12: {"konto_id": 12, "konto_nr": "1021", "konto": "Geschäftskonto",
         "klasse": "aktiven", "aktiv": True, "gesperrt": False},
}


class UnechterClient:
    """Ein Bexio, das genau die gemessenen Eigenheiten nachstellt."""

    def __init__(self, eintraege, *, offset_wirkt=True, seitengroesse=None):
        self.eintraege = eintraege
        self.offset_wirkt = offset_wirkt
        self.seitengroesse = seitengroesse
        self.abrufe: list[tuple[int, int]] = []

    async def list_journal(self, von, bis, limit=2000, offset=0):
        self.abrufe.append((limit, offset))
        if not self.offset_wirkt:
            offset = 0
        return self.eintraege[offset: offset + limit]

    async def get_journal(self, von, bis, limit=2000, offset=0):
        """Die echte Blätterschleife, gegen diesen unechten Server gefahren."""
        from bexio_client import BexioClient

        return await BexioClient.get_journal(self, von, bis, limit, offset)


def buchung(kennung, soll, haben, betrag, *, ref=None, datum="2026-03-01"):
    return {
        "id": kennung, "date": datum, "amount": betrag,
        "base_currency_amount": betrag, "debit_account_id": soll,
        "credit_account_id": haben, "ref_class": ref, "description": "Test",
    }


class TestAufwandIstEineEigenschaftDerSollseite:
    @pytest.mark.asyncio
    async def test_kreditkartenbuchung_gilt_als_aufwand(self):
        """Soll 6570 Software / Haben 2120 -- der häufigste Fall überhaupt."""
        client = UnechterClient([buchung(1, 227, 98, 240.0)])
        bestand = await journal_laden(client, KONTEN, JAHRE)

        (zeile,) = bestand.zeilen
        assert zeile["ist_aufwand"] is True
        assert zeile["soll_konto"] == "6570 Software"
        assert zeile["haben_konto_nr"] == "2120", "der Zahlweg muss ablesbar bleiben"

    @pytest.mark.asyncio
    async def test_geldeingang_ist_kein_aufwand(self):
        """Soll 1021 Geschäftskonto -- eine Einzahlung, kein Aufwand.

        Ohne diese Unterscheidung wäre jeder Zahlungseingang eine Ausgabe, und die
        Summe sähe trotzdem plausibel aus.
        """
        client = UnechterClient([buchung(1, 12, 227, 240.0)])
        bestand = await journal_laden(client, KONTEN, JAHRE)

        assert bestand.zeilen[0]["ist_aufwand"] is False

    @pytest.mark.asyncio
    async def test_summe_ueber_die_sollseite_zaehlt_jeden_betrag_einmal(self):
        client = UnechterClient([
            buchung(1, 227, 98, 100.0),
            buchung(2, 227, 12, 50.0),
            buchung(3, 12, 227, 30.0),
        ])
        bestand = await journal_laden(client, KONTEN, JAHRE)

        aufwand = sum(z["betrag_chf"] for z in bestand.zeilen if z["ist_aufwand"])
        assert aufwand == 150.0


class TestBlaettern:
    @pytest.mark.asyncio
    async def test_die_probe_erkennt_ein_wirkungsloses_offset(self):
        """Die Falle, die ``/4.0/purchase/bills`` tatsächlich stellt."""
        client = UnechterClient([buchung(i, 227, 98, 1.0) for i in range(20)],
                                offset_wirkt=False)
        bestand = await journal_laden(client, KONTEN, JAHRE)

        assert bestand.blaettern_geprueft is True
        assert bestand.blaettern_wirkungslos is True

    @pytest.mark.asyncio
    async def test_die_probe_bestaetigt_ein_wirksames_offset(self):
        client = UnechterClient([buchung(i, 227, 98, 1.0) for i in range(20)])
        bestand = await journal_laden(client, KONTEN, JAHRE)

        assert bestand.blaettern_geprueft is True
        assert bestand.blaettern_wirkungslos is False

    @pytest.mark.asyncio
    async def test_zu_wenige_buchungen_ergeben_keine_aussage(self):
        """Eine unbeantwortete Frage als beantwortet auszugeben wäre schlimmer, als
        offen zu lassen, dass sie offen ist."""
        client = UnechterClient([buchung(1, 227, 98, 1.0)])
        bestand = await journal_laden(client, KONTEN, JAHRE)

        assert bestand.blaettern_geprueft is False
        assert bestand.blaettern_wirkungslos is False

    @pytest.mark.asyncio
    async def test_die_schleife_haengt_nicht_an_einem_ignorierten_offset(self):
        """Ohne den Kennungs-Wächter liefe ``get_journal`` endlos.

        Ein Stillstand im Abgleich-Worker wäre die teuerste Art, diese Falle zu
        erben: er fällt erst auf, wenn nichts mehr geht.
        """
        client = UnechterClient([buchung(i, 227, 98, 1.0) for i in range(10)],
                                offset_wirkt=False)
        eintraege = await client.get_journal("2026-01-01", "2026-12-31", limit=5)

        assert len(eintraege) == 5, "wiederholte Seiten dürfen nicht angehäuft werden"

    @pytest.mark.asyncio
    async def test_ein_jahrgang_am_limit_wird_gemeldet(self):
        """Ein abgeschnittener Jahrgang sieht aus wie ein sparsames Jahr."""

        class AmLimit(UnechterClient):
            async def get_journal(self, von, bis, limit=2000, offset=0):
                return [buchung(i, 227, 98, 1.0) for i in range(limit)]

        bestand = await journal_laden(AmLimit([]), KONTEN, JAHRE)
        assert bestand.jahre_am_limit == [2026]


class TestHerkunft:
    def test_leere_ref_class_ist_manuell_und_kein_fehler(self):
        assert herkunft_beschriften(None) == HERKUNFT_MANUELL
        assert herkunft_beschriften("") == HERKUNFT_MANUELL

    def test_bekannte_klassen_werden_uebersetzt(self):
        assert herkunft_beschriften("KbBill") == "lieferantenrechnung"
        assert herkunft_beschriften("KbInvoice") == "kundenrechnung"
        assert herkunft_beschriften("KbClientAccountEntry") == "zahlung"

    def test_unbekannte_klasse_bleibt_erkennbar(self):
        """Nicht stillschweigend einsortieren -- sonst wächst eine Kategorie, die
        niemand erklären kann."""
        assert herkunft_beschriften("KbNeuesDing") == "unbekannt_kbneuesding"

    @pytest.mark.asyncio
    async def test_unbekannte_herkunft_wird_gezaehlt(self):
        client = UnechterClient([buchung(1, 227, 98, 1.0, ref="KbNeuesDing")])
        bestand = await journal_laden(client, KONTEN, JAHRE)

        assert bestand.unbekannte_herkunft == {"unbekannt_kbneuesding": 1}


class TestUnaufloesbareKonten:
    @pytest.mark.asyncio
    async def test_fehlendes_konto_wird_gemeldet_statt_verschluckt(self):
        client = UnechterClient([buchung(1, 999, 98, 42.0)])
        bestand = await journal_laden(client, KONTEN, JAHRE)

        assert bestand.ohne_konto == 1
        assert bestand.zeilen[0]["soll_konto"] == ""
        assert bestand.zeilen[0]["ist_aufwand"] is False, (
            "ohne bekannte Klasse darf nicht auf Aufwand geraten werden"
        )


class TestStammdaten:
    def test_kontoklasse_folgt_der_ersten_ziffer(self):
        assert kontoklasse("6570") == "betriebsaufwand"
        assert kontoklasse("1021") == "aktiven"
        assert kontoklasse("3400") == "ertrag"

    def test_unbekannte_nummer_wird_nicht_geraten(self):
        assert kontoklasse("") == "unbekannt"
        assert kontoklasse("abc") == "unbekannt"

    @pytest.mark.asyncio
    async def test_kontenplan_liefert_tabelle_und_nachschlagewerk_aus_einem_abruf(self):
        class Konten:
            def __init__(self):
                self.abrufe = 0

            async def list_accounts(self, limit=500):
                self.abrufe += 1
                return [{"id": 227, "account_no": "6570", "name": "Software",
                         "is_active": True, "is_locked": False}]

        client = Konten()
        zeilen, verzeichnis = await kontenplan_laden(client)

        assert client.abrufe == 1
        assert zeilen[0]["klasse"] == "betriebsaufwand"
        assert verzeichnis[227]["konto"] == "Software"

    @pytest.mark.asyncio
    async def test_offenes_geschaeftsjahr_ist_als_teiljahr_erkennbar(self):
        """Der Vergleich eines laufenden mit einem abgeschlossenen Jahr zeigt einen
        Einbruch, wo bloss Monate fehlen."""

        class Jahre:
            async def get_business_years(self):
                return [
                    {"start": "2025-01-01", "end": "2025-12-31", "status": "closed",
                     "closed_at": "2026-04-30"},
                    {"start": "2026-01-01", "end": "2026-12-31", "status": "open"},
                ]

        zeilen = await geschaeftsjahre_laden(Jahre())

        assert [z["jahr"] for z in zeilen] == [2025, 2026]
        assert zeilen[0]["ist_abgeschlossen"] is True
        assert zeilen[1]["ist_abgeschlossen"] is False

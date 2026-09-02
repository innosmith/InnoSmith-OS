"""Tests für den Datenraum und die Rechnungs-Normalisierung.

Absicherung von vier Fehlern, die alle **still** waren -- sie erzeugten eine
plausible Zahl statt einer Fehlermeldung. Gemessen am 02.09.2026 gegen den
Echtbestand: 652 Rechnungen, 50 Kunden.

1. ``GET /kb_invoice?contact_id=X`` filtert bei Bexio nicht. Der Parameter wird
   entgegengenommen und ignoriert -- 652 ungefiltert, 652 "gefiltert". Wer sich
   darauf verliess, wies den Umsatz aller Kunden einem einzigen zu.
2. Ohne Paginierungsschleife lieferte die Liste 50 von 652 Datensätzen.
3. ``search_invoices`` fiel ohne Kriterien auf ``list_invoices(limit=200)`` zurück
   und schnitt zwei Drittel ab.
4. ``kb_item_status_id`` blieb undekodiert, womit Entwürfe als Umsatz zählten.
"""

import json
import os
import sys
import types
from datetime import date

import httpx
import pytest
import respx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bexio"))

from bexio_client import BASE_URL_V2, BexioClient, BexioConfig  # noqa: E402
from rechnungen import (  # noqa: E402
    RECHNUNGSSTATUS,
    betrag,
    betraege,
    kontakte_laden,
    rechnungen_laden,
    status_beschriften,
)


@pytest.fixture
def client():
    return BexioClient(BexioConfig(api_token="test-token"))


def _rechnung(rid: int, contact_id: int = 1, status: int = 9, netto: str = "100.00") -> dict:
    """Eine Bexio-Rohrechnung, wie der Endpunkt sie liefert.

    ``total_gross`` steht bewusst gleich ``total_net``: so kommt es aus Bexio, wenn
    kein Rabatt im Spiel ist -- und genau diese Gleichheit hat den Bruttobetrag
    jahrelang harmlos aussehen lassen.
    """
    steuer = round(float(netto) * 0.081, 2)
    return {
        "id": rid,
        "document_nr": f"RE-{rid}",
        "title": "Beratung",
        "contact_id": contact_id,
        "currency_id": 1,
        "is_valid_from": "2026-01-15",
        "is_valid_to": "2026-02-15",
        "kb_item_status_id": status,
        "total_net": netto,
        "total_gross": netto,
        "total_taxes": f"{steuer:.2f}",
        "total": f"{float(netto) + steuer:.2f}",
        "total_received_payments": "0.00",
        "total_remaining_payments": f"{float(netto) + steuer:.2f}",
        "updated_at": "2026-01-15 10:00:00",
    }


class TestStatusdekodierung:
    """Die Statustabelle ist deklariert, nicht erschlossen."""

    def test_bestaetigte_zuordnung(self):
        assert RECHNUNGSSTATUS[7] == ("entwurf", False)
        assert RECHNUNGSSTATUS[8] == ("offen", True)
        assert RECHNUNGSSTATUS[9] == ("bezahlt", True)

    def test_entwurf_ist_kein_umsatz(self):
        """Eine nie gestellte Rechnung darf keinen Umsatz erzeugen."""
        _, ist_umsatz = status_beschriften(7)
        assert ist_umsatz is False

    def test_offene_rechnung_ist_umsatz(self):
        """Fakturiert ist fakturiert -- Geldeingang beantwortet die Spalte 'bezahlt'."""
        assert status_beschriften(8) == ("offen", True)

    def test_unbekannter_status_zaehlt_nicht_und_bleibt_erkennbar(self):
        """Kein stiller Umsatz aus einer Kennung, die niemand deklariert hat."""
        beschriftung, ist_umsatz = status_beschriften(19)
        assert ist_umsatz is False
        assert beschriftung == "unbekannt_19"

    def test_kein_status_ist_kein_absturz(self):
        assert status_beschriften(None) == ("unbekannt", False)


class TestBetrag:
    """Bexio liefert Beträge als Zeichenkette -- eine Summe darüber verkettet."""

    def test_zeichenkette_wird_zahl(self):
        assert betrag("1234.50") == 1234.50

    def test_leer_ist_null(self):
        assert betrag("") == 0.0
        assert betrag(None) == 0.0

    def test_unlesbar_ist_null_statt_absturz(self):
        assert betrag("k.A.") == 0.0


class TestBetraege:
    """Der Bruttobetrag kommt nicht aus dem Feld, das brutto heisst."""

    def test_brutto_enthaelt_die_steuer(self):
        netto, mwst, brutto = betraege(_rechnung(1, netto="5625.00"))
        assert netto == 5625.00
        assert brutto == pytest.approx(netto + mwst)
        assert brutto == pytest.approx(6080.63, abs=0.02)

    def test_total_gross_wird_nicht_als_brutto_genommen(self):
        """Die Regressionsprobe: ``total_gross`` ist die Positionssumme vor Rabatt.

        Wird sie als Brutto ausgewiesen, meldet das System einen Bruttobetrag ohne
        Steuer -- und weil er zum Nettobetrag passt, fällt es niemandem auf.
        """
        roh = _rechnung(1, netto="5625.00") | {"total_gross": "5625.00"}
        _, _, brutto = betraege(roh)
        assert brutto != 5625.00

    def test_fehlendes_total_wird_aus_netto_und_steuer_gebildet(self):
        roh = _rechnung(1, netto="100.00")
        del roh["total"]
        assert betraege(roh) == (100.00, 8.10, 108.10)

    def test_rechnung_ohne_steuer_wird_gezaehlt(self):
        """«Keine Mehrwertsteuer» muss eine Aussage über die Daten sein."""
        roh = _rechnung(1, netto="100.00") | {"total_taxes": "0.00", "total": "100.00"}
        assert betraege(roh) == (100.00, 0.0, 100.00)


class TestPaginierung:
    """Ohne Schleife endet jede Auswertung bei der ersten Seite."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_alle_seiten_werden_geholt(self, client):
        seiten = [
            [_rechnung(i) for i in range(500)],
            [_rechnung(500 + i) for i in range(152)],
        ]
        aufrufe = []

        def antwort(request):
            aufrufe.append(dict(request.url.params))
            return httpx.Response(200, json=seiten[len(aufrufe) - 1])

        respx.get(f"{BASE_URL_V2}/kb_invoice").mock(side_effect=antwort)

        alle = await client.alle_rechnungen()

        assert len(alle) == 652
        assert [a["offset"] for a in aufrufe] == ["0", "500"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_letzte_seite_beendet_die_schleife(self, client):
        respx.get(f"{BASE_URL_V2}/kb_invoice").mock(
            return_value=httpx.Response(200, json=[_rechnung(1)])
        )
        assert len(await client.alle_rechnungen()) == 1


class TestKundenfilter:
    """Der GET-Filter wirkt nicht -- eingegrenzt wird über die Suche."""

    @pytest.mark.asyncio
    @respx.mock
    async def test_contact_id_geht_nie_ueber_get(self, client):
        get_route = respx.get(f"{BASE_URL_V2}/kb_invoice").mock(
            return_value=httpx.Response(200, json=[_rechnung(i) for i in range(652)])
        )
        such_route = respx.post(f"{BASE_URL_V2}/kb_invoice/search").mock(
            return_value=httpx.Response(200, json=[_rechnung(1, contact_id=42)])
        )

        treffer = await client.list_invoices(contact_id=42)

        assert not get_route.called, "GET würde alle 652 Rechnungen liefern"
        assert such_route.called
        assert len(treffer) == 1
        kriterien = json.loads(such_route.calls[0].request.content)
        assert {"field": "contact_id", "value": "42", "criteria": "="} in kriterien

    @pytest.mark.asyncio
    @respx.mock
    async def test_auftraege_ebenso(self, client):
        get_route = respx.get(f"{BASE_URL_V2}/kb_order").mock(
            return_value=httpx.Response(200, json=[{"id": 1}, {"id": 2}])
        )
        such_route = respx.post(f"{BASE_URL_V2}/kb_order/search").mock(
            return_value=httpx.Response(200, json=[{"id": 1}])
        )

        await client.list_orders(contact_id=42)

        assert not get_route.called
        assert such_route.called


class TestSuchkriterien:
    @pytest.mark.asyncio
    @respx.mock
    async def test_status_wird_zur_kennung(self, client):
        """'offen' muss als 8 ankommen -- der Name trifft das numerische Feld nie."""
        route = respx.post(f"{BASE_URL_V2}/kb_invoice/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        await client.search_invoices(status="offen")
        kriterien = json.loads(route.calls[0].request.content)
        assert {"field": "kb_item_status_id", "value": "8", "criteria": "="} in kriterien

    @pytest.mark.asyncio
    @respx.mock
    async def test_kennung_bleibt_kennung(self, client):
        route = respx.post(f"{BASE_URL_V2}/kb_invoice/search").mock(
            return_value=httpx.Response(200, json=[])
        )
        await client.search_invoices(status=9)
        kriterien = json.loads(route.calls[0].request.content)
        assert kriterien[0]["value"] == "9"

    @pytest.mark.asyncio
    async def test_unbekannter_status_wird_abgelehnt(self, client):
        with pytest.raises(ValueError, match="Unbekannter Rechnungsstatus"):
            await client.search_invoices(status="ueberfaellig")

    @pytest.mark.asyncio
    @respx.mock
    async def test_ohne_kriterien_kommt_der_ganze_bestand(self, client):
        """Früher: stiller Rückfall auf 200 Datensätze von 652."""
        seiten = [[_rechnung(i) for i in range(500)], [_rechnung(500 + i) for i in range(152)]]
        aufrufe = []

        def antwort(request):
            aufrufe.append(1)
            return httpx.Response(200, json=seiten[len(aufrufe) - 1])

        respx.get(f"{BASE_URL_V2}/kb_invoice").mock(side_effect=antwort)
        assert len(await client.search_invoices()) == 652


class TestKontaktsuche:
    @pytest.mark.asyncio
    @respx.mock
    async def test_sucht_in_beiden_namensfeldern(self, client):
        """'GSW' muss 'GSW Treuhand AG' finden, egal in welchem Feld es steht."""
        route = respx.post(f"{BASE_URL_V2}/contact/search").mock(
            return_value=httpx.Response(200, json=[{"id": 7, "name_1": "GSW Treuhand AG"}])
        )
        treffer = await client.search_contact_by_name("GSW")

        felder = [json.loads(c.request.content)[0]["field"] for c in route.calls]
        assert felder == ["name_1", "name_2"]
        assert len(treffer) == 1, "derselbe Kontakt darf nicht doppelt erscheinen"

    @pytest.mark.asyncio
    async def test_leerer_name_wird_abgelehnt(self, client):
        with pytest.raises(ValueError):
            await client.search_contact_by_name("   ")


class TestNormalisierung:
    @pytest.mark.asyncio
    @respx.mock
    async def test_zeilen_tragen_kundennamen_und_umsatzkennzeichen(self, client):
        respx.get(f"{BASE_URL_V2}/kb_invoice").mock(
            return_value=httpx.Response(200, json=[
                _rechnung(1, contact_id=7, status=9, netto="1000.00"),
                _rechnung(2, contact_id=7, status=7, netto="500.00"),
            ])
        )
        respx.get(f"{BASE_URL_V2}/contact").mock(
            return_value=httpx.Response(200, json=[
                {"id": 7, "name_1": "GSW Treuhand AG", "name_2": ""},
            ])
        )
        respx.get(f"{BASE_URL_V2}/currency").mock(
            return_value=httpx.Response(200, json=[{"id": 1, "name": "CHF"}])
        )

        bestand = await rechnungen_laden(client)

        assert [z["kunde"] for z in bestand.zeilen] == ["GSW Treuhand AG"] * 2
        assert [z["ist_umsatz"] for z in bestand.zeilen] == [True, False]
        umsatz = sum(z["netto"] for z in bestand.zeilen if z["ist_umsatz"])
        assert umsatz == 1000.00, "der Entwurf über 500 darf nicht mitzählen"
        assert bestand.waehrungen == ["CHF"]

    @pytest.mark.asyncio
    @respx.mock
    async def test_fehlende_waehrungen_brechen_nichts(self, client):
        """Ein Ausfall darf sichtbar sein, aber nicht als CHF durchgehen."""
        respx.get(f"{BASE_URL_V2}/kb_invoice").mock(
            return_value=httpx.Response(200, json=[_rechnung(1)])
        )
        respx.get(f"{BASE_URL_V2}/contact").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE_URL_V2}/currency").mock(return_value=httpx.Response(500))

        bestand = await rechnungen_laden(client)
        assert bestand.zeilen[0]["waehrung"] == "id:1"

    @pytest.mark.asyncio
    @respx.mock
    async def test_unbekannter_status_wird_gezaehlt_und_gemeldet(self, client):
        respx.get(f"{BASE_URL_V2}/kb_invoice").mock(
            return_value=httpx.Response(200, json=[_rechnung(1, status=19)])
        )
        respx.get(f"{BASE_URL_V2}/contact").mock(return_value=httpx.Response(200, json=[]))
        respx.get(f"{BASE_URL_V2}/currency").mock(return_value=httpx.Response(200, json=[]))

        bestand = await rechnungen_laden(client)
        assert bestand.unbekannte_status == {"unbekannt_19": 1}
        assert bestand.zeilen[0]["ist_umsatz"] is False

    @pytest.mark.asyncio
    @respx.mock
    async def test_kontakte_werden_normalisiert(self, client):
        respx.get(f"{BASE_URL_V2}/contact").mock(
            return_value=httpx.Response(200, json=[
                {"id": 7, "name_1": "Muster", "name_2": "Anna", "contact_type_id": 2,
                 "mail": "a@b.ch", "city": "Bern", "postcode": "3000"},
            ])
        )
        zeilen = await kontakte_laden(client)
        assert zeilen[0]["name"] == "Muster Anna"
        assert zeilen[0]["typ"] == "person"


class TestTogglZeitraum:
    """Toggl begrenzt das Fenster auf ein Jahr und blättert seitenweise.

    Beides fiel beim Erstabgleich am 02.09.2026 auf: ein Fenster von 730 Tagen
    beantwortete Toggl mit ``400 Bad Request``. Die fehlende Seitenschleife wäre
    dagegen nie aufgefallen -- sie hätte einfach zu wenig Stunden gemeldet.
    """

    @pytest.fixture
    def toggl(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "toggl"))
        from toggl_client import TogglClient, TogglConfig

        return TogglClient(TogglConfig(api_token="test", workspace_id=1))

    @pytest.mark.asyncio
    @respx.mock
    async def test_zeitraum_wird_in_jahresscheiben_zerlegt(self, toggl):
        from toggl_client import REPORTS_URL

        fenster = []

        def antwort(request):
            rumpf = json.loads(request.content)
            fenster.append((rumpf["start_date"], rumpf["end_date"]))
            return httpx.Response(200, json=[{"id": len(fenster)}])

        respx.post(f"{REPORTS_URL}/workspace/1/search/time_entries").mock(side_effect=antwort)

        await toggl.search_all_time_entries(1, "2024-09-02", "2026-09-02")

        assert len(fenster) == 3, "730 Tage müssen in Scheiben von höchstens einem Jahr"
        for start, ende in fenster:
            spanne = (date.fromisoformat(ende) - date.fromisoformat(start)).days
            assert spanne < 365
        assert fenster[0][0] == "2024-09-02"
        assert fenster[-1][1] == "2026-09-02"

    @pytest.mark.asyncio
    @respx.mock
    async def test_alle_seiten_werden_geholt(self, toggl):
        from toggl_client import REPORTS_URL

        aufrufe = []

        def antwort(request):
            aufrufe.append(json.loads(request.content).get("first_row_number"))
            if len(aufrufe) == 1:
                return httpx.Response(200, json=[{"id": 1}], headers={"X-Next-Row-Number": "51"})
            return httpx.Response(200, json=[{"id": 2}])

        respx.post(f"{REPORTS_URL}/workspace/1/search/time_entries").mock(side_effect=antwort)

        eintraege = await toggl.search_all_time_entries(1, "2026-01-01", "2026-06-30")

        assert len(eintraege) == 2
        assert aufrufe == [None, 51]


class TestTogglAuffaltung:
    """Die Reports-API antwortet gruppiert -- Datum und Kunde stehen nicht, wo man sie sucht."""

    @pytest.fixture
    def gruppe(self):
        """Eine Antwortzeile in der Form, die Toggl am 02.09.2026 tatsächlich lieferte."""
        return {
            "project_id": 7,
            "username": "Anthony Smith",
            "description": "Workshop",
            "billable": True,
            "currency": "CHF",
            "hourly_rate_in_cents": 25000,
            "billable_amount_in_cents": 75000,
            "time_entries": [
                {"id": 101, "start": "2026-08-31T09:00:00+02:00", "seconds": 3600},
                {"id": 102, "start": "2026-09-01T09:00:00+02:00", "seconds": 7200},
            ],
        }

    @pytest.mark.asyncio
    async def test_jede_buchung_wird_eine_zeile_mit_datum_und_kunde(self, gruppe, monkeypatch):
        """Der Regressionstest zum Fehler, der alle 2639 Einträge unbrauchbar machte.

        ``start`` und ``client_id`` gibt es auf der obersten Ebene nicht: das Datum
        steht im Untereintrag, der Kunde hängt am Projekt.
        """
        from app.services import datenraum

        class FakeToggl:
            def __init__(self, *a, **k):
                pass

            async def list_projects(self, active=None):
                return [{"id": 7, "name": "Beratung", "client_id": 3, "active": True, "billable": True}]

            async def list_clients(self, status=None):
                # Ohne "both" fehlen archivierte Kunden -- und mit ihnen der Name
                # jedes Projekts, das ihnen gehoert.
                assert status == "both"
                return [{"id": 3, "name": "GSW Treuhand AG"}]

            async def search_all_time_entries(self, ws, von, bis):
                return [gruppe]

        modul = types.ModuleType("toggl_client")
        modul.TogglClient = FakeToggl
        modul.TogglConfig = lambda **k: None
        monkeypatch.setitem(sys.modules, "toggl_client", modul)

        tabellen, hinweise = await datenraum._lade_toggl(
            {"toggl_api_token": "t", "toggl_workspace_id": 1}
        )
        zeilen = tabellen["toggl_zeiteintraege"]

        assert len(zeilen) == 2, "eine Zeile je Buchung, nicht je Gruppe"
        assert [z["datum"] for z in zeilen] == ["2026-08-31", "2026-09-01"]
        assert all(z["kunde"] == "GSW Treuhand AG" for z in zeilen)
        assert [z["stunden"] for z in zeilen] == [1.0, 2.0]
        assert hinweise["eintraege_ohne_kundennamen"] == 0

    @pytest.mark.asyncio
    async def test_betrag_folgt_der_zeit(self, gruppe, monkeypatch):
        """Der Gruppenbetrag wird nach Sekunden aufgeteilt -- die Umkehrung seiner Entstehung."""
        from app.services import datenraum

        class FakeToggl:
            def __init__(self, *a, **k):
                pass

            async def list_projects(self, active=None):
                return [{"id": 7, "name": "Beratung", "client_id": 3}]

            async def list_clients(self, status=None):
                return [{"id": 3, "name": "GSW Treuhand AG"}]

            async def search_all_time_entries(self, ws, von, bis):
                return [gruppe]

        modul = types.ModuleType("toggl_client")
        modul.TogglClient = FakeToggl
        modul.TogglConfig = lambda **k: None
        monkeypatch.setitem(sys.modules, "toggl_client", modul)

        zeilen = (await datenraum._lade_toggl({"toggl_api_token": "t", "toggl_workspace_id": 1}))[0]["toggl_zeiteintraege"]

        assert [z["betrag"] for z in zeilen] == [250.0, 500.0]
        assert sum(z["betrag"] for z in zeilen) == 750.0
        assert all(z["stundensatz"] == 250.0 for z in zeilen)


class TestDatenraumAblage:
    """Schreiben, Katalog, atomarer Tausch."""

    @pytest.fixture(autouse=True)
    def _eigenes_verzeichnis(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TP_DATENRAUM_DIR", str(tmp_path / "datenraum"))
        from app.config import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    def test_tabelle_wird_geschrieben_und_beschrieben(self):
        from app.services.datenraum import datenraum_pfad, tabelle_schreiben

        beschreibung = tabelle_schreiben("probe", [{"kunde": "GSW", "netto": 100.0}])

        assert beschreibung["zeilen"] == 1
        assert set(beschreibung["spalten"]) == {"kunde", "netto"}
        assert (datenraum_pfad() / "probe.parquet").is_file()

    def test_leere_tabelle_ersetzt_den_bestand_nicht(self):
        """Sonst löscht ein Ausfall der Quelle stillschweigend die letzten guten Daten."""
        from app.services.datenraum import datenraum_pfad, tabelle_schreiben

        tabelle_schreiben("probe", [{"kunde": "GSW"}])
        with pytest.raises(ValueError, match="leer"):
            tabelle_schreiben("probe", [])
        assert (datenraum_pfad() / "probe.parquet").is_file()

    def test_kein_zwischenstand_bleibt_liegen(self):
        from app.services.datenraum import datenraum_pfad, tabelle_schreiben

        tabelle_schreiben("probe", [{"kunde": "GSW"}])
        assert not list(datenraum_pfad().glob("*.tmp"))

    def test_durchgehend_leere_spalte_wird_gemeldet(self):
        """Der Wächter gegen die teuerste Fehlerart: eine Spalte, die es gibt und die nichts trägt.

        Genau so gingen alle 2639 Toggl-Zeiteinträge ohne Datum und ohne Kunde in den
        Datenraum -- die Tabelle war vollzählig, jede Zeitfrage trotzdem falsch.
        """
        from app.services.datenraum import tabelle_schreiben

        beschreibung = tabelle_schreiben("probe", [
            {"kunde": "", "datum": None, "stunden": 1.5},
            {"kunde": "", "datum": None, "stunden": 2.0},
        ])
        assert set(beschreibung["leere_spalten"]) == {"kunde", "datum"}

    def test_einzelne_luecke_ist_keine_meldung(self):
        """Fehlwerte sind normal -- nur der Totalausfall einer Spalte ist ein Befund."""
        from app.services.datenraum import tabelle_schreiben

        beschreibung = tabelle_schreiben("probe", [
            {"kunde": "GSW", "datum": "2026-08-31"},
            {"kunde": "", "datum": None},
        ])
        assert "leere_spalten" not in beschreibung

    def test_katalog_ist_lesbar_und_ueberlebt_muell(self):
        from app.services.datenraum import datenraum_pfad, katalog_lesen, katalog_schreiben

        katalog_schreiben({"tabellen": {"probe": {"zeilen": 1}}, "quellen": {}})
        assert katalog_lesen()["tabellen"]["probe"]["zeilen"] == 1

        (datenraum_pfad() / "_katalog.json").write_text("{kaputt", encoding="utf-8")
        assert katalog_lesen() == {"tabellen": {}, "quellen": {}}

    def test_verwaiste_tabellen_verschwinden(self):
        """Wird eine Quelle abgeschaltet, darf ihr letzter Abzug nicht liegenbleiben."""
        from app.services.datenraum import (
            datenraum_pfad,
            katalog_schreiben,
            tabelle_schreiben,
            verwaiste_tabellen_entfernen,
        )

        tabelle_schreiben("bleibt", [{"a": 1}])
        tabelle_schreiben("geht", [{"a": 1}])
        katalog_schreiben({"tabellen": {"bleibt": {"datei": "bleibt.parquet"}}, "quellen": {}})

        assert verwaiste_tabellen_entfernen() == ["geht.parquet"]
        assert (datenraum_pfad() / "bleibt.parquet").is_file()
        assert not (datenraum_pfad() / "geht.parquet").exists()


class TestQuellenIsolation:
    """Ein Teilausfall darf die anderen Quellen nicht mitreissen."""

    @pytest.fixture(autouse=True)
    def _eigenes_verzeichnis(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TP_DATENRAUM_DIR", str(tmp_path / "datenraum"))
        from app.config import get_settings

        get_settings.cache_clear()
        yield
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_fehler_landet_im_katalog_statt_zu_fliegen(self):
        from app.services import datenraum as dr

        async def kaputt(_settings):
            raise RuntimeError("Token nicht konfiguriert")

        katalog: dict = {}
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(dr.QUELLEN, "bexio", dr.Quelle(lader=kaputt, beschreibung="Test"))
            eintrag = await dr.quelle_abgleichen("bexio", {}, katalog)

        assert "Token nicht konfiguriert" in eintrag["letzter_fehler"]
        assert katalog.get("tabellen", {}) == {}

    @pytest.mark.asyncio
    async def test_erfolg_loescht_den_alten_fehler(self):
        from app.services import datenraum as dr

        async def gut(_settings):
            return {"probe_tabelle": [{"a": 1}]}, {"hinweis": "ok"}

        katalog = {"quellen": {"bexio": {"letzter_fehler": "alt"}}}
        with pytest.MonkeyPatch.context() as mp:
            mp.setitem(dr.QUELLEN, "bexio", dr.Quelle(lader=gut, beschreibung="Test"))
            eintrag = await dr.quelle_abgleichen("bexio", {}, katalog)

        assert eintrag["letzter_fehler"] is None
        assert katalog["tabellen"]["probe_tabelle"]["zeilen"] == 1
        assert eintrag["hinweise"] == {"hinweis": "ok"}


class TestZeitspalten:
    """Ein Datum gehoert als Datum ins Parquet, nicht als Zeichenkette.

    Der Anlass ist ein gescheiterter Lauf vom 02.09.2026: ``gewonnen_am`` lag als
    Text vor, und die Frage «wie viel Umsatz pro Jahr» endete in einem
    Binder-Fehler von DuckDB statt in einer Zahl. Wer Datumswerte als Text ablegt,
    verlagert eine Umwandlung, die hier einmal richtig zu loesen ist, in jede
    einzelne Abfrage -- und dort wird sie geraten.
    """

    def test_reines_datum_wird_date(self):
        import pyarrow as pa
        from app.services.datenraum import zeitspalten_setzen

        tabelle = pa.Table.from_pylist([
            {"datum": "2026-06-30", "kunde": "GSW"},
            {"datum": "2026-07-31", "kunde": "GSW"},
        ])
        umgewandelt = zeitspalten_setzen("bexio_rechnungen", tabelle)
        assert pa.types.is_date(umgewandelt.column("datum").type)
        assert umgewandelt.column("datum")[0].as_py() == date(2026, 6, 30)
        assert pa.types.is_string(umgewandelt.column("kunde").type)

    def test_zeitstempel_bleibt_zeitstempel(self):
        import pyarrow as pa
        from app.services.datenraum import zeitspalten_setzen

        tabelle = pa.Table.from_pylist([
            {"gewonnen_am": "2024-01-11T10:23:00Z"},
            {"gewonnen_am": "2025-01-08T08:00:00Z"},
        ])
        umgewandelt = zeitspalten_setzen("pipedrive_deals", tabelle)
        assert pa.types.is_timestamp(umgewandelt.column("gewonnen_am").type)

    def test_leerer_text_wird_null_statt_fehler(self):
        """Ein fehlendes Datum ist kein Datum -- und darf die Umwandlung nicht kippen."""
        import pyarrow as pa
        from app.services.datenraum import zeitspalten_setzen

        tabelle = pa.Table.from_pylist([
            {"gewonnen_am": "2024-01-11T10:23:00Z"},
            {"gewonnen_am": ""},
            {"gewonnen_am": None},
        ])
        spalte = zeitspalten_setzen("pipedrive_deals", tabelle).column("gewonnen_am")
        assert pa.types.is_timestamp(spalte.type)
        assert spalte.null_count == 2

    def test_unlesbares_bleibt_text_statt_zu_verschwinden(self):
        """Lieber eine unbequeme Spalte als eine stillschweigend geleerte."""
        import pyarrow as pa
        from app.services.datenraum import zeitspalten_setzen

        tabelle = pa.Table.from_pylist([{"datum": "irgendwann"}, {"datum": "spaeter"}])
        spalte = zeitspalten_setzen("bexio_rechnungen", tabelle).column("datum")
        assert pa.types.is_string(spalte.type)
        assert spalte[0].as_py() == "irgendwann"

    def test_fehlende_spalte_stoert_nicht(self):
        import pyarrow as pa
        from app.services.datenraum import zeitspalten_setzen

        tabelle = pa.Table.from_pylist([{"kunde": "GSW"}])
        assert zeitspalten_setzen("bexio_rechnungen", tabelle).column_names == ["kunde"]

    def test_jahresauswertung_laeuft_nach_der_umwandlung(self):
        """Die Frage, an der der Lauf scheiterte -- jetzt als Test.

        DuckDB ist keine Abhaengigkeit des Backends, sondern lebt im Sandbox-Bild.
        Fehlt es lokal, wird uebersprungen statt rot gemeldet -- gepruefte Aussage
        bleibt die Umwandlung selbst, die die uebrigen Tests ohne DuckDB halten.
        """
        import pyarrow as pa

        duckdb = pytest.importorskip("duckdb")
        from app.services.datenraum import zeitspalten_setzen

        pipedrive_deals = zeitspalten_setzen("pipedrive_deals", pa.Table.from_pylist([  # noqa: F841
            {"gewonnen_am": "2024-01-11T10:23:00Z", "wert": 150000.0},
            {"gewonnen_am": "2025-01-08T08:00:00Z", "wert": 90000.0},
            {"gewonnen_am": "2025-06-02T08:00:00Z", "wert": 10000.0},
        ]))
        ergebnis = duckdb.sql(
            "SELECT EXTRACT(YEAR FROM gewonnen_am)::INT AS jahr, SUM(wert) AS wert "
            "FROM pipedrive_deals GROUP BY jahr ORDER BY jahr"
        ).fetchall()
        assert ergebnis == [(2024, 150000.0), (2025, 100000.0)]

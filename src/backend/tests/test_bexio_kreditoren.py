"""Tests für die Bexio-Lieferantenrechnungen.

Geprüft wird nicht, ob der Code läuft, sondern die vier Eigenschaften, an denen die
Schnittstelle still falsche Antworten gibt -- alle vier am 03.09.2026 gegen den
Echtbestand gemessen:

1. ``offset`` wird ignoriert; nur ``page`` blättert.
2. ``gross`` ist kein Bruttobetrag -- es gibt gar keine Steuer.
3. Lieferant und Kontierung stehen nur im Einzelabruf.
4. Fremdwährung hat in der Liste keinen CHF-Gegenwert.

Alle vier erzeugen ohne diese Tests eine plausible Zahl statt einer Fehlermeldung.
"""

from __future__ import annotations

import os
import sys

import httpx
import pytest
import respx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bexio"))

from bexio_client import BASE_URL_V2, BASE_URL_V4, BexioClient, BexioConfig  # noqa: E402
from kreditoren import (  # noqa: E402
    KREDITORENSTATUS,
    lieferantenrechnungen_laden,
    status_beschriften,
)


@pytest.fixture
def client():
    return BexioClient(BexioConfig(api_token="test-token"))


def _kopf(kennung: str, **felder) -> dict:
    grund = {
        "id": kennung,
        "document_no": f"00{kennung}",
        "bill_date": "2026-01-15",
        "due_date": "2026-02-14",
        "vendor": "Muster AG",
        "title": "Irgendetwas",
        "net": 100.0,
        "gross": 100.0,
        "pending_amount": 0.0,
        "currency_code": "CHF",
        "status": "PAID",
        "overdue": False,
        "created_at": "2026-01-16T10:00:00+0000",
    }
    grund.update(felder)
    return grund


def _detail(**felder) -> dict:
    grund = {
        "supplier_id": 78,
        "base_currency_amount": None,
        "base_currency_code": "CHF",
        "exchange_rate": None,
        "line_items": [
            {"booking_account_id": 42, "amount": 100.0, "tax_id": None, "tax_calc": 0.0},
        ],
    }
    grund.update(felder)
    return grund


def _seiten_route(seiten: dict[int, list[dict]], gesamt: int) -> callable:
    """Antwortet auf ``page`` und ignoriert ``offset`` -- wie Bexio es tut."""

    def antworten(request: httpx.Request) -> httpx.Response:
        seite = int(request.url.params.get("page", "1"))
        return httpx.Response(200, json={
            "data": seiten.get(seite, []),
            "paging": {"page": seite, "page_size": 100, "item_count": gesamt},
        })

    return antworten


# ── Status ───────────────────────────────────────────────

def test_der_status_ist_eine_zeichenkette_keine_kennung():
    """Bei den Debitoren ist der Status eine Zahl, hier eine Zeichenkette. Wer die
    Zuordnung von dort überträgt, trifft nie."""
    assert status_beschriften("PAID") == ("bezahlt", False)
    assert status_beschriften("SENT") == ("offen", True)
    assert status_beschriften("DRAFT") == ("entwurf", False)


def test_unbekannter_status_wird_gekennzeichnet_nicht_einsortiert():
    """Ein neuer Statuswert darf weder verschwinden noch stillschweigend als
    bezahlt gelten."""
    beschriftung, ist_offen = status_beschriften("CANCELLED")

    assert beschriftung == "unbekannt_cancelled"
    assert ist_offen is False
    assert "CANCELLED" not in KREDITORENSTATUS


# ── Blättern ─────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_geblaettert_wird_ueber_page_nicht_ueber_offset(client):
    """Die teuerste Falle dieser Schnittstelle: ``offset=50`` liefert dieselben
    Zeilen wie ``offset=0``. Wer ihn benutzt, lädt fünfmal die erste Seite und
    hält 100 Zeilen für 435 -- ohne Fehlermeldung."""
    seiten = {
        1: [_kopf(str(i)) for i in range(100)],
        2: [_kopf(str(i)) for i in range(100, 150)],
    }
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(side_effect=_seiten_route(seiten, 150))

    zeilen, gemeldet = await client.alle_lieferantenrechnungen()

    assert len(zeilen) == 150
    assert gemeldet == 150
    assert len({z["id"] for z in zeilen}) == 150


@pytest.mark.asyncio
@respx.mock
async def test_unvollstaendiger_abzug_wird_erkannt_nicht_verschwiegen(client, caplog):
    """``paging.item_count`` ist die Sollzahl. Ohne diesen Abgleich sieht ein
    abgebrochener Abzug aus wie ein kleiner Bestand."""
    seiten = {1: [_kopf(str(i)) for i in range(3)]}
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(side_effect=_seiten_route(seiten, 99))
    respx.get(url__regex=rf"{BASE_URL_V4}/purchase/bills/\d+").mock(
        return_value=httpx.Response(200, json=_detail())
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(200, json=[]))

    bestand = await lieferantenrechnungen_laden(client)

    assert bestand.gemeldet == 99
    assert len(bestand.zeilen) == 3
    assert bestand.vollstaendig is False


# ── Beträge ──────────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_es_gibt_genau_einen_betrag_und_keine_steuerspalte(client):
    """``gross`` ist bei allen 435 Rechnungen des Bestands identisch mit ``net``,
    und ``tax_id`` ist durchgehend leer: dieses Konto verbucht Kreditoren ohne
    Vorsteuer. Eine Brutto-Spalte wäre eine Lüge mit plausibler Zahl."""
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(
        side_effect=_seiten_route({1: [_kopf("1", net=250.0, gross=250.0)]}, 1)
    )
    respx.get(url__regex=rf"{BASE_URL_V4}/purchase/bills/\d+").mock(
        return_value=httpx.Response(200, json=_detail())
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(200, json=[]))

    bestand = await lieferantenrechnungen_laden(client)
    zeile = bestand.zeilen[0]

    assert zeile["betrag"] == 250.0
    assert zeile["betrag_chf"] == 250.0
    assert "mwst" not in zeile
    assert "brutto" not in zeile


@pytest.mark.asyncio
@respx.mock
async def test_fremdwaehrung_bekommt_ihren_chf_gegenwert(client):
    """Der CHF-Betrag steht nur im Einzelabruf. Wer die Liste allein summiert,
    addiert Euro zu Franken -- eine Summe, die stimmt aussieht und zu klein ist."""
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(
        side_effect=_seiten_route({1: [_kopf("1", net=390.0, currency_code="EUR")]}, 1)
    )
    respx.get(url__regex=rf"{BASE_URL_V4}/purchase/bills/\d+").mock(
        return_value=httpx.Response(200, json=_detail(
            base_currency_amount=409.5, exchange_rate=1.05,
        ))
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(200, json=[]))

    bestand = await lieferantenrechnungen_laden(client)
    zeile = bestand.zeilen[0]

    assert zeile["betrag"] == 390.0
    assert zeile["waehrung"] == "EUR"
    assert zeile["betrag_chf"] == 409.5
    assert zeile["kurs"] == 1.05
    assert bestand.ohne_umrechnung == 0


@pytest.mark.asyncio
@respx.mock
async def test_fehlende_umrechnung_wird_gezaehlt_nicht_geraten(client):
    """Fehlt der Gegenwert, bleibt ``betrag_chf`` null **und** der Fall wird
    gezählt. Den Fremdwährungsbetrag als CHF zu übernehmen wäre der stille Fehler."""
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(
        side_effect=_seiten_route({1: [_kopf("1", net=390.0, currency_code="EUR")]}, 1)
    )
    respx.get(url__regex=rf"{BASE_URL_V4}/purchase/bills/\d+").mock(
        return_value=httpx.Response(200, json=_detail(base_currency_amount=None))
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(200, json=[]))

    bestand = await lieferantenrechnungen_laden(client)

    assert bestand.zeilen[0]["betrag_chf"] == 0.0
    assert bestand.ohne_umrechnung == 1


# ── Einzelabruf ──────────────────────────────────────────

@pytest.mark.asyncio
@respx.mock
async def test_lieferantenkennung_und_konto_kommen_aus_dem_einzelabruf(client):
    """Die Liste kennt den Lieferanten nur als freien Text. Nach ihm zu gruppieren
    ist dieselbe Falle, die bei Pipedrive den Umsatz eines Amts vervielfacht hat."""
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(
        side_effect=_seiten_route({1: [_kopf("1")]}, 1)
    )
    respx.get(url__regex=rf"{BASE_URL_V4}/purchase/bills/\d+").mock(
        return_value=httpx.Response(200, json=_detail(supplier_id=78))
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(
        200, json=[{"id": 42, "account_no": "6512", "name": "Internet"}]
    ))

    zeile = (await lieferantenrechnungen_laden(client)).zeilen[0]

    assert zeile["lieferant_id"] == 78
    assert zeile["konto"] == "6512 Internet"
    assert zeile["konto_nr"] == "6512"


@pytest.mark.asyncio
@respx.mock
async def test_bei_mehreren_positionen_zaehlt_die_groesste_und_die_anzahl(client):
    """Eine Rechnung auf mehrere Konten hat keine eine Kategorie. Die grösste
    Position zu nehmen ist eine Vereinfachung -- und ``positionen`` macht sie
    sichtbar, statt sie zu verschweigen."""
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(
        side_effect=_seiten_route({1: [_kopf("1", net=300.0)]}, 1)
    )
    respx.get(url__regex=rf"{BASE_URL_V4}/purchase/bills/\d+").mock(
        return_value=httpx.Response(200, json=_detail(line_items=[
            {"booking_account_id": 42, "amount": 100.0},
            {"booking_account_id": 43, "amount": 200.0},
        ]))
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(200, json=[
        {"id": 42, "account_no": "6512", "name": "Internet"},
        {"id": 43, "account_no": "4200", "name": "Dienstleistungsaufwand"},
    ]))

    zeile = (await lieferantenrechnungen_laden(client)).zeilen[0]

    assert zeile["konto"] == "4200 Dienstleistungsaufwand"
    assert zeile["positionen"] == 2


@pytest.mark.asyncio
@respx.mock
async def test_ein_misslungener_einzelabruf_ist_eine_luecke_kein_absturz(client):
    """Eine Rechnung ohne Detail hat keinen Lieferanten und kein Konto. Das muss
    als Lücke im Befund stehen -- und darf die anderen 434 nicht mitreissen."""
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(
        side_effect=_seiten_route({1: [_kopf("1"), _kopf("2")]}, 2)
    )
    respx.get(f"{BASE_URL_V4}/purchase/bills/1").mock(
        return_value=httpx.Response(500, json={"error": "kaputt"})
    )
    respx.get(f"{BASE_URL_V4}/purchase/bills/2").mock(
        return_value=httpx.Response(200, json=_detail())
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(200, json=[]))

    bestand = await lieferantenrechnungen_laden(client)

    assert len(bestand.zeilen) == 2
    assert bestand.ohne_detail == ["1"]
    assert bestand.zeilen[0]["lieferant_id"] is None


@pytest.mark.asyncio
@respx.mock
async def test_ausgefallener_kontenplan_laesst_die_kennung_stehen(client):
    """Fällt der Kontenplan aus, bleibt die Kennung roh -- sichtbar unbrauchbar
    statt unsichtbar falsch."""
    respx.get(f"{BASE_URL_V4}/purchase/bills").mock(
        side_effect=_seiten_route({1: [_kopf("1")]}, 1)
    )
    respx.get(url__regex=rf"{BASE_URL_V4}/purchase/bills/\d+").mock(
        return_value=httpx.Response(200, json=_detail())
    )
    respx.get(f"{BASE_URL_V2}/accounts").mock(return_value=httpx.Response(503, json={}))

    zeile = (await lieferantenrechnungen_laden(client)).zeilen[0]

    assert zeile["konto"] == "id 42"

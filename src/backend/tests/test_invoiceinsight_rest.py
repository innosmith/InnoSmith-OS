"""Der Bestandsabzug über die HTTP-Schnittstelle von InvoiceInsight.

Geprüft wird nicht, dass ein Abruf gelingt -- geprüft wird, dass ein
**unvollständiger** Abzug als solcher erkennbar ist. Genau daran ist die
Belegbeschaffung andernorts gescheitert: ein abgebrochener Abzug sah aus wie
ein kleiner Bestand, und niemand konnte den Unterschied sehen.
"""

import json

import httpx
import pytest
import respx

from app.services.invoiceinsight_rest import (
    SchnittstellenFehler,
    beleg_holen,
    beleg_korrigieren,
    rechnungen_holen,
)

WURZEL = "http://invoiceinsight.test"


def _seite(rows, *, total, offset, as_of="2026-09-21T22:00:00Z", fehlend=None):
    """Eine Antwort in der Form, die die Schnittstelle liefert."""
    return {
        "total": total,
        "offset": offset,
        "limit": 2,
        "returned": len(rows),
        "as_of": as_of,
        "schema": {
            "columns": ["id", "betrag"],
            "meaning": {"betrag": "Rechnungsbetrag in CHF, inklusive Steuer"},
        },
        "rows": rows,
        "nicht_auswertbar": fehlend or {},
    }


@pytest.mark.asyncio
@respx.mock
async def test_blaettert_ueber_alle_seiten():
    seiten = [
        _seite([{"id": 1}, {"id": 2}], total=5, offset=0),
        _seite([{"id": 3}, {"id": 4}], total=5, offset=2),
        _seite([{"id": 5}], total=5, offset=4),
        _seite([], total=5, offset=5),
    ]
    aufrufe = iter(seiten)
    route = respx.get(f"{WURZEL}/rechnungen").mock(
        side_effect=lambda request: httpx.Response(200, json=next(aufrufe))
    )

    zeilen, befund = await rechnungen_holen(WURZEL, "geheim", seitengroesse=2)

    assert [z["id"] for z in zeilen] == [1, 2, 3, 4, 5]
    assert befund["gemeldet"] == 5
    assert befund["geholt"] == 5
    assert "unvollstaendig" not in befund
    assert route.call_count == 4


@pytest.mark.asyncio
@respx.mock
async def test_token_wandert_in_den_kopf():
    respx.get(f"{WURZEL}/rechnungen").mock(
        return_value=httpx.Response(200, json=_seite([], total=0, offset=0))
    )

    await rechnungen_holen(WURZEL, "geheim")

    assert respx.calls.last.request.headers["Authorization"] == "Bearer geheim"


@pytest.mark.asyncio
@respx.mock
async def test_abgeschnittener_abzug_wird_gemeldet():
    """Der Server nennt 99 Zeilen und liefert zwei. Das darf nicht durchgehen."""
    seiten = [
        _seite([{"id": 1}, {"id": 2}], total=99, offset=0),
        _seite([], total=99, offset=2),
    ]
    aufrufe = iter(seiten)
    respx.get(f"{WURZEL}/rechnungen").mock(
        side_effect=lambda request: httpx.Response(200, json=next(aufrufe))
    )

    zeilen, befund = await rechnungen_holen(WURZEL, "geheim", seitengroesse=2)

    assert len(zeilen) == 2
    assert befund["unvollstaendig"] == "2 von 99 Zeilen"


@pytest.mark.asyncio
@respx.mock
async def test_gewachsener_bestand_heisst_nicht_unvollstaendig():
    """Während des Blätterns treffen Belege ein -- geholt schlägt angekündigt."""
    seiten = [
        _seite([{"id": 1}, {"id": 2}], total=3, offset=0),
        _seite([{"id": 3}, {"id": 4}], total=4, offset=2),
        _seite([], total=4, offset=4),
    ]
    aufrufe = iter(seiten)
    respx.get(f"{WURZEL}/rechnungen").mock(
        side_effect=lambda request: httpx.Response(200, json=next(aufrufe))
    )

    zeilen, befund = await rechnungen_holen(WURZEL, "geheim", seitengroesse=2)

    assert len(zeilen) == 4
    assert "unvollstaendig" not in befund
    assert befund["bestand_bewegt"] == "4 geholt, 3 angekündigt"


@pytest.mark.asyncio
@respx.mock
async def test_spaltenbedeutung_und_luecken_stehen_im_befund():
    respx.get(f"{WURZEL}/rechnungen").mock(
        return_value=httpx.Response(
            200,
            json=_seite([], total=0, offset=0, fehlend={"ohne_datum": 3}),
        )
    )

    _, befund = await rechnungen_holen(WURZEL, "geheim")

    assert befund["spalten_bedeutung"]["betrag"].startswith("Rechnungsbetrag")
    assert befund["nicht_auswertbare_belege"] == {"ohne_datum": 3}
    assert befund["stand"] == "2026-09-21T22:00:00Z"
    assert "nicht_auswertbar_einzeln" not in befund


@pytest.mark.asyncio
@respx.mock
async def test_einzelliste_wird_auch_leer_weitergereicht():
    """Leer heisst «nichts unlesbar», fehlend heisst «Schnittstelle kennt keine Liste»."""
    seite = _seite([], total=0, offset=0)
    seite["nicht_auswertbare_belege"] = []
    respx.get(f"{WURZEL}/rechnungen").mock(return_value=httpx.Response(200, json=seite))

    _, befund = await rechnungen_holen(WURZEL, "geheim")

    assert befund["nicht_auswertbar_einzeln"] == []


@pytest.mark.asyncio
@respx.mock
async def test_unerreichbare_schnittstelle_scheitert_laut():
    respx.get(f"{WURZEL}/rechnungen").mock(
        side_effect=httpx.ConnectError("Verbindung abgelehnt")
    )

    with pytest.raises(RuntimeError, match="nicht erreichbar"):
        await rechnungen_holen(WURZEL, "geheim")


@pytest.mark.asyncio
@respx.mock
async def test_fehlerstatus_scheitert_laut():
    respx.get(f"{WURZEL}/rechnungen").mock(return_value=httpx.Response(401))

    with pytest.raises(RuntimeError, match="401"):
        await rechnungen_holen(WURZEL, "falsches-token")


@pytest.mark.asyncio
@respx.mock
async def test_einzelbeleg_kommt_mit_allen_feldern():
    respx.get(f"{WURZEL}/rechnungen/906").mock(
        return_value=httpx.Response(
            200, json={"id": 906, "supplier_name_display": "Blinks Labs"}
        )
    )

    beleg = await beleg_holen(WURZEL, "geheim", 906)

    assert beleg["id"] == 906


@pytest.mark.asyncio
@respx.mock
async def test_korrektur_sendet_nur_das_geaenderte_und_den_urheber():
    route = respx.patch(f"{WURZEL}/rechnungen/906").mock(
        return_value=httpx.Response(200, json={"id": 906, "geaendert": ["invoice_number"]})
    )

    ergebnis = await beleg_korrigieren(
        WURZEL, "geheim", 906, {"invoice_number": "R-42"}, "anthony@innosmith.ch"
    )

    gesendet = json.loads(route.calls.last.request.content)
    assert gesendet == {
        "invoice_number": "R-42",
        "geprueft_von": "anthony@innosmith.ch",
    }
    assert ergebnis["geaendert"] == ["invoice_number"]


@pytest.mark.asyncio
@respx.mock
async def test_unbekannter_beleg_bleibt_ein_404():
    respx.get(f"{WURZEL}/rechnungen/9999").mock(
        return_value=httpx.Response(404, json={"detail": "Beleg 9999 gibt es nicht"})
    )

    with pytest.raises(SchnittstellenFehler) as fehler:
        await beleg_holen(WURZEL, "geheim", 9999)

    assert fehler.value.status == 404
    assert "9999" in fehler.value.meldung


@pytest.mark.asyncio
@respx.mock
async def test_abgelehnte_korrektur_behaelt_ihren_wortlaut():
    """Aus «Feld x ist kein gültiges Datum» darf kein «Fehler beim Speichern»
    werden -- sonst weiss der Mensch vor der Maske nicht, was er ändern soll."""
    respx.patch(f"{WURZEL}/rechnungen/906").mock(
        return_value=httpx.Response(
            422, json={"detail": "invoice_date ist kein gültiges Datum"}
        )
    )

    with pytest.raises(SchnittstellenFehler) as fehler:
        await beleg_korrigieren(WURZEL, "geheim", 906, {"invoice_date": "irgendwann"}, "a@b.ch")

    assert fehler.value.status == 422
    assert fehler.value.meldung == "invoice_date ist kein gültiges Datum"


@pytest.mark.asyncio
@respx.mock
async def test_fremde_antwortform_scheitert_laut():
    """Eine ältere Fassung der Schnittstelle liefert etwas ohne ``rows``."""
    respx.get(f"{WURZEL}/rechnungen").mock(
        return_value=httpx.Response(200, json={"belege": []})
    )

    with pytest.raises(RuntimeError, match="keine Bestandsseite"):
        await rechnungen_holen(WURZEL, "geheim")

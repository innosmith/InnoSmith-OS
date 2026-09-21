"""Tests für den Bexio Buchhaltungs-API Client.

Testet:
- Bearer Token Authentifizierung
- Rate-Limit-Retry (429 → automatischer Retry)
- Kontakte, Aufträge, Rechnungen, Projekte
- Kontaktsuche nach Name und E-Mail
"""

import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
import httpx
import respx

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "bexio"))

from bexio_client import (
    BexioClient,
    BexioConfig,
    BASE_URL_V2,
    BASE_URL_V3,
    decode_token_expiry,
)


def _make_jwt(exp_dt: datetime | None) -> str:
    """Baut ein synthetisches JWT (nur Payload relevant) fuer Tests -- kein echtes Secret."""
    def _b64(obj: dict) -> str:
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    header = _b64({"alg": "RS256", "typ": "JWT"})
    payload_dict: dict = {"login_id": "abc"}
    if exp_dt is not None:
        payload_dict["exp"] = int(exp_dt.timestamp())
    payload = _b64(payload_dict)
    return f"{header}.{payload}.signature-irrelevant"


@pytest.fixture
def bx_client():
    return BexioClient(BexioConfig(api_token="test-bexio-token"))


# ── Config ───────────────────────────────────────────────

def test_config_defaults():
    config = BexioConfig()
    assert config.is_configured is False


def test_config_with_token():
    config = BexioConfig(api_token="abc123")
    assert config.is_configured is True


# ── Verbindungstest ──────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_connection_success(bx_client):
    respx.get(f"{BASE_URL_V3}/users/me").mock(
        return_value=httpx.Response(200, json={
            "id": 1,
            "firstname": "Anthony",
            "lastname": "Smith",
            "email": "anthony@example.com",
        })
    )

    result = await bx_client.test_connection()

    assert result["ok"] is True
    assert result["name"] == "Anthony Smith"
    assert result["email"] == "anthony@example.com"


@respx.mock
@pytest.mark.asyncio
async def test_connection_auth_header(bx_client):
    respx.get(f"{BASE_URL_V3}/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1, "firstname": "A", "lastname": "S", "email": "a@b.com"})
    )

    await bx_client.test_connection()

    req = respx.calls[0].request
    assert req.headers["authorization"] == "Bearer test-bexio-token"


# ── Kontakte ─────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_contacts(bx_client):
    respx.get(f"{BASE_URL_V2}/contact").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "name_1": "Muster AG", "mail": "info@muster.ch"},
            {"id": 2, "name_1": "Beispiel GmbH", "mail": "info@beispiel.ch"},
        ])
    )

    contacts = await bx_client.list_contacts(limit=10)

    assert len(contacts) == 2
    assert contacts[0]["name_1"] == "Muster AG"


@respx.mock
@pytest.mark.asyncio
async def test_get_contact(bx_client):
    respx.get(f"{BASE_URL_V2}/contact/1").mock(
        return_value=httpx.Response(200, json={
            "id": 1, "name_1": "Muster AG", "mail": "info@muster.ch",
        })
    )

    contact = await bx_client.get_contact(1)

    assert contact["id"] == 1
    assert contact["name_1"] == "Muster AG"


@respx.mock
@pytest.mark.asyncio
async def test_create_contact(bx_client):
    respx.post(f"{BASE_URL_V2}/contact").mock(
        return_value=httpx.Response(201, json={
            "id": 42, "name_1": "Neue Firma AG",
        })
    )

    result = await bx_client.create_contact({"name_1": "Neue Firma AG", "contact_type_id": 1})

    assert result["id"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_search_contact_by_name(bx_client):
    respx.post(f"{BASE_URL_V2}/contact/search").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "name_1": "Innosmith GmbH", "mail": "info@innosmith.ch"},
        ])
    )

    results = await bx_client.search_contact_by_name("Innosmith")

    assert len(results) == 1
    assert results[0]["name_1"] == "Innosmith GmbH"


@respx.mock
@pytest.mark.asyncio
async def test_search_contact_by_email(bx_client):
    respx.post(f"{BASE_URL_V2}/contact/search").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "name_1": "Innosmith GmbH", "mail": "info@innosmith.ch"},
        ])
    )

    results = await bx_client.search_contact_by_email("info@innosmith.ch")

    assert len(results) == 1


# ── Aufträge ─────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_orders(bx_client):
    respx.get(f"{BASE_URL_V2}/kb_order").mock(
        return_value=httpx.Response(200, json=[
            {"id": 10, "title": "Auftrag 2026-001", "total": "5000.00"},
        ])
    )

    orders = await bx_client.list_orders(limit=10)

    assert len(orders) == 1
    assert orders[0]["title"] == "Auftrag 2026-001"


@respx.mock
@pytest.mark.asyncio
async def test_get_order(bx_client):
    respx.get(f"{BASE_URL_V2}/kb_order/10").mock(
        return_value=httpx.Response(200, json={
            "id": 10, "title": "Auftrag 2026-001",
        })
    )

    order = await bx_client.get_order(10)

    assert order["id"] == 10


# ── Rechnungen ───────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_invoices(bx_client):
    respx.get(f"{BASE_URL_V2}/kb_invoice").mock(
        return_value=httpx.Response(200, json=[
            {"id": 20, "title": "Rechnung 2026-001", "total": "3500.00"},
        ])
    )

    invoices = await bx_client.list_invoices()

    assert len(invoices) == 1


@respx.mock
@pytest.mark.asyncio
async def test_get_invoice_positions_vereint_beide_arten(bx_client):
    """Produktzeilen und Textzeilen kommen aus zwei Pfaden und muessen unterscheidbar bleiben.

    Beide Rohantworten tragen ``id`` und ``text``; ohne ``positionsart`` saehe
    eine Textzeile aus wie eine Produktzeile mit Menge null.
    """
    respx.get(f"{BASE_URL_V2}/kb_invoice/20/kb_position_custom").mock(
        return_value=httpx.Response(200, json=[
            {"id": 1, "pos": 1, "amount": "20.00", "unit_id": 2, "text": "20h/Monat fix"},
            {"id": 2, "pos": 3, "amount": "2.50", "unit_id": 2, "text": "Variable Zusatzstunden"},
        ])
    )
    respx.get(f"{BASE_URL_V2}/kb_invoice/20/kb_position_text").mock(
        return_value=httpx.Response(200, json=[
            {"id": 9, "pos": 2, "text": "Per 31.08.2026 sind 3h verrechnet aber noch nicht geleistet."},
        ])
    )

    positionen = await bx_client.get_invoice_positions(20)

    assert [p["positionsart"] for p in positionen] == ["custom", "text", "custom"]
    assert [p["pos"] for p in positionen] == [1, 2, 3]


@respx.mock
@pytest.mark.asyncio
async def test_get_invoice_positions_verschluckt_keinen_fehler(bx_client):
    """Die Vorlage gab bei jedem Netzfehler ``None`` zurueck.

    In ``admin_core.get_invoice_with_positions`` stand
    ``except requests.exceptions.RequestException: return None``. Ein abgelaufener
    Token ergab damit eine Rechnung ohne Positionen -- also ohne Stunden -- und
    jede Pruefung darueber haette sie als leer durchgewunken, ohne zu murren.
    """
    respx.get(f"{BASE_URL_V2}/kb_invoice/20/kb_position_custom").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await bx_client.get_invoice_positions(20)


@respx.mock
@pytest.mark.asyncio
async def test_rechnung_aus_auftrag_geht_ohne_rumpf(bx_client):
    """Ein leeres ``{}`` quittiert Bexio mit 415 -- gemessen am 21.09.2026.

    Der Test haelt das fest, kann es aber nicht beweisen: ein nachgebildeter
    Server nimmt jeden Rumpf an. Geprueft wird deshalb, dass **nichts**
    gesendet wird; die Gegenprobe lief gegen das echte Bexio.
    """
    route = respx.post(f"{BASE_URL_V2}/kb_order/31/invoice").mock(
        return_value=httpx.Response(201, json={"id": 900, "document_nr": "RE-00700"})
    )

    rechnung = await bx_client.create_invoice_from_order(31)

    assert rechnung["document_nr"] == "RE-00700"
    assert route.calls[0].request.content == b""
    assert "content-type" not in route.calls[0].request.headers


@respx.mock
@pytest.mark.asyncio
async def test_entwurf_wird_ausgestellt(bx_client):
    respx.get(f"{BASE_URL_V2}/kb_invoice/706").mock(
        return_value=httpx.Response(200, json={"id": 706, "kb_item_status_id": 7})
    )
    route = respx.post(f"{BASE_URL_V2}/kb_invoice/706/issue").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    assert await bx_client.issue_invoice(706) is True
    assert route.call_count == 1


@respx.mock
@pytest.mark.asyncio
async def test_bereits_ausgestellte_rechnung_ist_nichts_zu_tun(bx_client):
    """Sonst scheiterte jedes Wiederholen nach einem Abbruch an genau den
    Rechnungen, die schon durch sind."""
    respx.get(f"{BASE_URL_V2}/kb_invoice/706").mock(
        return_value=httpx.Response(200, json={"id": 706, "kb_item_status_id": 8})
    )
    route = respx.post(f"{BASE_URL_V2}/kb_invoice/706/issue").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    assert await bx_client.issue_invoice(706) is False
    assert route.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_ausstellen_verschickt_nichts(bx_client):
    """``send`` stellt aus **und** mailt an die Kundschaft. Ein Tippfehler im
    Pfad waere der Unterschied zwischen einem Statuswechsel und zwanzig
    ungeprueften Mails."""
    respx.get(f"{BASE_URL_V2}/kb_invoice/706").mock(
        return_value=httpx.Response(200, json={"id": 706, "kb_item_status_id": 7})
    )
    respx.post(f"{BASE_URL_V2}/kb_invoice/706/issue").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    versand = respx.post(f"{BASE_URL_V2}/kb_invoice/706/send").mock(
        return_value=httpx.Response(200, json={"success": True})
    )
    vermerk = respx.post(f"{BASE_URL_V2}/kb_invoice/706/mark_as_sent").mock(
        return_value=httpx.Response(200, json={"success": True})
    )

    await bx_client.issue_invoice(706)

    assert versand.call_count == 0
    assert vermerk.call_count == 0


@respx.mock
@pytest.mark.asyncio
async def test_pdf_wird_aus_base64_dekodiert(bx_client):
    """Bexio liefert JSON mit Base64, nicht das PDF.

    Am 21.09.2026 an RE-00706 gemessen: Felder ``name``, ``size``, ``mime``,
    ``content``; ``size`` = 310436 entsprach der dekodierten Laenge, Signatur
    ``%PDF-1.5``.
    """
    inhalt = b"%PDF-1.5\nhier stuende das Dokument\n%%EOF"
    respx.get(f"{BASE_URL_V2}/kb_invoice/706/pdf").mock(
        return_value=httpx.Response(200, json={
            "name": "re-00706.pdf", "size": len(inhalt), "mime": "application/pdf",
            "content": base64.b64encode(inhalt).decode(),
        })
    )

    assert await bx_client.get_invoice_pdf(706) == inhalt


@respx.mock
@pytest.mark.asyncio
async def test_pdf_ohne_signatur_wird_abgewiesen(bx_client):
    """Eine Datei, die wie ein PDF heisst und keines ist, faellt sonst erst
    auf, wenn jemand sie oeffnet -- moeglicherweise die Kundschaft."""
    respx.get(f"{BASE_URL_V2}/kb_invoice/706/pdf").mock(
        return_value=httpx.Response(200, json={
            "content": base64.b64encode(b"<html>Sitzung abgelaufen</html>").decode(),
        })
    )

    with pytest.raises(ValueError, match="Signatur"):
        await bx_client.get_invoice_pdf(706)


@respx.mock
@pytest.mark.asyncio
async def test_pdf_ohne_inhalt_wird_abgewiesen(bx_client):
    """Null Bytes sehen auf jedem Verzeichnislisting aus wie ein Ergebnis."""
    respx.get(f"{BASE_URL_V2}/kb_invoice/706/pdf").mock(
        return_value=httpx.Response(200, json={"name": "re-00706.pdf", "content": ""})
    )

    with pytest.raises(ValueError, match="kein PDF"):
        await bx_client.get_invoice_pdf(706)


@respx.mock
@pytest.mark.asyncio
async def test_pdf_fehler_wird_nicht_verschluckt(bx_client):
    respx.get(f"{BASE_URL_V2}/kb_invoice/706/pdf").mock(
        return_value=httpx.Response(404, json={"message": "not found"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await bx_client.get_invoice_pdf(706)


@respx.mock
@pytest.mark.asyncio
async def test_update_invoice_schickt_nur_die_genannten_felder(bx_client):
    """Ein mitgeschicktes Feld mit Vorgabewert ueberschriebe stillschweigend,
    was auf der Rechnung steht -- etwa den Titel oder die Kontaktadresse."""
    route = respx.post(f"{BASE_URL_V2}/kb_invoice/900").mock(
        return_value=httpx.Response(200, json={"id": 900, "is_valid_from": "2026-09-30"})
    )

    await bx_client.update_invoice(
        900, is_valid_from="2026-09-30", is_valid_to="2026-10-29"
    )

    assert json.loads(route.calls[0].request.content) == {
        "is_valid_from": "2026-09-30", "is_valid_to": "2026-10-29",
    }


@pytest.mark.asyncio
async def test_update_invoice_ohne_felder_ist_ein_fehler(bx_client):
    """Ein Aufruf ohne Inhalt waere ein Schreibzugriff ohne Absicht."""
    with pytest.raises(ValueError):
        await bx_client.update_invoice(900)


@respx.mock
@pytest.mark.asyncio
async def test_fehler_beim_erzeugen_wird_nicht_verschluckt(bx_client):
    respx.post(f"{BASE_URL_V2}/kb_order/31/invoice").mock(
        return_value=httpx.Response(422, json={"message": "Auftrag bereits verrechnet"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await bx_client.create_invoice_from_order(31)


@respx.mock
@pytest.mark.asyncio
async def test_update_invoice_position_schickt_genau_die_uebergebenen_felder(bx_client):
    """Nichts wird ergaenzt und nichts mit Vorgabewerten aufgefuellt.

    Die Vorlage setzte ``unit_price`` notfalls auf ``'200'``. Bei einem Vertrag
    zu 250 CHF waeren das 50 CHF je Stunde Schaden -- auf einer Rechnung, die
    zum Kunden geht, und ohne Meldung.
    """
    route = respx.put(f"{BASE_URL_V2}/kb_invoice/20/kb_position_custom/7").mock(
        return_value=httpx.Response(200, json={"id": 7, "amount": "35.25"})
    )

    await bx_client.update_invoice_position(20, 7, "custom", {
        "amount": "35.25", "unit_price": "250.000000", "tax_id": 36,
        "text": "Effektiver Aufwand", "unit_id": 2,
    })

    assert json.loads(route.calls[0].request.content) == {
        "amount": "35.25", "unit_price": "250.000000", "tax_id": 36,
        "text": "Effektiver Aufwand", "unit_id": 2,
    }


@respx.mock
@pytest.mark.asyncio
async def test_textposition_geht_an_den_eigenen_pfad(bx_client):
    """Zwei Ressourcen, zwei Pfade -- sonst 404 auf eine Position, die es gibt."""
    route = respx.put(f"{BASE_URL_V2}/kb_invoice/20/kb_position_text/9").mock(
        return_value=httpx.Response(200, json={"id": 9})
    )

    await bx_client.update_invoice_position(20, 9, "text", {"text": "Per 30.09.2026 …"})

    assert route.called


@pytest.mark.asyncio
async def test_unbekannte_positionsart_wird_abgewiesen(bx_client):
    """Sonst entstuende der Pfad ``kb_position_article`` aus einem Tippfehler."""
    with pytest.raises(ValueError):
        await bx_client.update_invoice_position(20, 7, "produkt", {"amount": "1"})


@respx.mock
@pytest.mark.asyncio
async def test_delete_invoice_position_verschluckt_keinen_fehler(bx_client):
    """``admin_core`` gab hier ``False`` zurueck -- ununterscheidbar von
    «die Position liess sich nicht loeschen»."""
    respx.delete(f"{BASE_URL_V2}/kb_invoice/20/kb_position_custom/7").mock(
        return_value=httpx.Response(401, json={"message": "Unauthorized"})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await bx_client.delete_invoice_position(20, 7, "custom")


# ── Projekte ─────────────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_list_projects(bx_client):
    respx.get(f"{BASE_URL_V2}/pr_project").mock(
        return_value=httpx.Response(200, json=[
            {"id": 5, "name": "TaskPilot", "status_id": 1},
        ])
    )

    projects = await bx_client.list_projects()

    assert len(projects) == 1
    assert projects[0]["name"] == "TaskPilot"


@respx.mock
@pytest.mark.asyncio
async def test_get_project(bx_client):
    respx.get(f"{BASE_URL_V2}/pr_project/5").mock(
        return_value=httpx.Response(200, json={
            "id": 5, "name": "TaskPilot",
        })
    )

    project = await bx_client.get_project(5)

    assert project["name"] == "TaskPilot"


# ── Rate-Limit Retry ─────────────────────────────────────

@respx.mock
@pytest.mark.asyncio
async def test_rate_limit_retry(bx_client):
    """429 wird automatisch wiederholt."""
    route = respx.get(f"{BASE_URL_V2}/contact")
    route.side_effect = [
        httpx.Response(429, json={"error": "Rate limit exceeded"}),
        httpx.Response(200, json=[{"id": 1, "name_1": "Test"}]),
    ]

    result = await bx_client.list_contacts()

    assert len(result) == 1
    assert route.call_count == 2


# ── Token-Ablauf (PAT laeuft nach 6 Monaten ab) ──────────

def test_token_expiry_valid_token():
    """Gueltiger Token: days_remaining positiv, nicht abgelaufen."""
    token = _make_jwt(datetime.now(timezone.utc) + timedelta(days=90))
    info = decode_token_expiry(token)
    assert info is not None
    assert info["is_expired"] is False
    assert 89 <= info["days_remaining"] <= 90


def test_token_expiry_expired_token():
    """Abgelaufener Token wird als is_expired=True erkannt."""
    token = _make_jwt(datetime.now(timezone.utc) - timedelta(days=2))
    info = decode_token_expiry(token)
    assert info is not None
    assert info["is_expired"] is True
    assert info["days_remaining"] < 0


def test_token_expiry_near_expiry():
    """Token kurz vor Ablauf liefert kleinen, positiven days_remaining."""
    token = _make_jwt(datetime.now(timezone.utc) + timedelta(days=5))
    info = decode_token_expiry(token)
    assert info is not None
    assert info["is_expired"] is False
    assert 4 <= info["days_remaining"] <= 5


def test_token_expiry_no_exp_claim():
    """JWT ohne exp-Claim -> None (kein Ablauf bestimmbar)."""
    assert decode_token_expiry(_make_jwt(None)) is None


@pytest.mark.parametrize("bad_token", ["", "not-a-jwt", "only.two", "a.b.c.d"])
def test_token_expiry_non_jwt(bad_token):
    """Nicht-JWT (z.B. alter statischer Token) -> None."""
    assert decode_token_expiry(bad_token) is None


def test_client_token_expiry_method():
    """BexioClient.token_expiry() delegiert an decode_token_expiry."""
    token = _make_jwt(datetime.now(timezone.utc) + timedelta(days=30))
    client = BexioClient(BexioConfig(api_token=token))
    info = client.token_expiry()
    assert info is not None
    assert info["is_expired"] is False

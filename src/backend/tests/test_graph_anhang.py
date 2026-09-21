"""Tests für Anhänge an Mailentwürfen.

Die interessante Stelle ist nicht, dass ein Anhang ankommt, sondern **wo die
Grenze zwischen den beiden Wegen liegt**. Graph nimmt bis 3 MB Base64 im Rumpf
entgegen und verlangt darüber eine Upload-Sitzung. Base64 bläht um ein Drittel
auf — eine Prüfung auf die Rohgrösse läge in einem Streifen von 750 KB Breite
falsch, und dort gäbe es keinen Fehler, sondern einen abgelehnten Anhang mitten
im Rechnungslauf.
"""

import base64

import httpx
import pytest
import respx

import graph_client as gc
from graph_client import GraphClient, GraphConfig

GRAPH = "https://graph.microsoft.com/v1.0"
NACHRICHT = f"{GRAPH}/users/user@example.com/messages/AAMk-1"


@pytest.fixture
def graph():
    client = GraphClient(GraphConfig(
        tenant_id="t", client_id="c", client_secret="s",
        user_email="user@example.com",
    ))
    client._token.access_token = "fake-token"
    client._token.expires_at = 9999999999.0
    return client


@respx.mock
@pytest.mark.asyncio
async def test_kleiner_anhang_geht_direkt_in_den_rumpf(graph):
    route = respx.post(f"{NACHRICHT}/attachments").mock(
        return_value=httpx.Response(201, json={"id": "anhang-1"})
    )

    kennung = await graph.add_attachment("AAMk-1", "RE-00706.pdf", b"%PDF-1.5 klein")

    assert kennung == "anhang-1"
    gesendet = route.calls[0].request.read()
    assert b"microsoft.graph.fileAttachment" in gesendet
    assert base64.b64encode(b"%PDF-1.5 klein") in gesendet


@respx.mock
@pytest.mark.asyncio
async def test_die_grenze_zaehlt_die_kodierte_laenge(graph, monkeypatch):
    """Eine Datei knapp unter der Rohgrenze liegt kodiert schon darüber.

    Genau dieser Streifen ist der Fehler, den die naheliegende Prüfung
    ``len(inhalt) > 3 MB`` einbaut. Der Schwellwert wird hier verkleinert,
    damit der Test nicht mit Megabytes hantieren muss — geprüft wird die
    Rechenweise, nicht die Zahl.
    """
    monkeypatch.setattr(gc, "_ANHANG_EINFACH_MAX", 100)
    monkeypatch.setattr(gc, "_UPLOAD_STUECK", 64)

    # 80 Rohbytes: unter 100, kodiert aber 108 — also über der Grenze.
    inhalt = b"x" * 80
    assert len(inhalt) < 100 < len(base64.b64encode(inhalt))

    respx.post(f"{NACHRICHT}/attachments/createUploadSession").mock(
        return_value=httpx.Response(201, json={"uploadUrl": "https://upload.test/s1"})
    )
    stuecke = respx.put("https://upload.test/s1").mock(
        side_effect=[
            httpx.Response(200, json={"nextExpectedRanges": ["64"]}),
            httpx.Response(201, json={"id": "anhang-gross"}),
        ]
    )

    kennung = await graph.add_attachment("AAMk-1", "gross.pdf", inhalt)

    assert kennung == "anhang-gross"
    assert stuecke.call_count == 2


@respx.mock
@pytest.mark.asyncio
async def test_stuecke_sind_lueckenlos_und_richtig_bereichsangegeben(graph, monkeypatch):
    """Ein krummer ``Content-Range`` quittiert Graph mit 400 — auf eine Angabe,
    die richtig aussieht."""
    monkeypatch.setattr(gc, "_ANHANG_EINFACH_MAX", 10)
    monkeypatch.setattr(gc, "_UPLOAD_STUECK", 64)

    inhalt = bytes(range(256)) * 1  # 256 Bytes → 64 + 64 + 64 + 64
    respx.post(f"{NACHRICHT}/attachments/createUploadSession").mock(
        return_value=httpx.Response(201, json={"uploadUrl": "https://upload.test/s1"})
    )
    stuecke = respx.put("https://upload.test/s1").mock(
        side_effect=[httpx.Response(200, json={})] * 3
        + [httpx.Response(201, json={"id": "fertig"})]
    )

    await graph.add_attachment("AAMk-1", "gross.pdf", inhalt)

    bereiche = [c.request.headers["Content-Range"] for c in stuecke.calls]
    assert bereiche == [
        "bytes 0-63/256", "bytes 64-127/256",
        "bytes 128-191/256", "bytes 192-255/256",
    ]
    # Zusammengesetzt muss wieder die Datei herauskommen, sonst wäre der
    # Anhang beim Empfänger beschädigt — und das sieht man ihm nicht an.
    assert b"".join(c.request.read() for c in stuecke.calls) == inhalt


@respx.mock
@pytest.mark.asyncio
async def test_kein_bearer_token_an_die_upload_url(graph, monkeypatch):
    """Die URL trägt ihre eigene Ermächtigung; ein Authorization-Header
    führt dort zu 401."""
    monkeypatch.setattr(gc, "_ANHANG_EINFACH_MAX", 10)
    monkeypatch.setattr(gc, "_UPLOAD_STUECK", 1024)

    respx.post(f"{NACHRICHT}/attachments/createUploadSession").mock(
        return_value=httpx.Response(201, json={"uploadUrl": "https://upload.test/s1"})
    )
    stuecke = respx.put("https://upload.test/s1").mock(
        return_value=httpx.Response(201, json={"id": "fertig"})
    )

    await graph.add_attachment("AAMk-1", "gross.pdf", b"y" * 50)

    assert "authorization" not in stuecke.calls[0].request.headers


@respx.mock
@pytest.mark.asyncio
async def test_fehlende_upload_url_wird_gemeldet(graph, monkeypatch):
    """Ohne diese Prüfung liefe die Schleife gegen ``None`` und der Anhang
    fehlte stillschweigend an einer Mail, die trotzdem versandfertig aussieht."""
    monkeypatch.setattr(gc, "_ANHANG_EINFACH_MAX", 10)

    respx.post(f"{NACHRICHT}/attachments/createUploadSession").mock(
        return_value=httpx.Response(201, json={})
    )

    with pytest.raises(RuntimeError, match="uploadUrl"):
        await graph.add_attachment("AAMk-1", "gross.pdf", b"y" * 50)


@respx.mock
@pytest.mark.asyncio
async def test_abgelehnter_anhang_wird_nicht_verschluckt(graph):
    respx.post(f"{NACHRICHT}/attachments").mock(
        return_value=httpx.Response(413, json={"error": {"message": "too large"}})
    )

    with pytest.raises(httpx.HTTPStatusError):
        await graph.add_attachment("AAMk-1", "RE-00706.pdf", b"%PDF klein")

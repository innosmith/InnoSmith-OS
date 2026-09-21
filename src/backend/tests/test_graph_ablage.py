"""Tests für den Schreibzugriff auf OneDrive — Ordnerkette und Datei-Upload.

Drei Stellen sind heikel und nur hier prüfbar:

**Der 404 darf nicht zum Sammelbecken werden.** ``drive_item_by_path`` gibt
``None`` für «gibt es nicht» — und muss jeden anderen Fehler weiterfliegen
lassen. Ein verschluckter Zeitausfall sähe wie eine freie Stelle aus, und der
Aufrufer schriebe über eine Datei, die er für abwesend hält.

**Nicht überschreiben ist die Voreinstellung.** ``conflictBehavior`` muss am
Aufruf ankommen, nicht bloss im Docstring stehen.

**Die Grenze zählt hier die Rohgrösse**, anders als beim Mailanhang: der Rumpf
ist binär, nicht Base64. Beide Zahlen im selben Modul zu haben, ohne dass ein
Test sie auseinanderhält, wäre eine Einladung zum Verwechseln.
"""

import httpx
import pytest
import respx

import graph_client as gc
from graph_client import GraphClient, GraphConfig

GRAPH = "https://graph.microsoft.com/v1.0"
WURZEL = f"{GRAPH}/users/user@example.com/drive/root"


@pytest.fixture
def graph():
    client = GraphClient(GraphConfig(
        tenant_id="t", client_id="c", client_secret="s",
        user_email="user@example.com",
    ))
    client._token.access_token = "fake-token"
    client._token.expires_at = 9999999999.0
    return client


# ── Vorhandensein ────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_vorhandene_datei_kommt_zurueck(graph):
    respx.get(f"{WURZEL}:/Finanzen/Debitoren/AUE/2026/RE-00700.pdf").mock(
        return_value=httpx.Response(200, json={"id": "x1", "size": 371624})
    )
    item = await graph.drive_item_by_path("Finanzen/Debitoren/AUE/2026/RE-00700.pdf")
    assert item is not None and item["size"] == 371624


@respx.mock
@pytest.mark.asyncio
async def test_fehlende_datei_ergibt_none(graph):
    respx.get(f"{WURZEL}:/Finanzen/Debitoren/AUE/2026/fehlt.pdf").mock(
        return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}})
    )
    assert await graph.drive_item_by_path("Finanzen/Debitoren/AUE/2026/fehlt.pdf") is None


@respx.mock
@pytest.mark.asyncio
async def test_anderer_fehler_wird_nicht_zu_none(graph):
    """Ein 500 ist nicht «gibt es nicht» — sonst würde überschrieben."""
    respx.get(f"{WURZEL}:/Finanzen/kaputt.pdf").mock(
        return_value=httpx.Response(500, json={"error": {"code": "serviceError"}})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await graph.drive_item_by_path("Finanzen/kaputt.pdf")


@respx.mock
@pytest.mark.asyncio
async def test_fehlende_berechtigung_bleibt_ein_fehler(graph):
    """403 darf nicht als «nicht vorhanden» durchgehen."""
    respx.get(f"{WURZEL}:/Finanzen/gesperrt.pdf").mock(
        return_value=httpx.Response(403, json={"error": {"message": "no Files.ReadWrite"}})
    )
    with pytest.raises(PermissionError):
        await graph.drive_item_by_path("Finanzen/gesperrt.pdf")


# ── Ordnerkette ──────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_ordnerkette_legt_nur_das_fehlende_an(graph):
    """Vorhandene Ebenen werden gelesen, nicht ersetzt."""
    respx.get(f"{WURZEL}:/Finanzen").mock(
        return_value=httpx.Response(200, json={"id": "d1", "folder": {}})
    )
    respx.get(f"{WURZEL}:/Finanzen/Debitoren").mock(
        return_value=httpx.Response(200, json={"id": "d2", "folder": {}})
    )
    respx.get(f"{WURZEL}:/Finanzen/Debitoren/GSW Treuhand AG").mock(
        return_value=httpx.Response(200, json={"id": "d3", "folder": {}})
    )
    respx.get(f"{WURZEL}:/Finanzen/Debitoren/GSW Treuhand AG/2026").mock(
        return_value=httpx.Response(404, json={"error": {"code": "itemNotFound"}})
    )
    angelegt = respx.post(
        f"{WURZEL}:/Finanzen/Debitoren/GSW Treuhand AG:/children"
    ).mock(return_value=httpx.Response(201, json={"id": "d4", "folder": {}}))

    ergebnis = await graph.ensure_drive_folder(
        "Finanzen/Debitoren/GSW Treuhand AG/2026"
    )

    assert ergebnis["id"] == "d4"
    assert angelegt.call_count == 1
    rumpf = angelegt.calls[0].request.read()
    assert b'"2026"' in rumpf
    assert b'"fail"' in rumpf, "Ein bestehender Ordner darf nicht ersetzt werden"


@respx.mock
@pytest.mark.asyncio
async def test_datei_im_pfad_bricht_ab(graph):
    """Läge auf einer Ebene eine Datei, landete die Ablage am falschen Ort."""
    respx.get(f"{WURZEL}:/Finanzen").mock(
        return_value=httpx.Response(200, json={"id": "f1", "file": {}})
    )
    with pytest.raises(ValueError, match="kein Ordner"):
        await graph.ensure_drive_folder("Finanzen/Debitoren")


@respx.mock
@pytest.mark.asyncio
async def test_leerer_ordner_gilt_als_ordner(graph):
    """``"folder": {}`` ist ein Ordner — und in Python zugleich falsch.

    Dieser Test entstand aus dem Fehler: mit ``not item.get("folder")`` brach
    die Ablage bei einem leeren Ordner ab, mit der Meldung, er sei eine Datei.
    Geprüft wird das Vorhandensein der Facette, nicht ihr Wahrheitswert.
    """
    respx.get(f"{WURZEL}:/Finanzen").mock(
        return_value=httpx.Response(200, json={"id": "d1", "folder": {}})
    )
    ergebnis = await graph.ensure_drive_folder("Finanzen")
    assert ergebnis["id"] == "d1"


@pytest.mark.asyncio
async def test_leerer_pfad_wird_abgelehnt(graph):
    with pytest.raises(ValueError):
        await graph.ensure_drive_folder("/")


# ── Datei schreiben ──────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_kleine_datei_geht_in_einem_zug(graph):
    route = respx.put(f"{WURZEL}:/Finanzen/Debitoren/AUE/2026/RE.pdf:/content").mock(
        return_value=httpx.Response(201, json={"id": "neu", "size": 9})
    )
    ergebnis = await graph.upload_drive_file(
        "Finanzen/Debitoren/AUE/2026/RE.pdf", b"%PDF-1.5"
    )
    assert ergebnis["id"] == "neu"
    assert "conflictBehavior=fail" in str(route.calls[0].request.url)


@respx.mock
@pytest.mark.asyncio
async def test_ueberschreiben_nur_auf_ausdrueckliches_verlangen(graph):
    route = respx.put(f"{WURZEL}:/Finanzen/x.pdf:/content").mock(
        return_value=httpx.Response(200, json={"id": "ersetzt"})
    )
    await graph.upload_drive_file("Finanzen/x.pdf", b"neu", ueberschreiben=True)
    assert "conflictBehavior=replace" in str(route.calls[0].request.url)


@respx.mock
@pytest.mark.asyncio
async def test_bestehende_datei_erzeugt_einen_fehler(graph):
    """Graph lehnt ab, und der Fehler bleibt sichtbar."""
    respx.put(f"{WURZEL}:/Finanzen/x.pdf:/content").mock(
        return_value=httpx.Response(409, json={"error": {"code": "nameAlreadyExists"}})
    )
    with pytest.raises(httpx.HTTPStatusError):
        await graph.upload_drive_file("Finanzen/x.pdf", b"neu")


@respx.mock
@pytest.mark.asyncio
async def test_grosse_datei_laeuft_ueber_eine_sitzung(graph, monkeypatch):
    """Über der Grenze wird gestückelt — und die Stücke sind vollständig.

    Die beiden Zahlen werden verkleinert, damit der Test nicht mit Megabytes
    hantiert; geprüft wird die Rechenweise, nicht die Zahl.
    """
    monkeypatch.setattr(gc, "_UPLOAD_EINFACH_MAX", 10)
    monkeypatch.setattr(gc, "_UPLOAD_STUECK", 4)

    respx.post(f"{WURZEL}:/Finanzen/gross.pdf:/createUploadSession").mock(
        return_value=httpx.Response(200, json={"uploadUrl": "https://up.example/s1"})
    )
    stuecke: list[bytes] = []
    bereiche: list[str] = []

    def entgegennehmen(request: httpx.Request) -> httpx.Response:
        stuecke.append(request.read())
        bereiche.append(request.headers["Content-Range"])
        if len(stuecke) < 3:
            return httpx.Response(202, json={})
        return httpx.Response(201, json={"id": "fertig", "size": 11})

    respx.put("https://up.example/s1").mock(side_effect=entgegennehmen)

    inhalt = b"0123456789A"  # 11 Bytes -> 4 + 4 + 3
    ergebnis = await graph.upload_drive_file("Finanzen/gross.pdf", inhalt)

    assert ergebnis["id"] == "fertig"
    assert b"".join(stuecke) == inhalt, "Die Stücke ergeben nicht das Ganze"
    assert bereiche == [
        "bytes 0-3/11", "bytes 4-7/11", "bytes 8-10/11",
    ]


@respx.mock
@pytest.mark.asyncio
async def test_sitzung_ohne_url_bricht_ab(graph, monkeypatch):
    monkeypatch.setattr(gc, "_UPLOAD_EINFACH_MAX", 1)
    respx.post(f"{WURZEL}:/Finanzen/gross.pdf:/createUploadSession").mock(
        return_value=httpx.Response(200, json={})
    )
    with pytest.raises(RuntimeError, match="uploadUrl"):
        await graph.upload_drive_file("Finanzen/gross.pdf", b"zu gross")


def test_die_beiden_grenzen_sind_verschieden():
    """Mailanhang zählt kodiert, OneDrive zählt roh — 3 MB gegen 4 MB.

    Stünden beide Zahlen gleich da, würde eine von ihnen irgendwann der
    anderen nachgezogen, und der Fehler wäre ein abgelehnter Upload mitten im
    Rechnungslauf.
    """
    assert gc._ANHANG_EINFACH_MAX == 3 * 1024 * 1024
    assert gc._UPLOAD_EINFACH_MAX == 4 * 1024 * 1024
    assert gc._UPLOAD_STUECK % (320 * 1024) == 0


# ── Blätterung ───────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_ordnerinhalt_folgt_dem_verweis_auf_die_naechste_seite(graph):
    """Der Fehler, der aussieht wie ein Ergebnis.

    Graph liefert Ordnerinhalte seitenweise. Ohne ``@odata.nextLink`` zu
    folgen, gelingt der Aufruf, wirft nichts und liefert die erste Seite --
    eine Liste, die aussieht wie das Ganze. Der Lieferantenordner hat 149
    Unterordner und lieferte 20; die Sperrklinke in ``debitoren_ablage``
    hätte bei einem Kunden ab dem 21. Ordner «kein Projektordner vorhanden»
    geantwortet und flach abgelegt.
    """
    seite2 = f"{GRAPH}/naechste-seite"
    respx.get(f"{WURZEL}:/Finanzen:/children").mock(return_value=httpx.Response(
        200, json={
            "value": [{"id": "1", "name": "A"}, {"id": "2", "name": "B"}],
            "@odata.nextLink": seite2,
        },
    ))
    respx.get(seite2).mock(return_value=httpx.Response(
        200, json={"value": [{"id": "3", "name": "C"}]},
    ))

    eintraege = await graph.list_drive_items("Finanzen")

    assert [e["name"] for e in eintraege] == ["A", "B", "C"]


@respx.mock
@pytest.mark.asyncio
async def test_derselbe_verweis_zweimal_beendet_die_blaetterung(graph):
    """Gegen die Verschlimmbesserung.

    Ein stiller Abbruch bei 20 Einträgen ist schlecht; ein Aufruf, der nie
    zurückkehrt, ist schlimmer. Zeigt Graph zweimal auf dieselbe Seite, ist
    Schluss.
    """
    selbstbezug = f"{GRAPH}/immer-dieselbe"
    respx.get(f"{WURZEL}:/Finanzen:/children").mock(return_value=httpx.Response(
        200, json={"value": [{"id": "1", "name": "A"}], "@odata.nextLink": selbstbezug},
    ))
    respx.get(selbstbezug).mock(return_value=httpx.Response(
        200, json={"value": [{"id": "2", "name": "B"}], "@odata.nextLink": selbstbezug},
    ))

    eintraege = await graph.list_drive_items("Finanzen")

    assert [e["name"] for e in eintraege] == ["A", "B"]

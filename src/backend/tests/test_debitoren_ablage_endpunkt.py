"""Tests für den Ablage-Endpunkt.

Geprüft wird vor allem, was er **verweigert**: eine Ablage ohne bestätigten
Versand, eine zweite Ablage derselben Rechnung, und jedes Überschreiben.

Der Vertrag hinter ``RECHNUNG`` ist «OnboardingQS» bei Swiss Bankers. Die
Kundschaft hat mehrere aktive Verträge, also entsteht die Projektebene — was
den Test zugleich zur Probe auf ``zielordner`` macht.
"""

from __future__ import annotations

import pytest

from test_debitoren_anwenden import FakeDb, RECHNUNG
from test_debitoren_erzeugen_endpunkt import FakeToggl7
from test_debitoren_mail_endpunkt import LaufbuchDb, PdfBexio


class AblageGraph:
    """Mailpostfach und OneDrive in einem — der Endpunkt braucht beides."""

    def __init__(self, vorhanden: dict[str, int] | None = None):
        self.entwuerfe: list[dict] = []
        self.anhaenge: list[tuple] = []
        self.gesendet: list[str] = []
        self.dateien: dict[str, int] = dict(vorhanden or {})
        self.ordner: set[str] = set()
        self.uploads: list[str] = []
        self._n = 0

    # -- Postfach ------------------------------------------------------
    async def create_draft(self, **kwargs):
        self._n += 1
        self.entwuerfe.append(kwargs)
        return {"id": f"AAMk-{self._n}"}

    async def add_attachment(self, kennung, name, inhalt, mime="application/pdf"):
        self.anhaenge.append((kennung, name, inhalt))
        return "anhang"

    async def delete_message(self, kennung):
        pass

    async def send_draft(self, kennung):
        self.gesendet.append(kennung)

    # -- OneDrive ------------------------------------------------------
    async def list_drive_items(self, pfad, top=20):
        pfad = pfad.strip("/")
        eintraege: list[dict] = []
        for d in self.ordner:
            if d.rsplit("/", 1)[0] == pfad:
                eintraege.append({"name": d.rsplit("/", 1)[-1], "folder": {}})
        for f in self.dateien:
            if f.rsplit("/", 1)[0] == pfad:
                eintraege.append({"name": f.rsplit("/", 1)[-1], "file": {}})
        return eintraege

    async def drive_item_by_path(self, pfad):
        pfad = pfad.strip("/")
        if pfad in self.dateien:
            return {"id": f"id-{pfad}", "size": self.dateien[pfad], "file": {}}
        if pfad in self.ordner:
            return {"id": f"dir-{pfad}", "folder": {}}
        return None

    async def ensure_drive_folder(self, pfad):
        bisher = ""
        for teil in [t for t in pfad.strip("/").split("/") if t]:
            bisher = f"{bisher}/{teil}" if bisher else teil
            self.ordner.add(bisher)
        return {"id": f"dir-{pfad}"}

    async def upload_drive_file(self, pfad, inhalt, *, ueberschreiben=False):
        pfad = pfad.strip("/")
        if pfad in self.dateien and not ueberschreiben:
            raise RuntimeError("nameAlreadyExists")
        self.uploads.append(pfad)
        self.dateien[pfad] = len(inhalt)
        return {"id": f"id-{pfad}", "size": len(inhalt)}


ZIEL = "Finanzen/Debitoren/Swiss Bankers/2026/OnboardingQS"


@pytest.fixture
def aufbau(monkeypatch):
    from app.database import get_db
    from app.main import app
    from app.services import fachsysteme, graph as graph_modul

    db = LaufbuchDb()

    def aufsetzen(bexio, toggl=None, graph=None):
        graph = graph or AblageGraph()
        monkeypatch.setattr(fachsysteme, "bexio_zugang", lambda user: bexio)
        monkeypatch.setattr(
            fachsysteme, "toggl_zugang",
            lambda user: (toggl or FakeToggl7([]), 1),
        )
        monkeypatch.setattr(graph_modul, "get_graph_client", lambda: graph)
        app.dependency_overrides[get_db] = lambda: db
        return db, graph

    yield aufsetzen
    app.dependency_overrides.pop(get_db, None)


async def _bis_versandbestaetigung(client, bexio):
    """Den Lauf so weit führen, dass die Ablage erlaubt ist."""
    await client.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )
    antwort = await client.post(
        "/api/debtors/pruefung/versendet",
        json={"month": "2026-08", "rechnung_id": 77},
    )
    assert antwort.status_code == 200, antwort.text


# ── Die Sperre ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_ohne_versandbestaetigung_wird_nichts_abgelegt(client_as_owner, aufbau):
    """Das Archiv hält fest, was die Kundschaft hat — nicht, was geplant ist."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    _db, graph = aufbau(bexio)
    await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.status_code == 200, antwort.text
    ergebnis = antwort.json()
    assert ergebnis["abgelegt"] == 0
    assert graph.uploads == []
    assert "nicht bestätigt" in ergebnis["protokoll"][0]["hindernis"]


@pytest.mark.asyncio
async def test_ohne_echt_wird_der_zielpfad_gezeigt_aber_nichts_geschrieben(
    client_as_owner, aufbau
):
    """Der Trockenlauf beantwortet die Frage, die bei der Ablage zählt: wohin.

    Der Pfad entsteht echt — Ordner-Alias, Jahr, Sperrklinke —, und das
    Nachzählen läuft mit durch. Nur Ordner und Datei entstehen nicht.
    """
    bexio = PdfBexio([RECHNUNG], {77: []})
    db, graph = aufbau(bexio)
    await _bis_versandbestaetigung(client_as_owner, bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08"}
    )

    ergebnis = antwort.json()
    assert ergebnis["trocken"] is True
    assert graph.uploads == [] and graph.ordner == set()

    zeile = ergebnis["protokoll"][0]
    assert zeile["ordner"] == "Finanzen/Debitoren/Swiss Bankers/2026/OnboardingQS"
    assert zeile["dateien"][0]["pfad"] == (
        "Finanzen/Debitoren/Swiss Bankers/2026/OnboardingQS/"
        "RE-00703 InnoSmith Rechnung Aug 2026.pdf"
    )
    # Kein Phantomfehler: das Nachzählen findet den Schatten und ist zufrieden.
    assert all(not d["hindernis"] for d in zeile["dateien"])
    assert db.lauf.rechnungen[0].abgelegt_am is None


@pytest.mark.asyncio
async def test_ohne_lauf_gibt_es_nichts_abzulegen(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.status_code == 404


@pytest.mark.asyncio
async def test_ohne_graph_keine_ablage(client_as_owner, aufbau, monkeypatch):
    from app.services import graph as graph_modul

    bexio = PdfBexio([RECHNUNG], {77: []})
    aufbau(bexio)
    monkeypatch.setattr(graph_modul, "get_graph_client", lambda: None)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.status_code == 400
    assert "OneDrive" in antwort.json()["detail"]


@pytest.mark.asyncio
async def test_nur_der_eigentuemer_darf_ablegen(client_as_member):
    antwort = await client_as_member.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )
    assert antwort.status_code == 403


# ── Der gelungene Weg ────────────────────────────────────


@pytest.mark.asyncio
async def test_nach_bestaetigung_landet_das_dokument_im_archiv(
    client_as_owner, aufbau
):
    bexio = PdfBexio([RECHNUNG], {77: []})
    db, graph = aufbau(bexio)
    await _bis_versandbestaetigung(client_as_owner, bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.status_code == 200, antwort.text
    ergebnis = antwort.json()
    assert ergebnis["abgelegt"] == 1
    zeile = ergebnis["protokoll"][0]
    assert zeile["ordner"] == ZIEL
    assert zeile["dateien"][0]["pfad"] == (
        f"{ZIEL}/RE-00703 InnoSmith Rechnung Aug 2026.pdf"
    )
    assert zeile["dateien"][0]["bytes"] > 0
    assert db.lauf.rechnungen[0].abgelegt_am is not None


@pytest.mark.asyncio
async def test_projektebene_entsteht_bei_mehreren_vertraegen(
    client_as_owner, aufbau
):
    """Swiss Bankers hat mehrere aktive Verträge — also ein Unterordner."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    _db, graph = aufbau(bexio)
    await _bis_versandbestaetigung(client_as_owner, bexio)
    await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert "Finanzen/Debitoren/Swiss Bankers/2026" in graph.ordner
    assert ZIEL in graph.ordner


@pytest.mark.asyncio
async def test_zweite_ablage_schreibt_nicht_erneut(client_as_owner, aufbau):
    """Sonst liefe bei jedem Druck ein Upload gegen eine bestehende Datei."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    _db, graph = aufbau(bexio)
    await _bis_versandbestaetigung(client_as_owner, bexio)

    await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )
    erste = list(graph.uploads)
    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.json()["abgelegt"] == 0
    assert graph.uploads == erste, "Es wurde ein zweites Mal geschrieben"
    assert "schon abgelegt" in antwort.json()["protokoll"][0]["hindernis"]


@pytest.mark.asyncio
async def test_vorhandene_datei_wird_nicht_ueberschrieben(client_as_owner, aufbau):
    """Der Laufbuch-Vermerk fehlt, die Datei ist aber da — etwa nach einem
    Abbruch. Dann gilt «lag schon da», nicht «ersetzen».
    """
    bexio = PdfBexio([RECHNUNG], {77: []})
    pfad = f"{ZIEL}/RE-00703 InnoSmith Rechnung Aug 2026.pdf"
    graph = AblageGraph({pfad: 999_999})
    aufbau(bexio, graph=graph)
    await _bis_versandbestaetigung(client_as_owner, bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.status_code == 200, antwort.text
    assert graph.uploads == []
    assert graph.dateien[pfad] == 999_999, "Die Datei wurde verändert"
    zeile = antwort.json()["protokoll"][0]
    assert zeile["dateien"][0]["lag_schon"] is True
    assert antwort.json()["abgelegt"] == 1


@pytest.mark.asyncio
async def test_leere_auswahl_legt_nichts_ab(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    _db, graph = aufbau(bexio)
    await _bis_versandbestaetigung(client_as_owner, bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ablegen",
        json={"month": "2026-08", "rechnungen": [], "echt": True},
    )

    assert antwort.json()["abgelegt"] == 0
    assert graph.uploads == []


@pytest.mark.asyncio
async def test_ablegen_versendet_nichts(client_as_owner, aufbau):
    """Weder Graph ``send_draft`` noch Bexio ``send`` — die Ablage ist stumm."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    _db, graph = aufbau(bexio)
    await _bis_versandbestaetigung(client_as_owner, bexio)
    await client_as_owner.post(
        "/api/debtors/pruefung/ablegen", json={"month": "2026-08", "echt": True}
    )

    assert graph.gesendet == []
    assert bexio.gesendet == []
    assert bexio.ausgestellt == [], "Ablegen darf nicht ausstellen"

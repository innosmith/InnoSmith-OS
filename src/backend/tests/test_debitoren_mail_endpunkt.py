"""Tests für Mailentwurf, Versandbestätigung und Ausstellen.

Geprüft wird vor allem, was die Endpunkte **verweigern**: ein zweiter Entwurf
im Postfach, ein Ausstellen ohne Versandbestätigung, und jedes Versenden —
egal ob über Graph oder über Bexio ``send``.
"""

from __future__ import annotations

import pytest
from fpdf import FPDF

from test_debitoren_anwenden import FakeDb, RECHNUNG
from test_debitoren_erzeugen_endpunkt import FakeToggl7
from test_debitoren_lauf import FakeBexio


def _pdf() -> bytes:
    doc = FPDF()
    doc.add_page()
    doc.set_font("Helvetica", size=12)
    doc.cell(0, 10, "Rechnung RE-00703")
    doc.add_page()
    doc.set_font("Helvetica", size=12)
    doc.cell(0, 10, "Einzahlungsschein CH60 3080 8001 2345 6789 0")
    return bytes(doc.output())


class PdfBexio(FakeBexio):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ausgestellt: list[int] = []
        self.gesendet: list[int] = []

    async def get_invoice_pdf(self, invoice_id):
        return _pdf()

    async def get_invoice(self, invoice_id):
        return {"id": invoice_id, "kb_item_status_id": 7}

    async def issue_invoice(self, invoice_id):
        self.ausgestellt.append(invoice_id)
        return True

    async def send_invoice(self, invoice_id):
        """Darf von keinem Endpunkt aufgerufen werden."""
        self.gesendet.append(invoice_id)


class FakeGraph:
    def __init__(self):
        self.entwuerfe: list[dict] = []
        self.anhaenge: list[tuple] = []
        self.gesendet: list[str] = []
        self._n = 0

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


#: Was eine Laufbuchzeile trägt. Für das Zurückrollen der Nachbildung.
_ZEILENFELDER = (
    "zurueckgestellt", "grund", "dokumente_erzeugt_am",
    "mailentwurf_id", "versendet_am", "abgelegt_am",
)


class LaufbuchDb(FakeDb):
    """Hält den angelegten Lauf im Speicher, damit der zweite Aufruf ihn sieht.

    Ohne das entstünde bei jedem Druck ein neuer Lauf und die Idempotenz
    der Mailentwürfe wäre nicht prüfbar.

    **Zurückrollen ist hier nicht Zierde.** Der Trockenlauf durchläuft die
    Laufbuch-Vermerke und rollt am Ende zurück; eine Nachbildung ohne
    Zurückrollen liesse den Vermerk stehen, und der darauf folgende echte Lauf
    hielte die Mail für längst angelegt. Genau dieser Fehler wäre im Betrieb
    ein Monat ohne Rechnungen.
    """

    def __init__(self):
        super().__init__()
        self.lauf = None
        self._sicher: dict[int, dict] | None = None

    def _stand(self) -> dict[int, dict] | None:
        if self.lauf is None:
            return None
        return {
            z.rechnung_id: {f: getattr(z, f, None) for f in _ZEILENFELDER}
            for z in self.lauf.rechnungen
        }

    async def commit(self):
        await super().commit()
        self._sicher = self._stand()

    async def rollback(self):
        await super().rollback()
        if self._sicher is None:
            self.lauf = None
            return
        behalten = []
        for zeile in self.lauf.rechnungen:
            alt = self._sicher.get(zeile.rechnung_id)
            if alt is None:
                continue  # erst in dieser Transaktion entstanden
            for feld, wert in alt.items():
                setattr(zeile, feld, wert)
            behalten.append(zeile)
        self.lauf.rechnungen = behalten

    async def execute(self, *_a, **_k):
        hier = self

        class Ergebnis:
            def scalars(self):
                return self

            def first(self):
                return hier.lauf

        return Ergebnis()

    def add(self, obj):
        super().add(obj)
        from app.models import Debitorenlauf, DebitorenlaufRechnung

        if isinstance(obj, Debitorenlauf):
            if getattr(obj, "rechnungen", None) is None:
                obj.rechnungen = []
            self.lauf = obj
        elif isinstance(obj, DebitorenlaufRechnung):
            if obj.zurueckgestellt is None:
                obj.zurueckgestellt = False
            if self.lauf is not None and obj not in self.lauf.rechnungen:
                self.lauf.rechnungen.append(obj)


@pytest.fixture
def aufbau(monkeypatch):
    from app.database import get_db
    from app.main import app
    from app.services import fachsysteme, graph as graph_modul

    db = LaufbuchDb()
    graph = FakeGraph()

    def aufsetzen(bexio, toggl=None):
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


@pytest.mark.asyncio
async def test_mailentwurf_entsteht_mit_anhang(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    db, graph = aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )

    assert antwort.status_code == 200, antwort.text
    ergebnis = antwort.json()
    assert ergebnis["angelegt"] == 1
    assert graph.entwuerfe[0]["to_recipients"]
    assert graph.anhaenge
    assert graph.gesendet == []
    assert db.commits == 1
    assert db.lauf.rechnungen[0].mailentwurf_id == "AAMk-1"


@pytest.mark.asyncio
async def test_ohne_echt_bleibt_das_postfach_leer(client_as_owner, aufbau):
    """Das Dokument entsteht wirklich, die Mail nicht.

    Damit zeigt der Trockenlauf das Teure zuerst: ob Rechnung und
    Leistungsrapport sich überhaupt zusammenbauen lassen und wie viele Seiten
    dabei herauskommen. Genau daran scheitert es, wenn es scheitert.
    """
    bexio = PdfBexio([RECHNUNG], {77: []})
    db, graph = aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe", json={"month": "2026-08"}
    )

    ergebnis = antwort.json()
    assert ergebnis["trocken"] is True
    assert graph.entwuerfe == [] and graph.anhaenge == [] and graph.gesendet == []
    assert db.commits == 0

    zeile = ergebnis["protokoll"][0]
    assert zeile["seiten"] > 0, "das Dokument wird echt gebaut"
    assert any("create_draft" in v for v in ergebnis["vermerke"])


@pytest.mark.asyncio
async def test_trockenlauf_vermerkt_nichts_im_laufbuch(client_as_owner, aufbau):
    """Sonst hielte der zweite, echte Aufruf die Mail für schon angelegt."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    db, graph = aufbau(bexio)

    await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe", json={"month": "2026-08"}
    )
    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )

    assert antwort.json()["angelegt"] == 1
    assert len(graph.entwuerfe) == 1
    assert db.lauf.rechnungen[0].mailentwurf_id == "AAMk-1"


@pytest.mark.asyncio
async def test_zweiter_aufruf_legt_keinen_zweiten_entwurf_an(client_as_owner, aufbau):
    """Sonst läge bei jedem Druck eine weitere Mail im Postfach."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    _db, graph = aufbau(bexio)

    await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )
    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )

    assert antwort.json()["angelegt"] == 0
    assert len(graph.entwuerfe) == 1


@pytest.mark.asyncio
async def test_leere_auswahl_erzeugt_nichts(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    _db, graph = aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "rechnungen": [], "echt": True},
    )

    assert antwort.json()["angelegt"] == 0
    assert graph.entwuerfe == []


@pytest.mark.asyncio
async def test_ausstellen_ohne_versandbestaetigung_wird_verweigert(
    client_as_owner, aufbau
):
    """Sonst stünde eine Forderung in den Büchern, die niemand gesehen hat."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ausstellen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.status_code == 200
    ergebnis = antwort.json()
    assert ergebnis["ausgestellt"] == 0
    assert bexio.ausgestellt == []
    assert "nicht bestätigt" in ergebnis["protokoll"][0]["hindernis"]


@pytest.mark.asyncio
async def test_ausstellen_nach_bestaetigung(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    db, _graph = aufbau(bexio)

    await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )
    bestaetigt = await client_as_owner.post(
        "/api/debtors/pruefung/versendet",
        json={"month": "2026-08", "rechnung_id": 77},
    )
    assert bestaetigt.status_code == 200, bestaetigt.text
    assert bestaetigt.json()["versendet_am"]

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/ausstellen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.json()["ausgestellt"] == 1
    assert bexio.ausgestellt == [77]
    assert bexio.gesendet == []
    assert db.commits >= 3


@pytest.mark.asyncio
async def test_versendet_ohne_mailentwurf_wird_abgewiesen(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/versendet",
        json={"month": "2026-08", "rechnung_id": 77},
    )

    assert antwort.status_code == 404


@pytest.mark.asyncio
async def test_dokument_vorschau_ist_ein_pdf(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    aufbau(bexio)

    antwort = await client_as_owner.get(
        "/api/debtors/pruefung/dokument/77?month=2026-08"
    )

    assert antwort.status_code == 200, antwort.text
    assert antwort.headers["content-type"].startswith("application/pdf")
    assert antwort.content.startswith(b"%PDF")


@pytest.mark.asyncio
async def test_nur_der_eigentuemer_darf_mails_anlegen(client_as_member):
    antwort = await client_as_member.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )
    assert antwort.status_code == 403


@pytest.mark.asyncio
async def test_ohne_graph_kein_entwurf(client_as_owner, aufbau, monkeypatch):
    from app.services import graph as graph_modul

    bexio = PdfBexio([RECHNUNG], {77: []})
    aufbau(bexio)
    monkeypatch.setattr(graph_modul, "get_graph_client", lambda: None)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )

    assert antwort.status_code == 400
    assert "Graph" in antwort.json()["detail"]

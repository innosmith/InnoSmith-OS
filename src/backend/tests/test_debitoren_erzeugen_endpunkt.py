"""Tests für den Endpunkt, der Entwürfe in Bexio anlegt.

Geprüft wird vor allem, was der Endpunkt **verweigert**: Aufträge, die der
Browser nennt, und Aufträge, für die es im Monat längst eine Rechnung gibt.
Beides erzeugte sonst eine zweite Rechnung an dieselbe Kundschaft.
"""

from __future__ import annotations

import pytest

from test_debitoren_anwenden import FakeDb
from test_debitoren_lauf import FakeBexio, FakeToggl

AUFTRAG_ROH = {
    "id": 31, "document_nr": "AU-00031", "title": "OnboardingQS",
    "contact_id": 5, "is_valid_from": "2026-01-01",
    "kb_item_status_id": 5, "is_recurring": True, "total": "2500.00",
}

RECHNUNG = {
    "id": 77, "document_nr": "RE-00703", "is_valid_from": "2026-08-31",
    "contact_id": 5, "title": "OnboardingQS",
    "total_net": "10000.00", "total_taxes": "810.00", "total": "10810.00",
}

NEUE_RECHNUNG = {
    "id": 900, "document_nr": "RE-00800",
    "is_valid_from": "2026-09-02", "is_valid_to": "2026-10-01",
}


class ErzeugenderBexio(FakeBexio):
    """Merkt sich, welche Aufträge verrechnet und welche Daten gesetzt wurden."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.erzeugt_fuer: list[int] = []
        self.datiert: list[tuple[int, dict]] = []

    async def create_invoice_from_order(self, order_id, positionen=None):
        self.erzeugt_fuer.append(order_id)
        return dict(NEUE_RECHNUNG)

    async def update_invoice(self, invoice_id, **felder):
        self.datiert.append((invoice_id, felder))
        return {**NEUE_RECHNUNG, **felder}


class FakeToggl7(FakeToggl):
    async def list_projects(self, active=None):
        return [{"id": 7, "name": "OnboardingQS", "client_id": 3, "active": True}]

    async def list_clients(self, status=None):
        return [{"id": 3, "name": "T+R AG"}]


@pytest.fixture
def aufbau(monkeypatch):
    from app.database import get_db
    from app.main import app
    from app.services import fachsysteme

    db = FakeDb()

    def aufsetzen(bexio, toggl=None):
        monkeypatch.setattr(fachsysteme, "bexio_zugang", lambda user: bexio)
        monkeypatch.setattr(
            fachsysteme, "toggl_zugang", lambda user: (toggl or FakeToggl7([]), 1)
        )
        app.dependency_overrides[get_db] = lambda: db
        return db

    yield aufsetzen
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_faelliger_auftrag_wird_auf_den_monatsletzten_datiert(
    client_as_owner, aufbau
):
    """Der eigentliche Zweck: Bexio datiert auf heute, richtig ist der 30.09."""
    bexio = ErzeugenderBexio([], {}, auftraege=[AUFTRAG_ROH])
    db = aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/erzeugen", json={"month": "2026-09", "echt": True}
    )

    assert antwort.status_code == 200
    ergebnis = antwort.json()
    assert ergebnis["stichtag"] == "2026-09-30"
    assert (ergebnis["erzeugt"], ergebnis["gescheitert"]) == (1, 0)
    assert bexio.erzeugt_fuer == [31]

    _, felder = bexio.datiert[0]
    assert felder["is_valid_from"] == "2026-09-30"
    assert felder["is_valid_to"] == "2026-10-29"  # Frist von Bexio, 29 Tage
    assert db.commits == 1


@pytest.mark.asyncio
async def test_ohne_echt_entsteht_keine_rechnung(client_as_owner, aufbau):
    """Der Stichtag wird echt gerechnet, die Rechnung nicht angelegt.

    Das Datum ist der Grund für diesen Endpunkt — also muss gerade es der
    Trockenlauf zeigen, nicht überspringen.
    """
    bexio = ErzeugenderBexio([], {}, auftraege=[AUFTRAG_ROH])
    db = aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/erzeugen", json={"month": "2026-09"}
    )

    ergebnis = antwort.json()
    assert ergebnis["trocken"] is True
    assert ergebnis["stichtag"] == "2026-09-30"
    assert ergebnis["protokoll"][0]["datum"] == "2026-09-30"
    assert bexio.erzeugt_fuer == []
    assert bexio.datiert == []
    assert db.commits == 0
    assert any("create_invoice_from_order(31)" in v for v in ergebnis["vermerke"])


@pytest.mark.asyncio
async def test_kein_lauf_entsteht_im_trockenlauf(client_as_owner, aufbau):
    """Ein Trockenlauf hinterlässt auch im Laufbuch keine Spur."""
    bexio = ErzeugenderBexio([], {}, auftraege=[AUFTRAG_ROH])
    db = aufbau(bexio)

    await client_as_owner.post(
        "/api/debtors/pruefung/erzeugen", json={"month": "2026-09"}
    )

    assert db.rollbacks == 1
    assert db.eintraege == []


@pytest.mark.asyncio
async def test_wo_der_monat_schon_eine_rechnung_traegt_entsteht_keine_zweite(
    client_as_owner, aufbau
):
    bexio = ErzeugenderBexio([RECHNUNG], {77: []}, auftraege=[AUFTRAG_ROH])
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/erzeugen", json={"month": "2026-08", "echt": True}
    )

    assert antwort.status_code == 200
    assert antwort.json()["erzeugt"] == 0
    assert bexio.erzeugt_fuer == []


@pytest.mark.asyncio
async def test_der_browser_kann_keinen_auftrag_benennen(client_as_owner, aufbau):
    """Genannt werden Verträge, nicht Auftragskennungen.

    Nähme der Endpunkt Kennungen entgegen, liesse sich aus jedem beliebigen
    Auftrag auf Zuruf eine Rechnung erzeugen.
    """
    bexio = ErzeugenderBexio([], {}, auftraege=[AUFTRAG_ROH])
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/erzeugen",
        json={
            "month": "2026-09", "vertraege": ["gibt-es-nicht"],
            "auftrag_ids": [31], "echt": True,
        },
    )

    assert antwort.status_code == 200
    ergebnis = antwort.json()
    assert ergebnis["erzeugt"] == 0
    assert ergebnis["uebergangen"] == ["gibt-es-nicht"]
    assert bexio.erzeugt_fuer == []


@pytest.mark.asyncio
async def test_auswahl_beschraenkt_auf_die_genannten_vertraege(client_as_owner, aufbau):
    """Ohne Angabe beide, mit Angabe einer.

    Beide Hälften stehen hier bewusst nebeneinander: liesse man die erste weg,
    bestünde der Test auch dann, wenn der zweite Auftrag aus einem ganz anderen
    Grund nie verrechnet würde.
    """
    zweiter = {**AUFTRAG_ROH, "id": 36, "document_nr": "AU-00036", "title": "COflow"}

    ohne = ErzeugenderBexio([], {}, auftraege=[AUFTRAG_ROH, zweiter])
    aufbau(ohne)
    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/erzeugen", json={"month": "2026-09", "echt": True}
    )
    assert antwort.status_code == 200
    assert sorted(ohne.erzeugt_fuer) == [31, 36]

    mit = ErzeugenderBexio([], {}, auftraege=[AUFTRAG_ROH, zweiter])
    aufbau(mit)
    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/erzeugen",
        json={"month": "2026-09", "vertraege": ["onboardingqs"], "echt": True},
    )
    assert antwort.status_code == 200
    assert mit.erzeugt_fuer == [31]


@pytest.mark.asyncio
async def test_nur_der_eigentuemer_darf_erzeugen(client_as_member):
    antwort = await client_as_member.post(
        "/api/debtors/pruefung/erzeugen", json={"month": "2026-09", "echt": True}
    )
    assert antwort.status_code == 403

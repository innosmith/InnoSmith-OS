"""Tests für den einzigen schreibenden Weg des Rechnungslaufs.

Der Endpunkt ändert Positionen auf Rechnungen, die zu Kunden gehen. Geprüft
wird deshalb weniger, dass er schreibt, als **was** er schreibt und was er
verweigert: keine Zahl aus dem Browser, keine Zeile ohne Vorschlag, kein
Abbruch in der Mitte.
"""

from __future__ import annotations

from datetime import date

import pytest

from test_debitoren_lauf import FakeBexio, FakeToggl

RECHNUNG = {
    "id": 77, "document_nr": "RE-00703", "is_valid_from": "2026-08-31",
    "contact_id": 5, "title": "OnboardingQS",
    "total_net": "10000.00", "total_taxes": "810.00", "total": "10810.00",
}

STUNDENZEILE = {
    "id": 401, "positionsart": "custom", "pos": 1, "unit_id": 2,
    "amount": "19.000000", "unit_price": "250.000000", "tax_id": 36,
    "text": "Onboarding QS – Effektiver Aufwand",
}

GRUPPE = {
    "project_id": 7, "description": "Umsetzung", "billable": True,
    "time_entries": [{"id": 1, "start": "2026-08-12T09:00:00+02:00", "seconds": 126900}],
}


class SchreibenderBexio(FakeBexio):
    """Wie ``FakeBexio``, merkt sich aber jeden Schreibzugriff."""

    def __init__(self, *args, scheitert_bei: set[int] | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.geschrieben: list[tuple] = []
        self.geloescht: list[tuple] = []
        self._scheitert_bei = scheitert_bei or set()

    async def update_invoice_position(self, invoice_id, position_id, positionsart, daten):
        if position_id in self._scheitert_bei:
            raise RuntimeError("422 Unprocessable")
        self.geschrieben.append((invoice_id, position_id, positionsart, daten))
        return {"id": position_id}

    async def delete_invoice_position(self, invoice_id, position_id, positionsart):
        self.geloescht.append((invoice_id, position_id, positionsart))
        return True


class FakeToggl7(FakeToggl):
    """Das Projekt heisst wie der Vertrag im echten Bestand."""

    async def list_projects(self, active=None):
        return [{"id": 7, "name": "OnboardingQS", "client_id": 3, "active": True}]

    async def list_clients(self, status=None):
        return [{"id": 3, "name": "T+R AG"}]


class LeeresErgebnis:
    def scalars(self):
        return self

    def first(self):
        return None


class FakeDb:
    """Datenbank-Ersatz ohne Bestand.

    ``execute`` liefert bewusst nichts: in diesen Tests gibt es kein Laufbuch,
    also ist «keine Zeile vermerkt» die richtige Antwort und nicht eine Lücke
    in der Nachbildung.
    """

    def __init__(self):
        self.eintraege: list = []
        self.commits = 0
        self.rollbacks = 0

    def add(self, eintrag):
        self.eintraege.append(eintrag)

    async def execute(self, *_args, **_kwargs):
        return LeeresErgebnis()

    async def flush(self):
        pass

    async def refresh(self, *_args, **_kwargs):
        pass

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        self.rollbacks += 1
        self.eintraege.clear()


@pytest.fixture
def aufbau(monkeypatch):
    """Bexio und Toggl unterschieben, Datenbank ersetzen."""
    from app.database import get_db
    from app.main import app
    from app.services import fachsysteme

    db = FakeDb()

    def aufsetzen(bexio, toggl=None):
        monkeypatch.setattr(fachsysteme, "bexio_zugang", lambda user: bexio)
        monkeypatch.setattr(
            fachsysteme, "toggl_zugang", lambda user: (toggl or FakeToggl7([GRUPPE]), 1)
        )
        app.dependency_overrides[get_db] = lambda: db
        return db

    yield aufsetzen
    app.dependency_overrides.pop(get_db, None)


@pytest.mark.asyncio
async def test_die_stundenzahl_kommt_aus_toggl_nicht_aus_dem_aufruf(client_as_owner, aufbau):
    """Der Kern der Absicherung.

    Der Browser schickt nur, welche Rechnung gemeint ist. Käme die Menge mit,
    liesse sich über diesen Endpunkt jede Zahl auf jede Rechnung schreiben.
    """
    bexio = SchreibenderBexio([RECHNUNG], {77: [STUNDENZEILE]})
    db = aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/anwenden",
        json={
            "month": "2026-08", "rechnungen": ["RE-00703"],
            "amount": "999", "echt": True,
        },
    )

    assert antwort.status_code == 200
    ergebnis = antwort.json()
    assert (ergebnis["geschrieben"], ergebnis["fehlgeschlagen"]) == (1, 0)

    rechnung_id, position_id, art, daten = bexio.geschrieben[0]
    assert (rechnung_id, position_id, art) == (77, 401, "custom")
    assert daten["amount"] == "35.25"  # 126900 Sekunden
    assert daten["unit_price"] == "250.000000"  # unverändert
    assert db.commits == 1


@pytest.mark.asyncio
async def test_ohne_echt_wird_gerechnet_aber_nicht_geschrieben(client_as_owner, aufbau):
    """Der Vorgabewert ist der Trockenlauf — ein vergessenes Feld schreibt nichts.

    Gerechnet wird trotzdem vollständig: die Stunden aus Toggl, die Position
    aus Bexio, der fertige Wert. Nur der letzte Aufruf wird angehalten. Ein
    Trockenlauf, der schon vorher abbräche, prüfte gerade das nicht, worauf es
    ankommt.
    """
    bexio = SchreibenderBexio([RECHNUNG], {77: [STUNDENZEILE]})
    db = aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/anwenden",
        json={"month": "2026-08", "rechnungen": ["RE-00703"]},
    )

    ergebnis = antwort.json()
    assert ergebnis["trocken"] is True
    assert bexio.geschrieben == []
    assert db.commits == 0

    # Der gerechnete Wert steht im Protokoll, obwohl er nicht geschrieben wurde.
    assert ergebnis["protokoll"][0]["neu"]
    assert "35.25" in ergebnis["vermerke"][0]


@pytest.mark.asyncio
async def test_ohne_auswahl_bleibt_alles_unberuehrt(client_as_owner, aufbau):
    """Eine leere Liste ist eine Auswahl, keine fehlende Angabe."""
    bexio = SchreibenderBexio([RECHNUNG], {77: [STUNDENZEILE]})
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/anwenden",
        json={"month": "2026-08", "rechnungen": [], "echt": True},
    )

    assert antwort.json()["geschrieben"] == 0
    assert bexio.geschrieben == []


@pytest.mark.asyncio
async def test_eine_gescheiterte_zeile_reisst_den_lauf_nicht(client_as_owner, aufbau):
    """Ein Abbruch in der Mitte hinterliesse eine halb angepasste Rechnung."""
    bexio = SchreibenderBexio(
        [RECHNUNG], {77: [STUNDENZEILE]}, scheitert_bei={401}
    )
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/anwenden", json={"month": "2026-08", "echt": True}
    )

    ergebnis = antwort.json()
    assert (ergebnis["geschrieben"], ergebnis["fehlgeschlagen"]) == (0, 1)
    assert "422" in ergebnis["protokoll"][0]["meldung"]


@pytest.mark.asyncio
async def test_stimmende_rechnung_wird_nicht_angefasst(client_as_owner, aufbau):
    """Ohne Vorschlag kein Schreibzugriff — sonst schriebe jeder Lauf."""
    passend = {**STUNDENZEILE, "amount": "35.250000"}
    bexio = SchreibenderBexio([RECHNUNG], {77: [passend]})
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/anwenden", json={"month": "2026-08", "echt": True}
    )

    assert antwort.json()["geschrieben"] == 0
    assert bexio.geschrieben == []


@pytest.mark.asyncio
async def test_unbekannte_rechnung_wird_als_uebergangen_gemeldet(client_as_owner, aufbau):
    """Stillschweigend nichts zu tun sähe aus wie «erledigt»."""
    bexio = SchreibenderBexio([RECHNUNG], {77: [STUNDENZEILE]})
    aufbau(bexio)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/anwenden",
        json={"month": "2026-08", "rechnungen": ["RE-99999"], "echt": True},
    )

    assert antwort.json()["uebergangen"] == ["RE-99999"]


@pytest.mark.asyncio
async def test_member_darf_nicht_schreiben(client_as_member):
    antwort = await client_as_member.post(
        "/api/debtors/pruefung/anwenden", json={"month": "2026-08", "echt": True}
    )
    assert antwort.status_code == 403


@pytest.mark.db
@pytest.mark.asyncio
async def test_zurueckgestellte_rechnung_bleibt_unberuehrt(
    client_as_owner, monkeypatch
):
    """«Zurückgestellt» heisst «vorerst nicht anfassen», nicht «später dran».

    Ohne diese Sperre nähme der Sammelknopf die Rechnung beim nächsten Druck
    wieder mit — und die Entscheidung, sie liegen zu lassen, wäre folgenlos.

    Läuft gegen die echte Datenbank, weil genau das Zusammenspiel aus Laufbuch
    und Schreibpfad geprüft wird.
    """
    from sqlalchemy import delete

    from app.database import async_session
    from app.models import Debitorenlauf
    from app.services import debitoren_laufbuch as lbuch
    from app.services import fachsysteme

    bexio = SchreibenderBexio([RECHNUNG], {77: [STUNDENZEILE]})
    monkeypatch.setattr(fachsysteme, "bexio_zugang", lambda user: bexio)
    monkeypatch.setattr(
        fachsysteme, "toggl_zugang", lambda user: (FakeToggl7([GRUPPE]), 1)
    )

    async with async_session() as db:
        await db.execute(delete(Debitorenlauf).where(Debitorenlauf.jahr == 2026, Debitorenlauf.monat == 8))
        lauf = await lbuch.lauf_oeffnen(
            db, jahr=2026, monat=8, stichtag=date(2026, 8, 31)
        )
        await lbuch.zuruecklegen(db, lauf, rechnung_id=77, grund="Kunde klärt noch")
        await db.commit()

    try:
        antwort = await client_as_owner.post(
            "/api/debtors/pruefung/anwenden",
            json={"month": "2026-08", "rechnungen": ["RE-00703"], "echt": True},
        )

        assert antwort.status_code == 200
        ergebnis = antwort.json()
        assert bexio.geschrieben == []
        assert ergebnis["geschrieben"] == 0
        # Nicht als «übergangen» und nicht als Fehlschlag: das wäre beides eine
        # Fehlermeldung für etwas, das genau so gewollt ist.
        assert ergebnis["zurueckgestellt"] == ["RE-00703"]
        assert ergebnis["uebergangen"] == []
        assert ergebnis["fehlgeschlagen"] == 0
    finally:
        async with async_session() as db:
            await db.execute(delete(Debitorenlauf).where(Debitorenlauf.jahr == 2026, Debitorenlauf.monat == 8))
            await db.commit()

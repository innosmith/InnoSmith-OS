"""Tests für den Endpunkt, der die Verrechnungsart nachträgt.

Geprüft wird vor allem die **Schranke**: Toggl ist im Lauf die
Beweisgrundlage, und bis der Versand bestätigt ist, wird sie nicht angefasst.

Der Vertrag hinter ``RECHNUNG`` ist «OnboardingQS» bei Swiss Bankers. Er trägt
keine eigene Verrechnungsart und erbt darum die Vorgabe «Kunde verrechnet» —
womit der Test zugleich die Erbfolge prüft.
"""

from __future__ import annotations

import pytest

from test_debitoren_anwenden import GRUPPE, RECHNUNG
from test_debitoren_lauf import FakeToggl
from test_debitoren_mail_endpunkt import FakeGraph, LaufbuchDb, PdfBexio

VIER_TAGS = [
    {"id": 1, "name": "Kunde verrechnet"},
    {"id": 2, "name": "Fixpreis"},
    {"id": 3, "name": "Extern rapportiert"},
    {"id": 4, "name": "keine Verrechnung"},
]

INTERNE_GRUPPE = {
    "project_id": 9, "description": "Netzwerkpflege", "billable": False,
    "time_entries": [{"id": 2, "start": "2026-08-14T09:00:00+02:00", "seconds": 3600}],
}


class TaggenderToggl(FakeToggl):
    """Kennt die vier echten Tags und merkt sich jeden Schreibzugriff."""

    def __init__(self, gruppen=None, *, vorhandene: dict[int, list[str]] | None = None):
        super().__init__(gruppen or [GRUPPE])
        self.gesetzt: list[tuple[int, int]] = []
        self._vorhandene = vorhandene or {}

    async def list_projects(self, active=None):
        return [
            {"id": 7, "name": "OnboardingQS", "client_id": 3, "active": True},
            {"id": 9, "name": "Sales & Networking", "client_id": None, "active": True},
        ]

    async def list_clients(self, status=None):
        return [{"id": 3, "name": "Swiss Bankers"}]

    async def list_tags(self, ws=None):
        return VIER_TAGS

    async def add_time_entry_tag(self, eintrag_id, tag_id, workspace=None):
        self.gesetzt.append((eintrag_id, tag_id))
        name = next(t["name"] for t in VIER_TAGS if t["id"] == tag_id)
        return {
            "id": eintrag_id,
            "tags": [*self._vorhandene.get(eintrag_id, []), name],
            "description": GRUPPE["description"],
            "duration": 3600,
        }


@pytest.fixture
def aufbau(monkeypatch):
    from app.database import get_db
    from app.main import app
    from app.services import fachsysteme, graph as graph_modul

    db = LaufbuchDb()

    def aufsetzen(bexio, toggl):
        monkeypatch.setattr(fachsysteme, "bexio_zugang", lambda user: bexio)
        monkeypatch.setattr(fachsysteme, "toggl_zugang", lambda user: (toggl, 1))
        monkeypatch.setattr(graph_modul, "get_graph_client", lambda: FakeGraph())
        app.dependency_overrides[get_db] = lambda: db
        return db

    yield aufsetzen
    app.dependency_overrides.pop(get_db, None)


async def _lauf_oeffnen(client):
    """Der Mailentwurf eröffnet den Lauf — ohne ihn gibt es keine Zeile."""
    antwort = await client.post(
        "/api/debtors/pruefung/mailentwuerfe",
        json={"month": "2026-08", "echt": True},
    )
    assert antwort.status_code == 200, antwort.text


async def _versand_bestaetigen(client):
    await _lauf_oeffnen(client)
    antwort = await client.post(
        "/api/debtors/pruefung/versendet",
        json={"month": "2026-08", "rechnung_id": 77},
    )
    assert antwort.status_code == 200, antwort.text


# ── Die Schranke ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_ohne_versandbestaetigung_bleibt_toggl_unangetastet(
    client_as_owner, aufbau
):
    """Der ganze Zweck der Reihenfolge: bis zum Versand ist Toggl der Beweis.

    Geht im Lauf etwas schief, muss auf unveränderte Zeitdaten
    zurückgegriffen werden können. Wer vorher taggt, hat die Beweisgrundlage
    angefasst, bevor er sie gebraucht hat.
    """
    bexio = PdfBexio([RECHNUNG], {77: []})
    toggl = TaggenderToggl()
    aufbau(bexio, toggl)
    await _lauf_oeffnen(client_as_owner)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "echt": True},
    )

    assert antwort.status_code == 200, antwort.text
    ergebnis = antwort.json()
    assert toggl.gesetzt == []
    assert ergebnis["gesetzt"] == 0
    assert any("nicht bestätigt" in u for u in ergebnis["uebergangen"])


@pytest.mark.asyncio
async def test_nach_bestaetigung_wird_die_art_gesetzt(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    toggl = TaggenderToggl()
    aufbau(bexio, toggl)
    await _versand_bestaetigen(client_as_owner)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "echt": True},
    )

    ergebnis = antwort.json()
    assert toggl.gesetzt == [(1, 1)], "Eintrag 1 bekommt «Kunde verrechnet»"
    assert ergebnis["gesetzt"] == 1
    assert ergebnis["protokoll"][0]["art"] == "Kunde verrechnet"
    assert ergebnis["protokoll"][0]["grund"] == "Vertrag onboardingqs"


@pytest.mark.asyncio
async def test_zweiter_aufruf_setzt_nichts_erneut(client_as_owner, aufbau):
    """Wer schon die richtige Art trägt, wird nicht angefasst."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    gruppe = {**GRUPPE, "tag_ids": [1]}
    toggl = TaggenderToggl([gruppe])
    aufbau(bexio, toggl)
    await _versand_bestaetigen(client_as_owner)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "echt": True},
    )

    ergebnis = antwort.json()
    assert toggl.gesetzt == []
    assert (ergebnis["gesetzt"], ergebnis["schon_richtig"]) == (0, 1)


@pytest.mark.asyncio
async def test_fremde_art_wird_gemeldet_nicht_ueberschrieben(client_as_owner, aufbau):
    """«keine Verrechnung» an einer verrechneten Stunde ist eine Entscheidung.

    Sie zu ersetzen hiesse, ein menschliches Urteil zu überstimmen. Sie steht
    darum unter ``fremd``, mit beiden Werten.
    """
    bexio = PdfBexio([RECHNUNG], {77: []})
    toggl = TaggenderToggl([{**GRUPPE, "tag_ids": [4]}])
    aufbau(bexio, toggl)
    await _versand_bestaetigen(client_as_owner)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "echt": True},
    )

    ergebnis = antwort.json()
    assert toggl.gesetzt == []
    assert ergebnis["fremd"][0]["vorhanden"] == ["keine Verrechnung"]
    assert ergebnis["fremd"][0]["art"] == "Kunde verrechnet"


# ── Die eigenen Projekte ─────────────────────────────────


@pytest.mark.asyncio
async def test_eigene_projekte_erst_wenn_der_monat_durch_ist(client_as_owner, aufbau):
    """«keine Verrechnung» ist erst eine Tatsache, wenn nichts mehr offen ist."""
    bexio = PdfBexio([RECHNUNG], {77: []})
    toggl = TaggenderToggl([GRUPPE, INTERNE_GRUPPE])
    aufbau(bexio, toggl)
    # Eine Laufbuchzeile entsteht, aber der Versand wird nicht bestätigt.
    await _lauf_oeffnen(client_as_owner)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "intern": True, "echt": True},
    )

    ergebnis = antwort.json()
    assert toggl.gesetzt == []
    assert "offene Rechnungen" in ergebnis["hindernis"]


@pytest.mark.asyncio
async def test_eigene_projekte_nach_abschluss(client_as_owner, aufbau):
    bexio = PdfBexio([RECHNUNG], {77: []})
    toggl = TaggenderToggl([GRUPPE, INTERNE_GRUPPE])
    aufbau(bexio, toggl)
    await _versand_bestaetigen(client_as_owner)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "intern": True, "echt": True},
    )

    ergebnis = antwort.json()
    assert (2, 4) in toggl.gesetzt, "das interne Projekt bekommt «keine Verrechnung»"
    assert ergebnis["hindernis"] == ""
    arten = {z["art"] for z in ergebnis["protokoll"]}
    assert arten == {"Kunde verrechnet", "keine Verrechnung"}


# ── Der Trockenlauf ──────────────────────────────────────


@pytest.mark.asyncio
async def test_ohne_echt_wird_die_art_gezeigt_aber_nicht_geschrieben(
    client_as_owner, aufbau
):
    """Der Trockenlauf beantwortet die Frage, die hier zählt: welche Art, warum.

    Die Zuordnung entsteht echt — Vertrag, Erbfolge, vorhandene Tags. Nur der
    ``PUT`` wird angehalten, und die Nachprüfung läuft am Schatten mit durch.
    """
    bexio = PdfBexio([RECHNUNG], {77: []})
    toggl = TaggenderToggl()
    aufbau(bexio, toggl)
    await _versand_bestaetigen(client_as_owner)

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart", json={"month": "2026-08"}
    )

    ergebnis = antwort.json()
    assert ergebnis["trocken"] is True
    assert toggl.gesetzt == []
    assert ergebnis["protokoll"][0]["art"] == "Kunde verrechnet"
    assert ergebnis["vermerke"] == ["add_time_entry_tag(1, 1, 1)"]


@pytest.mark.asyncio
async def test_ohne_lauf_kein_nachtrag(client_as_owner, aufbau):
    bexio = PdfBexio([], {})
    aufbau(bexio, TaggenderToggl())

    antwort = await client_as_owner.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "echt": True},
    )

    assert antwort.status_code == 404


@pytest.mark.asyncio
async def test_member_darf_nicht(client_as_member, aufbau):
    aufbau(PdfBexio([RECHNUNG], {77: []}), TaggenderToggl())

    antwort = await client_as_member.post(
        "/api/debtors/pruefung/verrechnungsart",
        json={"month": "2026-08", "echt": True},
    )

    assert antwort.status_code in (401, 403)

"""Die Verrechnungsart in Toggl nachtragen.

Geprüft wird, was stillschweigend falsch werden könnte: ein überschriebener
Tag, ein Projekt ohne Zuordnung, ein ``PUT``, der mehr verändert als das Tag,
und ein Tagname, den Toggl nicht kennt und darum anlegen würde.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_WURZEL = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_WURZEL / "src" / "toggl"))

from app.services import debitoren_verrechnungsart as va  # noqa: E402
from app.services.debitorenvertraege import (  # noqa: E402
    VERRECHNUNGSARTEN,
    Bestand,
    Kunde,
    Vertragsdaten,
    laden,
)

TAGS = [
    {"id": 1, "name": "Kunde verrechnet"},
    {"id": 2, "name": "Fixpreis"},
    {"id": 3, "name": "Extern rapportiert"},
    {"id": 4, "name": "keine Verrechnung"},
]


def bestand_bauen() -> Bestand:
    kunde = Kunde(schluessel="mba", empfaenger="k@be.ch", ablage="MBA")
    bfh = Kunde(
        schluessel="bfh", empfaenger="s@bfh.ch", ablage="Berner Fachhochschule",
        verrechnungsart="Fixpreis",
    )
    return Bestand(
        vertraege={
            "cheetah": Vertragsdaten(
                schluessel="cheetah", bezeichnung="Cheetah", kunde=kunde,
                mwst=8.1, erkennung=("Cheetah",),
                verrechnungsart="Kunde verrechnet",
            ),
            "cas-tcm-fs26": Vertragsdaten(
                schluessel="cas-tcm-fs26", bezeichnung="CAS TCM FS26", kunde=bfh,
                mwst=0.0, erkennung=("CAS TCM",),
                verrechnungsart="Fixpreis",
            ),
        },
        kunden={"mba": kunde, "bfh": bfh},
        interne_projekte=("Sales & Networking", "Finance & Admin"),
    )


def buchung(kennung: int, projekt: str, *, tag: str = "", stunden: float = 1.0) -> dict:
    return {
        "eintrag_id": kennung,
        "datum": "2026-09-15",
        "beginn": "2026-09-15T08:00:00Z",
        "projekt": projekt,
        "projekt_id": 100,
        "beschreibung": "Arbeit",
        "stunden": stunden,
        "verrechnungsart": tag,
    }


# ── Der Plan ───────────────────────────────────────────────────────────


class TestPlan:
    def test_vertrag_bestimmt_die_art(self):
        plan = va.planen(
            [buchung(1, "Cheetah")], bestand_bauen(), vertraege={"cheetah"}
        )
        assert [v.soll for v in plan.zu_setzen] == ["Kunde verrechnet"]
        assert plan.zu_setzen[0].grund == "Vertrag cheetah"

    def test_die_art_erbt_von_der_kundschaft(self):
        """Am Vertrag steht «Fixpreis», weil die BFH es vorgibt — nicht am Projekt."""
        plan = va.planen(
            [buchung(1, "CAS TCM Dozent FS26")],
            bestand_bauen(), vertraege={"cas-tcm-fs26"},
        )
        assert [v.soll for v in plan.zu_setzen] == ["Fixpreis"]

    def test_internes_projekt_bekommt_keine_verrechnung(self):
        plan = va.planen([buchung(1, "Sales & Networking")], bestand_bauen(), intern=True)
        assert [v.soll for v in plan.zu_setzen] == ["keine Verrechnung"]
        assert plan.zu_setzen[0].grund == "internes Projekt"

    def test_internes_bleibt_liegen_ohne_ausdrueckliche_bitte(self):
        """Die eigenen Projekte hängen an keiner Rechnung — sie kommen zuletzt."""
        plan = va.planen([buchung(1, "Sales & Networking")], bestand_bauen())
        assert plan.zu_setzen == []

    def test_nicht_gewaehlter_vertrag_bleibt_liegen(self):
        plan = va.planen(
            [buchung(1, "Cheetah"), buchung(2, "CAS TCM Dozent FS26")],
            bestand_bauen(), vertraege={"cheetah"},
        )
        assert [v.eintrag_id for v in plan.zu_setzen] == [1]

    def test_ohne_auswahl_ist_der_plan_leer(self):
        """«Alles» wäre die Voreinstellung, bei der ein Fehlgriff teuer wird."""
        plan = va.planen([buchung(1, "Cheetah")], bestand_bauen())
        assert plan.zu_setzen == []

    def test_richtiger_tag_wird_nicht_erneut_gesetzt(self):
        plan = va.planen(
            [buchung(1, "Cheetah", tag="Kunde verrechnet")],
            bestand_bauen(), vertraege={"cheetah"},
        )
        assert plan.zu_setzen == []
        assert len(plan.schon_richtig) == 1

    def test_fremder_tag_wird_gemeldet_nicht_ueberschrieben(self):
        """Ein anderer Tag ist eine Menschenentscheidung, kein Fehler."""
        plan = va.planen(
            [buchung(1, "Cheetah", tag="keine Verrechnung")],
            bestand_bauen(), vertraege={"cheetah"},
        )
        assert plan.zu_setzen == []
        assert len(plan.fremd) == 1
        assert plan.fremd[0].vorhanden == ("keine Verrechnung",)
        assert plan.fremd[0].soll == "Kunde verrechnet"

    def test_projekt_ohne_zuordnung_wird_nicht_getaggt(self):
        """Weder Vertrag noch intern: ein Stammdatenmangel, keine Tatsache.

        Das ist der teure Fall. Eine Regel «kein Vertrag heisst intern» hätte
        ein neu angelegtes Kundenprojekt still auf «keine Verrechnung»
        gestempelt — verrechenbare Stunden, als unverrechenbar abgestempelt.
        """
        plan = va.planen(
            [buchung(1, "Neues Kundenprojekt")],
            bestand_bauen(), vertraege={"cheetah"}, intern=True,
        )
        assert plan.zu_setzen == []
        assert len(plan.ohne_zuordnung) == 1
        assert "nicht als intern deklariert" in plan.ohne_zuordnung[0].grund

    def test_ohne_zuordnung_faellt_auch_bei_einzelauswahl_auf(self):
        """Sonst verschwiege eine Einzelauswahl den Stammdatenmangel."""
        plan = va.planen(
            [buchung(1, "Cheetah"), buchung(2, "Unbekannt")],
            bestand_bauen(), vertraege={"cheetah"},
        )
        assert [v.eintrag_id for v in plan.zu_setzen] == [1]
        assert [v.eintrag_id for v in plan.ohne_zuordnung] == [2]

    def test_mehrere_tags_am_eintrag_werden_zerlegt(self):
        plan = va.planen(
            [buchung(1, "Cheetah", tag="Fixpreis, Kunde verrechnet")],
            bestand_bauen(), vertraege={"cheetah"},
        )
        assert len(plan.schon_richtig) == 1

    def test_eintrag_ohne_kennung_wird_uebergangen(self):
        roh = buchung(1, "Cheetah")
        roh["eintrag_id"] = None
        plan = va.planen([roh], bestand_bauen(), vertraege={"cheetah"})
        assert plan.zu_setzen == []


# ── Das Schreiben ──────────────────────────────────────────────────────


class FakeToggl:
    """Ein Toggl, das den geänderten Eintrag zurückgibt — wie das echte."""

    def __init__(self, *, antwort: dict | None = None, fehler: Exception | None = None):
        self.aufrufe: list[tuple[int, int]] = []
        self._antwort = antwort
        self._fehler = fehler

    async def add_time_entry_tag(self, eintrag_id, tag_id, workspace=None):
        self.aufrufe.append((eintrag_id, tag_id))
        if self._fehler is not None:
            raise self._fehler
        if self._antwort is not None:
            return dict(self._antwort)
        name = next(t["name"] for t in TAGS if t["id"] == tag_id)
        return {
            "id": eintrag_id, "tags": [name],
            "description": "Arbeit", "duration": 3600,
        }


@pytest.mark.asyncio
class TestSetzen:
    async def test_tag_wird_ueber_die_kennung_gesetzt(self):
        """Über die Kennung, nicht über den Namen: Toggl legt fehlende Namen an."""
        plan = va.planen([buchung(7, "Cheetah")], bestand_bauen(), vertraege={"cheetah"})
        toggl = FakeToggl()
        ergebnisse = await va.setzen(toggl, 42, plan, tags=TAGS)

        assert toggl.aufrufe == [(7, 1)]
        assert [e.gelungen for e in ergebnisse] == [True]

    async def test_unbekannter_tagname_wird_nicht_angelegt(self):
        plan = va.planen([buchung(7, "Cheetah")], bestand_bauen(), vertraege={"cheetah"})
        toggl = FakeToggl()
        ergebnisse = await va.setzen(
            toggl, 42, plan, tags=[{"id": 2, "name": "Fixpreis"}]
        )

        assert toggl.aufrufe == []
        assert not ergebnisse[0].gelungen
        assert "wird nicht angelegt" in ergebnisse[0].hindernis

    async def test_ein_fehlschlag_haelt_den_lauf_nicht_auf(self):
        plan = va.planen(
            [buchung(1, "Cheetah"), buchung(2, "Cheetah")],
            bestand_bauen(), vertraege={"cheetah"},
        )
        toggl = FakeToggl(fehler=RuntimeError("Netz weg"))
        ergebnisse = await va.setzen(toggl, 42, plan, tags=TAGS)

        assert len(ergebnisse) == 2
        assert all(not e.gelungen for e in ergebnisse)
        assert "Netz weg" in ergebnisse[0].hindernis

    async def test_verlorene_beschreibung_haelt_an(self):
        """Ein ``PUT``, der mehr verändert als das Tag, ist ein Datenverlust.

        Er darf sich nicht über achtzig Einträge wiederholen, bevor jemand
        hinsieht — auffallen würde er erst beim nächsten Leistungsrapport.
        """
        plan = va.planen(
            [buchung(1, "Cheetah"), buchung(2, "Cheetah")],
            bestand_bauen(), vertraege={"cheetah"},
        )
        toggl = FakeToggl(antwort={
            "id": 1, "tags": ["Kunde verrechnet"], "description": "", "duration": 3600,
        })
        ergebnisse = await va.setzen(toggl, 42, plan, tags=TAGS)

        assert len(toggl.aufrufe) == 1, "nach der Abweichung wird nicht weitergeschrieben"
        assert len(ergebnisse) == 1
        assert "Beschreibung geändert" in ergebnisse[0].hindernis

    async def test_entwertete_dauer_haelt_an(self):
        plan = va.planen([buchung(1, "Cheetah")], bestand_bauen(), vertraege={"cheetah"})
        toggl = FakeToggl(antwort={
            "id": 1, "tags": ["Kunde verrechnet"], "description": "Arbeit",
            "duration": -1,
        })
        ergebnisse = await va.setzen(toggl, 42, plan, tags=TAGS)

        assert "entwertet" in ergebnisse[0].hindernis

    async def test_tag_fehlt_in_der_antwort(self):
        """Toggl antwortet mit 200 und hat nichts gesetzt — das ist kein Erfolg."""
        plan = va.planen([buchung(1, "Cheetah")], bestand_bauen(), vertraege={"cheetah"})
        toggl = FakeToggl(antwort={"id": 1, "tags": [], "duration": 3600})
        ergebnisse = await va.setzen(toggl, 42, plan, tags=TAGS)

        assert not ergebnisse[0].gelungen
        assert "steht danach nicht am Eintrag" in ergebnisse[0].hindernis

    async def test_leere_antwort_gilt_nicht_als_erfolg(self):
        plan = va.planen([buchung(1, "Cheetah")], bestand_bauen(), vertraege={"cheetah"})
        toggl = FakeToggl(antwort={})
        ergebnisse = await va.setzen(toggl, 42, plan, tags=TAGS)

        assert not ergebnisse[0].gelungen


# ── Die Stammdaten ─────────────────────────────────────────────────────


class TestStammdaten:
    def test_kein_name_enthaelt_ein_komma(self):
        """``_vorhandene`` zerlegt den Text an ``", "`` — das trägt nur so."""
        assert not any("," in name for name in VERRECHNUNGSARTEN)

    def test_die_datei_deklariert_die_art_fuer_jeden_aktiven_vertrag(self):
        bestand, _ = laden()
        ohne = [
            v.schluessel for v in bestand.aktive()
            if v.verrechnungsart not in VERRECHNUNGSARTEN
        ]
        assert ohne == [], f"ohne Verrechnungsart: {ohne}"

    def test_die_bfh_vertraege_tragen_fixpreis(self):
        """Abgelesen aus Januar bis August 2026: ein CAS wird zum Honorar gehalten."""
        bestand, _ = laden()
        bfh = [
            v for v in bestand.vertraege.values()
            if v.kunde.schluessel == "bfh"
        ]
        assert bfh, "die BFH muss Verträge haben"
        assert {v.verrechnungsart for v in bfh} == {"Fixpreis"}

    def test_interne_projekte_stehen_namentlich_in_der_datei(self):
        bestand, _ = laden()
        assert "Sales & Networking" in bestand.interne_projekte
        assert len(bestand.interne_projekte) >= 6

    def test_kein_internes_projekt_trifft_einen_vertrag(self):
        """Sonst wäre ein Projekt verrechenbar und unverrechenbar zugleich."""
        bestand, befund = laden()
        widersprueche = [
            m for m in befund.get("bemaengelt", [])
            if "trifft auch Vertrag" in m
        ]
        assert widersprueche == []

    def test_unbekannte_art_wird_bemaengelt_nicht_uebernommen(self, tmp_path):
        datei = tmp_path / "v.yaml"
        datei.write_text(
            "vorgaben:\n"
            "  mwst: 8.1\n"
            "  verrechnungsart: Kunde verrechnet\n"
            "kunden:\n"
            "  - schluessel: mba\n"
            "    empfaenger: k@be.ch\n"
            "    ablage: MBA\n"
            "vertraege:\n"
            "  - schluessel: x\n"
            "    bezeichnung: X\n"
            "    kunde: mba\n"
            "    verrechnungsart: Pauschale\n",
            encoding="utf-8",
        )
        bestand, befund = laden(datei, bekannte_kunden={"mba"})

        assert bestand.vertraege["x"].verrechnungsart == "Kunde verrechnet"
        assert any("Pauschale" in m for m in befund["bemaengelt"])

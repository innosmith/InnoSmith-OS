"""Tests für das Auffalten der gruppierten Toggl-Antwort.

Die tragenden Tests sind die beiden Rückfalltests: ``start`` und ``client_id``
stehen in der Reports-API **nicht** auf der obersten Ebene der Gruppe. Wer sie
dort liest, bekommt keine Fehlermeldung, sondern eine vollzählige Tabelle ohne
Datum und ohne Kundschaft — und jede Zeitfrage danach ist still falsch.
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "toggl"))

from zeiteintraege import (  # noqa: E402
    auffalten, je_projekt, je_verrechnungsart, projektzeilen,
)

PROJEKTE = [
    {"id": 11, "name": "Cheetah", "client_id": 5, "active": True, "billable": True},
    {"id": 12, "name": "Sympholio", "client_id": 5, "active": True, "billable": True},
    {"id": 13, "name": "Intern", "client_id": None, "active": True, "billable": False},
]
KUNDEN = [{"id": 5, "name": "MBA"}]


def gruppe(**felder):
    """Eine Reports-Gruppe, wie Toggl sie liefert."""
    standard = {
        "project_id": 11,
        "username": "Anthony Smith",
        "description": "Konzept",
        "billable": True,
        "hourly_rate_in_cents": 20000,
        "billable_amount_in_cents": 40000,
        "currency": "CHF",
        "time_entries": [
            {"id": 1, "start": "2026-08-14T09:00:00+02:00", "seconds": 7200},
        ],
    }
    return {**standard, **felder}


# ── Die beiden Rückfalltests ─────────────────────────────


def test_das_datum_kommt_aus_dem_untereintrag():
    """``start`` steht nicht auf der Gruppe. Dort gelesen, wären alle Daten leer."""
    eintraege, _ = auffalten([gruppe()], PROJEKTE, KUNDEN)
    assert eintraege[0]["datum"] == "2026-08-14"
    assert eintraege[0]["beginn"] == "2026-08-14T09:00:00+02:00"


def test_die_kundschaft_kommt_ueber_das_projekt():
    """``client_id`` steht nicht auf der Gruppe, sondern am Projekt."""
    eintraege, befund = auffalten([gruppe()], PROJEKTE, KUNDEN)
    assert (eintraege[0]["kunden_id"], eintraege[0]["kunde"]) == (5, "MBA")
    assert befund["eintraege_ohne_kundennamen"] == 0


def test_eine_gruppe_mit_start_auf_oberster_ebene_taeuscht_nicht():
    """Selbst wenn dort etwas steht, gilt der Untereintrag."""
    eintraege, _ = auffalten(
        [gruppe(start="2020-01-01T00:00:00+01:00")], PROJEKTE, KUNDEN
    )
    assert eintraege[0]["datum"] == "2026-08-14"


# ── Auffalten ────────────────────────────────────────────


def test_eine_gruppe_mit_mehreren_buchungen_wird_zu_mehreren_zeilen():
    g = gruppe(time_entries=[
        {"id": 1, "start": "2026-08-14T09:00:00+02:00", "seconds": 3600},
        {"id": 2, "start": "2026-08-15T09:00:00+02:00", "seconds": 5400},
    ])
    eintraege, befund = auffalten([g], PROJEKTE, KUNDEN)
    assert befund == {
        "gruppen": 1, "eintraege": 2, "eintraege_ohne_kundennamen": 0,
        "stunden_ohne_verrechnungsart": 2.5,
    }
    assert [e["stunden"] for e in eintraege] == [1.0, 1.5]
    assert [e["datum"] for e in eintraege] == ["2026-08-14", "2026-08-15"]


def test_der_betrag_wird_nach_sekunden_aufgeteilt():
    """Der Betrag gilt für die Gruppe; die Aufteilung kehrt seine Entstehung um."""
    g = gruppe(
        billable_amount_in_cents=30000,
        time_entries=[
            {"id": 1, "start": "2026-08-14T09:00:00+02:00", "seconds": 3600},
            {"id": 2, "start": "2026-08-15T09:00:00+02:00", "seconds": 1800},
        ],
    )
    eintraege, _ = auffalten([g], PROJEKTE, KUNDEN)
    assert [e["betrag"] for e in eintraege] == [200.0, 100.0]
    assert sum(e["betrag"] for e in eintraege) == 300.0


def test_gruppe_ohne_sekunden_teilt_nicht_durch_null():
    g = gruppe(time_entries=[{"id": 1, "start": "2026-08-14T09:00:00+02:00", "seconds": 0}])
    eintraege, _ = auffalten([g], PROJEKTE, KUNDEN)
    assert eintraege[0]["betrag"] == 0.0
    assert eintraege[0]["stunden"] == 0.0


def test_gruppe_ohne_projekt_wird_gezaehlt():
    eintraege, befund = auffalten([gruppe(project_id=None)], PROJEKTE, KUNDEN)
    assert befund["gruppen_ohne_projekt"] == 1
    assert eintraege[0]["projekt"] == ""


def test_kundennummer_ohne_namen_ist_ein_mangel_kein_leerer_wert():
    """Gezählt wird der unaufgelöste Name, nicht die fehlende Kennung."""
    projekte = [{"id": 11, "name": "Cheetah", "client_id": 99}]
    _, befund = auffalten([gruppe()], projekte, KUNDEN)
    assert befund["eintraege_ohne_kundennamen"] == 1


def test_leere_gruppe_erzeugt_keine_zeile():
    eintraege, befund = auffalten([gruppe(time_entries=[])], PROJEKTE, KUNDEN)
    assert eintraege == []
    assert befund["gruppen"] == 1


def test_keine_gruppen_ergibt_leeres_ergebnis():
    eintraege, befund = auffalten([], PROJEKTE, KUNDEN)
    assert eintraege == []
    assert befund["eintraege"] == 0


# ── Projektzeilen ────────────────────────────────────────


def test_projektzeilen_loesen_die_kundschaft_auf():
    zeilen = projektzeilen(PROJEKTE, KUNDEN)
    assert [z["kunde"] for z in zeilen] == ["MBA", "MBA", ""]
    assert [z["verrechenbar"] for z in zeilen] == [True, True, False]


# ── Verdichten je Projekt ────────────────────────────────


def test_je_projekt_summiert_alle_stunden_nicht_nur_verrechenbare():
    """Ein Fixvertrag verbraucht auch nicht verrechenbare Zeit."""
    gruppen = [
        gruppe(project_id=11, billable=True, time_entries=[
            {"id": 1, "start": "2026-08-14T09:00:00+02:00", "seconds": 7200},
        ]),
        gruppe(project_id=11, billable=False, billable_amount_in_cents=0, time_entries=[
            {"id": 2, "start": "2026-08-15T09:00:00+02:00", "seconds": 3600},
        ]),
    ]
    eintraege, _ = auffalten(gruppen, PROJEKTE, KUNDEN)
    verdichtet = je_projekt(eintraege)

    assert verdichtet[11]["stunden"] == 3.0
    assert verdichtet[11]["verrechenbare_stunden"] == 2.0
    assert verdichtet[11]["kunde"] == "MBA"


def test_je_projekt_trennt_die_projekte():
    gruppen = [
        gruppe(project_id=11),
        gruppe(project_id=12, time_entries=[
            {"id": 9, "start": "2026-08-20T09:00:00+02:00", "seconds": 1800},
        ]),
    ]
    eintraege, _ = auffalten(gruppen, PROJEKTE, KUNDEN)
    verdichtet = je_projekt(eintraege)

    assert set(verdichtet) == {11, 12}
    assert verdichtet[12]["stunden"] == 0.5
    assert verdichtet[12]["projekt"] == "Sympholio"


def test_je_projekt_ueberspringt_buchungen_ohne_projekt():
    eintraege, _ = auffalten([gruppe(project_id=None)], PROJEKTE, KUNDEN)
    assert je_projekt(eintraege) == {}


# ── Verrechnungsart, Person und Aufgabe ──────────────────

TAGS = [
    {"id": 7413719, "name": "Kunde verrechnet"},
    {"id": 8280154, "name": "Fixpreis"},
    {"id": 10954040, "name": "keine Verrechnung"},
]


def test_die_verrechnungsart_kommt_aus_den_tags():
    eintraege, _ = auffalten([gruppe(tag_ids=[8280154])], PROJEKTE, KUNDEN, TAGS)
    assert eintraege[0]["verrechnungsart"] == "Fixpreis"


def test_mehrere_tags_ergeben_immer_dieselbe_reihenfolge():
    """Sonst wären «Fixpreis, Kunde verrechnet» und die Umkehrung zwei
    verschiedene Gruppen in jeder Auswertung."""
    a, _ = auffalten([gruppe(tag_ids=[8280154, 7413719])], PROJEKTE, KUNDEN, TAGS)
    b, _ = auffalten([gruppe(tag_ids=[7413719, 8280154])], PROJEKTE, KUNDEN, TAGS)

    assert a[0]["verrechnungsart"] == b[0]["verrechnungsart"] == "Fixpreis, Kunde verrechnet"


def test_unbekannte_tag_kennung_bleibt_sichtbar_statt_zu_verschwinden():
    """Ein neu angelegter Tag darf nicht zu einer leeren Zelle werden, die
    aussieht wie «kein Tag gesetzt»."""
    eintraege, _ = auffalten([gruppe(tag_ids=[999])], PROJEKTE, KUNDEN, TAGS)
    assert eintraege[0]["verrechnungsart"] == "999"


def test_stunden_ohne_verrechnungsart_werden_gezaehlt():
    """Am 21.09.2026 waren das 428 der 1961 Jahresstunden — der zweitgrösste
    Posten und damit zu viel, um unbemerkt zu bleiben."""
    gruppen = [
        gruppe(tag_ids=[7413719]),
        gruppe(tag_ids=[], time_entries=[
            {"id": 9, "start": "2026-08-20T09:00:00+02:00", "seconds": 5400},
        ]),
    ]
    _, befund = auffalten(gruppen, PROJEKTE, KUNDEN, TAGS)

    assert befund["stunden_ohne_verrechnungsart"] == 1.5


def test_je_verrechnungsart_benennt_die_unbetagte_stunde():
    """``(ohne Tag)`` statt leer: eine leere Beschriftung sieht in jeder
    Darstellung wie ein Darstellungsfehler aus."""
    gruppen = [
        gruppe(tag_ids=[8280154]),
        gruppe(tag_ids=[], time_entries=[
            {"id": 9, "start": "2026-08-20T09:00:00+02:00", "seconds": 3600},
        ]),
    ]
    eintraege, _ = auffalten(gruppen, PROJEKTE, KUNDEN, TAGS)

    assert je_verrechnungsart(eintraege) == {"Fixpreis": 2.0, "(ohne Tag)": 1.0}


def test_person_und_aufgabe_werden_mitgefuehrt():
    """Beides braucht der Leistungsrapport: die Person fürs Kürzel an der
    Zeile, die Aufgabe für die Aufschlüsselung nach Bereich."""
    eintraege, _ = auffalten(
        [gruppe(user_id=5368086, task_id=239724200)], PROJEKTE, KUNDEN,
        aufgaben={239724200: "Evento"},
    )

    assert eintraege[0]["person_id"] == 5368086
    assert eintraege[0]["person"] == "Anthony Smith"
    assert eintraege[0]["aufgabe"] == "Evento"


def test_ohne_nachschlagetabellen_bleibt_die_kennung_und_der_name_leer():
    """Sichtbar, nicht geraten."""
    eintraege, _ = auffalten([gruppe(task_id=239724200)], PROJEKTE, KUNDEN)

    assert eintraege[0]["aufgabe_id"] == 239724200
    assert eintraege[0]["aufgabe"] == ""

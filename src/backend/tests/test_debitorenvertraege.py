"""Tests für das Vertragsstammdatum der Debitoren.

Zwei Sorten, und die erste ist die wichtigere: ein Teil prüft das Modul gegen
erfundene Dateien, ein Teil prüft die **echte** ``docs/debitorenvertraege.yaml``.
Ohne den zweiten Teil wäre ein Tippfehler in der gepflegten Datei genau das,
wogegen das Stammdatum antritt — eine Lücke, die niemandem auffällt.
"""

from __future__ import annotations

import textwrap
from datetime import date

import pytest

from app.services import debitorenvertraege as dv
from app.services.debitoren_positionen import Positionsdeutung
from app.services.debitoren_pruefung import Vertragsart


def schreiben(tmp_path, text: str):
    pfad = tmp_path / "vertraege.yaml"
    pfad.write_text(textwrap.dedent(text), encoding="utf-8")
    return pfad


KUNDEN = {"mba", "bfh", "agg", "wyss-academy"}


# ── Die gepflegte Datei ──────────────────────────────────


@pytest.fixture(scope="module")
def echt():
    bestand, befund = dv.laden()
    return bestand, befund


def test_die_gepflegte_datei_laesst_sich_lesen(echt):
    bestand, befund = echt
    assert befund["vertraege"] > 25
    assert befund["kunden"] >= 10
    assert "verworfen" not in befund, befund.get("verworfen")


def test_jede_kundschaft_steht_im_kundenschluessel(echt):
    """Der Wächter gegen die tote Verknüpfung.

    Ein ``kunde``, den der Kundenschlüssel nicht führt, sähe wie eine Zuordnung
    aus und trüge keine. ``laden`` verwirft solche Einträge — dass hier nichts
    verworfen wurde, ist der eigentliche Beweis.
    """
    bestand, _ = echt
    bekannt = dv.bekannte_kundschaften()
    assert bekannt, "kundenschluessel.yaml wurde nicht gefunden"
    assert {k.schluessel for k in bestand.kunden.values()} <= bekannt


def test_bildungsleistungen_sind_steuerfrei(echt):
    bestand, _ = echt
    bfh = [v for v in bestand.vertraege.values() if v.kunde.schluessel == "bfh"]
    # Keine feste Zahl: jeder neue Lehrgang bringt einen Vertrag mit, und eine
    # Zahl hier hiesse, den Test bei jedem Semester anzupassen.
    assert bfh
    assert all(v.mwst == 0.0 for v in bfh)
    assert all(not v.leistungsrapport for v in bfh)


def test_referenzpflicht_nur_beim_kanton(echt):
    bestand, _ = echt
    mit = {k for k, v in bestand.kunden.items() if v.referenz_pflicht}
    assert mit == {"mba", "agg", "aue"}


FIXSTUNDEN_BESTAETIGT = {
    "impulskoeniz": (7.0, False),
    "cheetah": (14.5, True),
    "unterstuetzung-fbi": (5.0, False),
    "sympholio": (14.0, True),
    "digitale-evolution-mit-ki": (14.0, True),
    "deinklima": (7.0, True),
}
"""Am 20.09.2026 bestätigt, hergeleitet aus den Positionstexten der
August-Rechnungen («7h fix inklusive (nicht übertragbar)» und so fort). Die
Zahlen stehen hier ein zweites Mal, damit eine stille Änderung an der Stammdatei
auffällt: eine falsche Fixstundenzahl erzeugt keine Fehlermeldung, sondern eine
plausible falsche Rechnung."""


def test_bestaetigte_fixstunden_stehen_im_bestand(echt):
    bestand, _ = echt
    for schluessel, (stunden, uebertragbar) in FIXSTUNDEN_BESTAETIGT.items():
        vertrag = bestand.vertraege[schluessel]
        assert vertrag.fix_stunden == stunden, schluessel
        assert vertrag.uebertragbar is uebertragbar, schluessel
        assert vertrag.bestaetigt, schluessel


def test_ohne_bestaetigung_wird_keine_pauschale_angenommen(echt):
    """Was niemand bestätigt hat, gilt als variabel — nie als geraten.

    Eine angenommene Pauschale wäre der teuerste Fehler dieser Datei: sie
    stellte einen Grundbetrag in Rechnung, den niemand vereinbart hat.
    """
    bestand, _ = echt
    unbestaetigt = [v for v in bestand.vertraege.values() if not v.bestaetigt]
    assert all(v.fix_stunden is None for v in unbestaetigt)
    assert all(
        v.als_vertrag().art in {Vertragsart.VARIABEL, Vertragsart.FESTPREIS}
        for v in unbestaetigt
    )


def test_der_august_wird_zur_probe_richtig_eingestuft(echt):
    """Gegenprobe am echten Rücklauf: die Pauschalverträge dürfen nicht mehr
    als variabel durchgehen, sonst wiederholt sich der Fehlalarm vom 20.09."""
    bestand, _ = echt
    uebertragbar = bestand.vertraege["cheetah"].als_vertrag().art
    fest = bestand.vertraege["impulskoeniz"].als_vertrag().art
    assert uebertragbar is Vertragsart.FIX_UEBERTRAGBAR
    assert fest is Vertragsart.FIX_VERFALLEND


def test_deinklima_wechselte_die_gegenpartei(echt):
    """Der Fall, der den Zeitbezug nötig macht — siehe ``Vertragsdaten.frueher``."""
    bestand, _ = echt
    vertrag = bestand.vertraege["deinklima"]

    assert vertrag.kunde.schluessel == "aue"
    assert vertrag.kunde_am(date(2026, 3, 31)).schluessel == "aue"
    assert vertrag.kunde_am(date(2025, 12, 31)).schluessel == "wyss-academy"
    assert vertrag.kunde_am(date(2024, 6, 30)).schluessel == "wyss-academy"


def test_die_referenzpflicht_folgt_der_damaligen_gegenpartei(echt):
    """Der Grund, warum der Wechsel überhaupt festgehalten wird."""
    bestand, _ = echt
    vertrag = bestand.vertraege["deinklima"]

    assert vertrag.als_vertrag(date(2026, 3, 31)).referenz_pflicht is True
    assert vertrag.als_vertrag(date(2025, 11, 30)).referenz_pflicht is False


@pytest.mark.parametrize(
    "titel, erwartet",
    [
        # So heissen die Rechnungen in Bexio …
        ("CAS IBD HS25 Lektionen", "cas-ibd-hs25"),
        ("Co-Leitung CAS IBD HS25 und Lektionen", "cas-ibd-hs25"),
        ("CAS TCM FS26 Lektionen", "cas-tcm-fs26"),
        ("CAS TCM FS26 Co-Leitung & Lektionen", "cas-tcm-fs26"),
        # … und so die Projekte in Toggl.
        ("CAS IBD Dozent HS25", "cas-ibd-hs25"),
        ("CAS IBD Co-Leitung HS25", "cas-ibd-hs25"),
        ("CAS TCM Dozent FS26", "cas-tcm-fs26"),
        ("CAS TCM Co-Leitung FS26", "cas-tcm-fs26"),
        ("CAS IBD Dozent HS26", "cas-ibd-hs26"),
    ],
)
def test_bfh_findet_beide_schreibweisen(echt, titel, erwartet):
    """Rechnungstitel und Toggl-Projekt heissen verschieden — beide müssen treffen.

    Der Fehler, den dieser Test festhält: die Verträge trugen nur die
    Toggl-Schreibweise («CAS TCM Dozent FS26»), die Rechnung heisst aber «CAS
    TCM FS26 Lektionen». Keines ist Teilzeichenkette des anderen, und so liefen
    sechs BFH-Rechnungen des Jahres 2026 als «ohne Vertrag» mit — ohne Meldung,
    weil «kein Treffer» kein Fehler ist.
    """
    bestand, _ = echt
    vertrag = bestand.finden(titel)
    assert vertrag is not None, f"{titel!r} findet keinen Vertrag"
    assert vertrag.schluessel == erwartet


def test_die_rolle_trennt_toggl_und_nicht_der_vertrag(echt):
    """Ein Vertrag je Semester, weil Bexio beide Rollen auf eine Rechnung legt.

    Die Trennung nach Dozent und Co-Leitung bleibt trotzdem sichtbar: sie steht
    in den Toggl-Projektnamen, die beide auf denselben Vertrag zeigen.
    """
    bestand, _ = echt
    vertrag = bestand.vertraege["cas-tcm-fs26"]
    rollen = [e for e in vertrag.erkennung if "Dozent" in e or "Co-Leitung" in e]
    assert len(rollen) == 2


@pytest.mark.parametrize(
    "tag, fix, takt",
    [
        (date(2025, 9, 30), 7.0, "monatlich"),    # RE-00570
        (date(2025, 10, 31), 3.0, "monatlich"),   # RE-00579
        (date(2025, 11, 30), 3.0, "monatlich"),   # RE-00589
        (date(2025, 12, 31), 1.0, "monatlich"),   # RE-00598
        (date(2026, 3, 31), 1.0, "quartalsweise"),  # RE-00634, Jan bis Mrz
        (date(2026, 6, 30), 1.0, "quartalsweise"),  # RE-00672, Apr bis Jun
        (date(2026, 7, 31), 1.0, "quartalsweise"),
        (date(2026, 8, 31), 7.0, "monatlich"),    # RE-00695
        (None, 7.0, "monatlich"),                 # was heute gilt
    ],
)
def test_impulskoeniz_wechselte_fuenfmal(echt, tag, fix, takt):
    """Der Vertrag, der die Zeitachse nötig machte — jede Stufe an einer
    Rechnung belegt. Ohne sie prüfte der Rücklauf sieben Monate gegen den
    heutigen Massstab und meldete eine Unterdeckung, die keine war."""
    bestand, _ = echt
    kond = bestand.vertraege["impulskoeniz"].konditionen_am(tag)
    assert kond.fix_stunden == fix
    assert kond.rhythmus.value == takt


def test_der_stichtag_wirkt_bis_in_die_pruefeingabe(echt):
    """``als_vertrag`` muss die Konditionen des Monats tragen, nicht die heutigen."""
    bestand, _ = echt
    vertrag = bestand.vertraege["impulskoeniz"]
    assert vertrag.als_vertrag(date(2026, 4, 30)).fix_stunden == 1.0
    assert vertrag.als_vertrag(date(2026, 8, 31)).fix_stunden == 7.0


def test_quartalsvertrag_erwartet_nur_im_quartalsmonat(echt):
    bestand, _ = echt
    takt = bestand.vertraege["impulskoeniz"].konditionen_am(date(2026, 4, 30)).rhythmus
    assert takt.erwartet_rechnung(4, stunden_vorhanden=True) is False
    assert takt.erwartet_rechnung(6, stunden_vorhanden=False) is True


def test_unbekannter_monat_unterdrueckt_keinen_befund():
    """Die Quartalsregel darf nichts wegblenden, was sie nicht beurteilen kann."""
    from app.services.debitorenvertraege import Rhythmus

    assert Rhythmus.QUARTALSWEISE.erwartet_rechnung(None, stunden_vorhanden=False) is True


def test_nach_aufwand_erwartet_nur_bei_stunden(echt):
    """AKV-Bot läuft sporadisch — ein Monat ohne Rechnung ist der Normalfall."""
    bestand, _ = echt
    takt = bestand.vertraege["akv-bot"].rhythmus
    assert takt.value == "nach_aufwand"
    assert takt.erwartet_rechnung(5, stunden_vorhanden=False) is False
    assert takt.erwartet_rechnung(5, stunden_vorhanden=True) is True


def test_ohne_angabe_gilt_monatlich(echt):
    bestand, _ = echt
    assert bestand.vertraege["cheetah"].rhythmus.value == "monatlich"


def test_unbekannter_rhythmus_faellt_zurueck_und_wird_gemeldet(tmp_path):
    """Ein Tippfehler im Takt darf nicht die Prüfung eines Kunden abschalten."""
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba, rhythmus: halbjaehrlich}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.vertraege["p"].rhythmus is dv.Rhythmus.MONATLICH
    assert any("halbjaehrlich" in m for m in befund["verworfen"])


def test_konditionen_ohne_datum_werden_verworfen(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - schluessel: p
            bezeichnung: P
            kunde: mba
            fix_stunden: 7.0
            uebertragbar: false
            konditionen:
              - {fix_stunden: 3.0}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.vertraege["p"].vorher == ()
    assert any("kein Datum" in m for m in befund["verworfen"])


def test_was_eine_stufe_nicht_nennt_gilt_wie_heute(tmp_path):
    """Eine Pflicht zur Wiederholung wäre eine Einladung zum Auseinanderlaufen."""
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - schluessel: p
            bezeichnung: P
            kunde: mba
            fix_stunden: 7.0
            uebertragbar: false
            konditionen:
              - {bis: 2025-12-31, fix_stunden: 3.0}
    """)
    bestand, _ = dv.laden(pfad, bekannte_kunden=KUNDEN)
    frueher = bestand.vertraege["p"].konditionen_am(date(2025, 6, 30))
    assert frueher.fix_stunden == 3.0
    assert frueher.uebertragbar is False   # vom heutigen Stand übernommen
    assert frueher.rhythmus is dv.Rhythmus.MONATLICH


def test_platzhalteradresse_ist_nicht_uebernommen(echt):
    """``TODO_EMAIL_EINTRAGEN`` aus der alten Konfiguration darf nicht mitwandern."""
    bestand, _ = echt
    for kunde in bestand.kunden.values():
        if kunde.empfaenger:
            assert not dv._ist_platzhalter(kunde.empfaenger), kunde.schluessel


# ── Laden und Wächter ────────────────────────────────────


def test_doppelter_schluessel_wird_verworfen(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: p, bezeichnung: Erst, kunde: mba}
          - {schluessel: p, bezeichnung: Zweit, kunde: mba}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert len(bestand.vertraege) == 1
    assert bestand.vertraege["p"].bezeichnung == "Erst"
    assert any("zweimal" in m for m in befund["verworfen"])


def test_unbekannte_kundschaft_wird_verworfen(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: erfunden, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: erfunden}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.kunden == {}
    assert bestand.vertraege == {}
    assert len(befund["verworfen"]) == 2


def test_fixstunden_ohne_uebertragsangabe_werden_nicht_geraten(tmp_path):
    """Der Kern: ein unvollständiger Vertrag darf nicht plausibel aussehen."""
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba, fix_stunden: 20}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    vertrag = bestand.vertraege["p"]

    assert not vertrag.vollstaendig
    assert befund["ohne_uebertragsangabe"] == ["p"]
    assert any("uebertragbar" in m for m in befund["bemaengelt"])
    # Und die Prüfung bekommt die Fixstunden nicht zu sehen, sonst prüfte sie
    # nach einer Lesart, die niemand erklärt hat.
    assert vertrag.als_vertrag().fix_stunden is None
    assert vertrag.als_vertrag().art is Vertragsart.VARIABEL


def test_vollstaendiger_fixvertrag_kommt_durch(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch, referenz_pflicht: true}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba, fix_stunden: 7.5, uebertragbar: true}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    vertrag = bestand.vertraege["p"].als_vertrag()

    assert vertrag.fix_stunden == 7.5  # halbe Stunden, die der alte Regex verbot
    assert vertrag.art is Vertragsart.FIX_UEBERTRAGBAR
    assert vertrag.referenz_pflicht is True  # kommt von der Kundschaft
    assert "ohne_uebertragsangabe" not in befund


def test_uebertragbar_false_ist_nicht_dasselbe_wie_fehlend(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch, ablage: MBA}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba, fix_stunden: 20, uebertragbar: false}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.vertraege["p"].vollstaendig
    assert bestand.vertraege["p"].als_vertrag().art is Vertragsart.FIX_VERFALLEND
    assert "bemaengelt" not in befund


def test_wechsel_auf_unbekannte_kundschaft_wird_verworfen(tmp_path):
    """Sonst fiele die Prüfung stillschweigend auf die heutige Gegenpartei zurück."""
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch, referenz_pflicht: true}
        vertraege:
          - schluessel: p
            bezeichnung: P
            kunde: mba
            kundenwechsel:
              - {kunde: gibt-es-nicht, bis: 2025-12-31}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.vertraege["p"].frueher == ()
    assert any("Kundenwechsel" in m for m in befund["verworfen"])


def test_wechsel_ohne_datum_wird_verworfen(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
          - {schluessel: bfh, empfaenger: c@d.ch}
        vertraege:
          - schluessel: p
            bezeichnung: P
            kunde: mba
            kundenwechsel:
              - {kunde: bfh, bis: 'irgendwann'}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.vertraege["p"].frueher == ()
    assert any("Kundenwechsel" in m for m in befund["verworfen"])


def test_mehrere_wechsel_werden_nach_datum_geordnet(tmp_path):
    """Die Reihenfolge in der Datei darf nicht über das Ergebnis entscheiden."""
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
          - {schluessel: bfh, empfaenger: c@d.ch}
          - {schluessel: agg, empfaenger: e@f.ch}
        vertraege:
          - schluessel: p
            bezeichnung: P
            kunde: mba
            kundenwechsel:
              - {kunde: agg, bis: 2025-12-31}
              - {kunde: bfh, bis: 2023-12-31}
    """)
    bestand, _ = dv.laden(pfad, bekannte_kunden=KUNDEN)
    vertrag = bestand.vertraege["p"]

    assert vertrag.kunde_am(date(2023, 5, 1)).schluessel == "bfh"
    assert vertrag.kunde_am(date(2024, 5, 1)).schluessel == "agg"
    assert vertrag.kunde_am(date(2026, 5, 1)).schluessel == "mba"
    assert vertrag.kunde_am().schluessel == "mba"


def test_fehlender_empfaenger_wird_gemeldet_aber_nicht_bei_ruhenden(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba}
          - {schluessel: bfh}
        vertraege:
          - {schluessel: aktiv, bezeichnung: A, kunde: mba}
          - {schluessel: alt, bezeichnung: B, kunde: bfh, ruhend: true}
    """)
    _, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    bemaengelt = " ".join(befund["bemaengelt"])
    assert "'aktiv'" in bemaengelt
    assert "'alt'" not in bemaengelt


def test_platzhalter_faellt_auf(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: TODO_EMAIL_EINTRAGEN}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba}
    """)
    _, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert any("Platzhalter" in m for m in befund["bemaengelt"])


def test_unmoeglicher_steuersatz_wird_verworfen(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba, mwst: 810}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.vertraege == {}
    assert any("Mehrwertsteuersatz" in m for m in befund["verworfen"])


def test_vorgabe_gilt_wo_der_vertrag_schweigt(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1, anrede: ich}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: a, bezeichnung: A, kunde: mba}
          - {schluessel: b, bezeichnung: B, kunde: mba, mwst: 0.0, anrede: wir}
    """)
    bestand, _ = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert (bestand.vertraege["a"].mwst, bestand.vertraege["a"].anrede) == (8.1, "ich")
    assert (bestand.vertraege["b"].mwst, bestand.vertraege["b"].anrede) == (0.0, "wir")


def test_fehlende_datei_ergibt_leeren_bestand(tmp_path):
    bestand, befund = dv.laden(tmp_path / "gibt-es-nicht.yaml", bekannte_kunden=KUNDEN)
    assert bestand.vertraege == {}
    assert befund["vertraege"] == 0


def test_ablageordner_faellt_nicht_auf_den_schluessel_zurueck(tmp_path):
    """Ohne gepflegten Alias gibt es keinen Ordner — und damit keine Ablage.

    Bis zum 21.09.2026 fiel der Wert auf den Schlüssel zurück. Gegen das echte
    Archiv gemessen hätten damit elf von 25 aktiven Verträgen einen **neuen**
    Ordner angelegt: «bfh» neben «Berner Fachhochschule», «gsw» neben «GSW
    Treuhand AG». Niemand hätte es bemerkt, denn eine Ablage, die einen Ordner
    anlegt, sieht aus wie eine, die abgelegt hat.
    """
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch, ablage: MBA}
          - {schluessel: bfh, empfaenger: c@d.ch}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: bfh}
    """)
    bestand, befund = dv.laden(pfad, bekannte_kunden=KUNDEN)
    assert bestand.kunden["mba"].ordner == "MBA"
    assert bestand.kunden["bfh"].ordner is None
    # Der Mangel wird gemeldet, nicht verschwiegen.
    assert any("Ablageordner" in m for m in befund.get("bemaengelt") or [])


# ── Finden ───────────────────────────────────────────────


@pytest.fixture
def klein(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: fbi, bezeichnung: Unterstützung FBI, kunde: mba, erkennung: [FBI]}
          - {schluessel: cheetah, bezeichnung: Cheetah, kunde: mba}
          - {schluessel: alt, bezeichnung: Altes, kunde: mba, ruhend: true}
    """)
    bestand, _ = dv.laden(pfad, bekannte_kunden=KUNDEN)
    return bestand


def test_finden_ueber_die_bezeichnung(klein):
    assert klein.finden("Cheetah").schluessel == "cheetah"
    assert klein.finden("  cheetah  ").schluessel == "cheetah"


def test_finden_ueber_das_stichwort(klein):
    assert klein.finden("Projekt FBI Februar").schluessel == "fbi"


def test_die_bezeichnung_schlaegt_das_stichwort(klein):
    """Sonst gewänne ein zufälliges Stichwort gegen den vollen Namen."""
    assert klein.finden("Unterstützung FBI").schluessel == "fbi"


def test_was_nicht_passt_wird_nicht_erraten(klein):
    assert klein.finden("Ein ganz anderes Projekt") is None


def test_ruhende_zaehlen_nicht_als_aktiv(klein):
    assert {v.schluessel for v in klein.aktive()} == {"fbi", "cheetah"}


# ── Abgleich ─────────────────────────────────────────────


def test_abgleich_findet_das_projekt_ohne_vertrag(klein):
    """Die gefährliche Richtung: ungeprüft sieht aus wie unbeanstandet."""
    ergebnis = dv.abgleichen(klein, ["Cheetah", "Nagelneu"])
    assert ergebnis["ohne_vertrag"] == ["Nagelneu"]
    assert ergebnis["zugeordnet"] == 1


def test_abgleich_findet_den_vertrag_ohne_rechnung(klein):
    ergebnis = dv.abgleichen(klein, ["Cheetah"])
    assert ergebnis["ohne_rechnung"] == ["fbi"]


def test_abgleich_meldet_ruhende_nicht_als_fehlend(klein):
    ergebnis = dv.abgleichen(klein, ["Cheetah", "Unterstützung FBI"])
    assert "ohne_rechnung" not in ergebnis
    assert "ohne_vertrag" not in ergebnis


# ── Fixstunden vorschlagen ───────────────────────────────


def test_vorschlag_bei_leerem_vertrag(klein):
    vorschlaege = dv.vorschlagen(
        klein, {"cheetah": Positionsdeutung(fix_stunden=20.0, uebertrag_angabe=3.0)}
    )
    assert len(vorschlaege) == 1
    assert vorschlaege[0]["art"] == "neu"
    assert vorschlaege[0]["fix_stunden_laut_rechnung"] == 20.0
    assert vorschlaege[0]["uebertragszeile_vorhanden"] is True
    assert "20 Stunden" in vorschlaege[0]["frage"]


def test_abweichung_wird_gemeldet_statt_uebernommen(tmp_path):
    """Eine Maschine darf hinzufügen, nie ändern."""
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba, fix_stunden: 20, uebertragbar: true}
    """)
    bestand, _ = dv.laden(pfad, bekannte_kunden=KUNDEN)
    vorschlaege = dv.vorschlagen(bestand, {"p": Positionsdeutung(fix_stunden=25.0)})

    assert vorschlaege[0]["art"] == "abweichend"
    assert vorschlaege[0]["fix_stunden_laut_vertrag"] == 20.0
    assert bestand.vertraege["p"].fix_stunden == 20.0  # unverändert


def test_uebereinstimmung_ist_kein_vorschlag(tmp_path):
    pfad = schreiben(tmp_path, """
        vorgaben: {mwst: 8.1}
        kunden:
          - {schluessel: mba, empfaenger: a@b.ch}
        vertraege:
          - {schluessel: p, bezeichnung: P, kunde: mba, fix_stunden: 20, uebertragbar: true}
    """)
    bestand, _ = dv.laden(pfad, bekannte_kunden=KUNDEN)
    vorschlaege = dv.vorschlagen(bestand, {"p": Positionsdeutung(fix_stunden=20.0)})
    assert vorschlaege[0]["art"] == "bestaetigt"
    assert "frage" not in vorschlaege[0]


def test_ohne_gelesene_fixstunden_kein_vorschlag(klein):
    assert dv.vorschlagen(klein, {"cheetah": Positionsdeutung()}) == []


def test_unbekannter_vertrag_erzeugt_keinen_vorschlag(klein):
    assert dv.vorschlagen(klein, {"gibt-es-nicht": Positionsdeutung(fix_stunden=8)}) == []

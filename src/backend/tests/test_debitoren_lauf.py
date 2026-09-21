"""Tests für das Zusammentragen des Rechnungslaufs.

Geprüft wird vor allem, was **nicht** verschwinden darf: ein Entwurf ohne
Vertrag, erfasste Stunden ohne Entwurf, ein fehlgeschlagener Teilabruf. Alles
drei sieht ohne Meldung wie «nichts zu beanstanden» aus, und genau das war der
Befund am alten Excel-Bericht.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pytest

from app.services import debitoren_lauf as lauf
from app.services import debitoren_pruefung as pr
from app.services import debitoren_uebertrag as ue
from app.services.debitorenvertraege import Bestand, Kunde, Vertragsdaten

AGG = Kunde(schluessel="agg", empfaenger="re@agg.ch", referenz_pflicht=True)
BFH = Kunde(schluessel="bfh", empfaenger="re@bfh.ch")


def bestand() -> Bestand:
    return Bestand(
        vertraege={
            "beratung": Vertragsdaten(
                schluessel="beratung", bezeichnung="AGG Beratung", kunde=AGG,
                mwst=8.1, fix_stunden=20.0, uebertragbar=True,
            ),
            "cas": Vertragsdaten(
                schluessel="cas", bezeichnung="CAS IBD", kunde=BFH, mwst=0.0,
                stunden_pruefen=False, leistungsrapport=False,
            ),
        },
        kunden={"agg": AGG, "bfh": BFH},
        vorgaben={"iban_praefix": "CH93 0076"},
    )


MASSSTAB = pr.Massstab(iban_erwartet="CH93 0076", uebertragsformel=ue.BESTAETIGT)


def entwurf(**abweichung) -> dict:
    grund = dict(
        rechnung_id=1, nummer="RE-1001", datum="2026-08-31", kunden_id=5,
        kunde="AGG", titel="AGG Beratung August", netto=4000.0, steuer=324.0,
        brutto=4324.0, referenz="Auftrag 4711",
    )
    return {**grund, **abweichung}


POSITIONEN = [
    {"positionsart": "custom", "pos": 1, "text": "20h/Monat fix inklusive",
     "amount": "20.0", "unit_id": 2, "unit_price": "200.0"},
]


# ── Der Steuersatz ───────────────────────────────────────


class TestSteuersatz:
    def test_wird_aus_beiden_betraegen_hergeleitet(self):
        """Bexio nennt keinen Satz, nur Netto und Steuerbetrag."""
        assert lauf.steuersatz(4000.0, 324.0) == 8.1

    def test_null_steuer_bei_umsatz_ist_null_prozent(self):
        """Die BFH-Verträge — steuerfrei ist eine Aussage, keine Lücke."""
        assert lauf.steuersatz(3000.0, 0.0) == 0.0

    def test_ohne_nettobetrag_bleibt_der_satz_unbekannt(self):
        """``0.0`` sähe wie ein steuerfreier Umsatz aus und wäre für die BFH
        sogar richtig — der Fehler würde also unsichtbar."""
        assert lauf.steuersatz(0.0, 0.0) is None

    def test_rundung_bleibt_in_der_toleranz_der_regel(self):
        """Die Regel vergleicht mit 0.01; 8.1 % auf krummen Beträgen darf nicht
        an der dritten Nachkommastelle scheitern."""
        satz = lauf.steuersatz(5625.0, 455.63)
        assert satz is not None and abs(satz - 8.1) < 0.01


class TestPeriodengrenzen:
    def test_letzter_tag_kommt_aus_dem_kalender(self):
        assert lauf.periodengrenzen(2026, 2) == (date(2026, 2, 1), date(2026, 2, 28))
        assert lauf.periodengrenzen(2024, 2)[1] == date(2024, 2, 29)


# ── Auswerten ────────────────────────────────────────────


class TestAuswerten:
    def test_entwurf_trifft_vertrag_und_stunden(self):
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                entwuerfe=[entwurf()],
                positionen={1: POSITIONEN},
                stunden={7: {"projekt": "AGG Beratung", "kunde": "AGG", "stunden": 22.0}},
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
            uebertrag_vormonat={"beratung": 5.0},
        )
        assert len(ergebnis.ergebnisse) == 1
        assert ergebnis.ohne_vertrag == [] and ergebnis.ohne_entwurf == []
        einzeln = ergebnis.ergebnisse[0]
        assert einzeln.projekt == "AGG Beratung"
        assert {b.regel: b.zustand for b in einzeln.befunde}["mwst"] is pr.Zustand.RICHTIG

    def test_entwurf_ohne_vertrag_wird_gemeldet_statt_uebergangen(self):
        """Die gefährliche Richtung: ungeprüft sieht aus wie unbeanstandet."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(entwuerfe=[entwurf(titel="Irgendein neues Projekt")]),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert ergebnis.ergebnisse == []
        assert [e["titel"] for e in ergebnis.ohne_vertrag] == ["Irgendein neues Projekt"]

    def test_stunden_ohne_entwurf_werden_gemeldet(self):
        """Die vergessene Rechnung — im Altbestand nur durch Nachrechnen zu finden."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                stunden={7: {"projekt": "AGG Beratung", "kunde": "AGG", "stunden": 12.0}}
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert [e["vertrag"] for e in ergebnis.ohne_entwurf] == ["beratung"]
        assert ergebnis.ohne_entwurf[0]["stunden"] == 12.0

    def test_stunden_auf_unbekanntem_projekt_werden_gemeldet(self):
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                stunden={9: {"projekt": "Neukunde XY", "kunde": "XY", "stunden": 3.0,
                             "verrechenbare_stunden": 3.0}}
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert ergebnis.ohne_entwurf[0]["grund"] == "kein Vertrag hinterlegt"

    def test_eigene_arbeit_ist_keine_vergessene_rechnung(self):
        """«Strategy & Planning» und Geschwister stehen in Toggl auf
        ``billable=false`` und haben keinen Vertrag. Drei Dauergäste in der
        Mängelliste lehren, über die Liste hinwegzulesen."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                stunden={
                    9: {"projekt": "Strategy & Planning", "kunde": "", "stunden": 0.5,
                        "verrechenbare_stunden": 0.0},
                    10: {"projekt": "Finance & Admin", "kunde": "", "stunden": 2.0,
                         "verrechenbare_stunden": 0.0},
                }
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert ergebnis.ohne_entwurf == []
        assert ergebnis.intern == ["Strategy & Planning", "Finance & Admin"]

    def test_ein_vertragsprojekt_bleibt_auch_ohne_verrechenbare_stunden_gemeldet(self):
        """Ein Fixvertrag verbraucht auch unverrechenbare Zeit — dort entscheidet
        der Vertrag und nicht das Häkchen in Toggl."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                stunden={7: {"projekt": "AGG Beratung", "kunde": "AGG", "stunden": 12.0,
                             "verrechenbare_stunden": 0.0}}
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert [e["vertrag"] for e in ergebnis.ohne_entwurf] == ["beratung"]
        assert ergebnis.intern == []

    def test_entwurf_mit_fremdem_datum_wird_nicht_beurteilt_sondern_ausgewiesen(self):
        """Eine BFH-Teilrechnung für einen anderen Monat darf nicht als falsch
        datiert gelten — aber auch nicht lautlos verschwinden."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(entwuerfe=[entwurf(datum="2026-10-31")]),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert ergebnis.ergebnisse == []
        assert [e["datum"] for e in ergebnis.ausserhalb] == ["2026-10-31"]

    def test_entwurf_ohne_datum_wird_geprueft_und_nicht_weggefiltert(self):
        """Sonst entkäme ausgerechnet die Rechnung ohne Datum der Datumsregel."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(entwuerfe=[entwurf(datum=None)], positionen={1: POSITIONEN}),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert len(ergebnis.ergebnisse) == 1
        befunde = {b.regel: b for b in ergebnis.ergebnisse[0].befunde}
        assert befunde["rechnungsdatum"].zustand is pr.Zustand.OFFEN

    def test_falsches_datum_innerhalb_des_monats_faellt_auf(self):
        """Der Zweck der Regel: der 15. statt des Monatsletzten verschiebt den
        Umsatz nicht, das Datum ist trotzdem falsch."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(entwuerfe=[entwurf(datum="2026-08-15")], positionen={1: POSITIONEN}),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        befunde = {b.regel: b for b in ergebnis.ergebnisse[0].befunde}
        assert befunde["rechnungsdatum"].zustand is pr.Zustand.FALSCH

    def test_ohne_uebertrag_wird_nicht_null_angenommen(self):
        """Ein angenommener Übertrag von null ist eine Behauptung über einen
        Vertrag, keine fehlende Angabe."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                entwuerfe=[entwurf()], positionen={1: POSITIONEN},
                stunden={7: {"projekt": "AGG Beratung", "kunde": "AGG", "stunden": 22.0}},
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        befunde = {b.regel: b for b in ergebnis.ergebnisse[0].befunde}
        assert befunde["uebertrag"].zustand is pr.Zustand.OFFEN

    def test_vor_dem_erzeugen_ist_die_iban_offen_und_nicht_gruen(self):
        """Die IBAN steht erst im PDF. «Nicht feststellbar» darf nicht wie
        «stimmt» aussehen."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(entwuerfe=[entwurf()], positionen={1: POSITIONEN}),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        befunde = {b.regel: b for b in ergebnis.ergebnisse[0].befunde}
        assert befunde["iban"].zustand is pr.Zustand.OFFEN
        assert befunde["leistungsrapport"].zustand is pr.Zustand.OFFEN

    def test_auffaelligkeiten_der_positionen_reisen_mit(self):
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                entwuerfe=[entwurf()],
                positionen={1: [
                    {"positionsart": "custom", "pos": 1, "text": "20h/Monat fix inklusive",
                     "amount": "20.0", "unit_id": 2, "unit_price": "200.0"},
                    {"positionsart": "custom", "pos": 2, "text": "25h/Monat fix inklusive",
                     "amount": "25.0", "unit_id": 2, "unit_price": "200.0"},
                ]},
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert "RE-1001" in ergebnis.auffaelligkeiten

    def test_massstab_kommt_sonst_aus_den_vorgaben(self):
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(entwuerfe=[entwurf()], positionen={1: POSITIONEN}),
            bestand(), jahr=2026, monat=8,
        )
        befunde = {b.regel: b for b in ergebnis.ergebnisse[0].befunde}
        assert befunde["iban"].zustand is pr.Zustand.OFFEN  # erwartet, aber nicht lesbar

    def test_abrufehler_bleiben_im_ergebnis_stehen(self):
        """Ein Teilausfall darf nicht wie «nichts gefunden» aussehen."""
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(fehler=["Toggl-Stunden: TimeoutException: …"]),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert ergebnis.fehler and "Toggl" in ergebnis.fehler[0]

    def test_zaehlung_trennt_versandbereit_von_blockiert(self):
        ergebnis = lauf.auswerten(
            lauf.Rohdaten(
                entwuerfe=[
                    entwurf(),
                    entwurf(rechnung_id=2, nummer="RE-1002", titel="CAS IBD",
                            netto=3000.0, steuer=243.0, brutto=3243.0),
                ],
                positionen={1: POSITIONEN, 2: []},
            ),
            bestand(), jahr=2026, monat=8, massstab=MASSSTAB,
        )
        assert ergebnis.versandbereit + ergebnis.blockiert == len(ergebnis.ergebnisse) == 2
        bfh = next(e for e in ergebnis.ergebnisse if e.rechnung == "RE-1002")
        # Der BFH-Vertrag ist steuerfrei; 8.1 % darauf hält die Rechnung zurück.
        assert {b.regel: b.zustand for b in bfh.befunde}["mwst"] is pr.Zustand.FALSCH
        assert not bfh.versandbereit


# ── Abrufen ──────────────────────────────────────────────


class FakeBexio:
    def __init__(self, invoices=None, positions=None, kaputt=False, auftraege=None):
        self._invoices = invoices or []
        self._positions = positions or {}
        self._kaputt = kaputt
        self._auftraege = auftraege or []
        self.aufrufe: list[int] = []

    async def search_invoices(self, status=None, from_date=None, to_date=None, contact_id=None):
        # ``entwurf`` im Lauf, ohne Status beim Lesen des Vormonatsübertrags:
        # dort zählt nicht der Zustand, sondern das Datum.
        assert status in ("entwurf", None)
        # Der Statusabruf darf **nie** nach Datum filtern -- sonst entkäme die
        # falsch datierte Rechnung genau der Regel, die sie finden soll. Der
        # Abruf für die Auftragslücke fragt umgekehrt nur nach dem Datum.
        if status is not None:
            assert from_date is None and to_date is None
        return self._invoices

    async def alle_auftraege(self, seite=500):
        return self._auftraege

    async def list_contacts(self, limit=None, offset=None, **kwargs):
        return [{"id": 5, "name_1": "AGG", "name_2": ""}] if offset in (0, None) else []

    async def get_invoice_positions(self, invoice_id):
        self.aufrufe.append(invoice_id)
        if self._kaputt:
            raise RuntimeError("500 vom Server")
        return self._positions.get(invoice_id, [])


class FakeToggl:
    def __init__(self, gruppen=None, kaputt=False):
        self._gruppen = gruppen or []
        self._kaputt = kaputt
        self.spannen: list[tuple[str, str]] = []

    async def list_projects(self, active=None):
        return [{"id": 7, "name": "AGG Beratung", "client_id": 3, "active": True}]

    async def list_clients(self, status=None):
        return [{"id": 3, "name": "AGG"}]

    async def list_tags(self, ws=None):
        # Die Tags tragen die Verrechnungsart.
        return [{"id": 8280154, "name": "Fixpreis"}]

    async def search_all_time_entries(self, ws, von, bis):
        if self._kaputt:
            raise RuntimeError("Zeitüberschreitung")
        self.spannen.append((von, bis))
        return self._gruppen


ROHRECHNUNG = {
    "id": 1, "document_nr": "RE-1001", "is_valid_from": "2026-08-31",
    "contact_id": 5, "title": "AGG Beratung August",
    "total_net": "4000.00", "total_taxes": "324.00", "total": "4324.00",
}

GRUPPE = {
    "project_id": 7, "description": "Beratung", "billable": True,
    "time_entries": [{"id": 1, "start": "2026-08-12T09:00:00+02:00", "seconds": 7200}],
}


@pytest.mark.asyncio
class TestAbrufen:
    async def test_holt_entwuerfe_positionen_und_stunden(self):
        bexio = FakeBexio([ROHRECHNUNG], {1: POSITIONEN})
        toggl = FakeToggl([GRUPPE])
        roh = await lauf.abrufen(bexio, toggl, jahr=2026, monat=8)

        assert toggl.spannen == [("2026-08-01", "2026-08-31")]
        assert [e["nummer"] for e in roh.entwuerfe] == ["RE-1001"]
        assert roh.entwuerfe[0]["netto"] == 4000.0
        assert roh.positionen[1] == POSITIONEN
        assert roh.stunden[7]["stunden"] == 2.0
        assert roh.fehler == []

    async def test_toggl_ausfall_liefert_die_entwuerfe_trotzdem(self):
        """Fehlerisolation: eine unerreichbare Quelle reisst den Lauf nicht."""
        roh = await lauf.abrufen(
            FakeBexio([ROHRECHNUNG], {1: POSITIONEN}),
            FakeToggl(kaputt=True), jahr=2026, monat=8,
        )
        assert len(roh.entwuerfe) == 1
        assert roh.stunden == {}
        assert any("Toggl" in f for f in roh.fehler)

    async def test_fehlgeschlagener_positionsabruf_nennt_die_rechnung(self):
        roh = await lauf.abrufen(
            FakeBexio([ROHRECHNUNG], kaputt=True), FakeToggl(), jahr=2026, monat=8
        )
        assert roh.positionen == {}
        assert any("RE-1001" in f for f in roh.fehler)

    async def test_ohne_entwuerfe_werden_keine_positionen_geholt(self):
        bexio = FakeBexio([])
        roh = await lauf.abrufen(bexio, FakeToggl(), jahr=2026, monat=8)
        assert bexio.aufrufe == [] and roh.entwuerfe == []


# ── Der Übertragsstand zu Monatsbeginn ───────────────────


def julirechnung(**abweichung) -> dict:
    grund = {
        "id": 9, "document_nr": "RE-0900", "is_valid_from": "2026-07-31",
        "contact_id": 5, "title": "AGG Beratung Juli",
        "total_net": "4000.00", "total_taxes": "324.00", "total": "4324.00",
    }
    return {**grund, **abweichung}


def uebertragssatz(stunden: str) -> dict:
    return {
        "positionsart": "text", "pos": 2,
        "text": f"Per 31.07.2026 sind {stunden}h verrechnet aber noch nicht "
                "geleistet, welche dem nächsten Monat angerechnet werden.",
    }


def buchung(datum: str, sekunden: int = 3600) -> dict:
    return {
        "project_id": 7, "description": "Beratung", "billable": True,
        "time_entries": [{"id": 1, "start": f"{datum}T09:00:00+02:00",
                          "seconds": sekunden}],
    }


@pytest.mark.asyncio
class TestUebertragZuMonatsbeginn:
    """Der Startwert kommt aus dem Bestand, nicht aus ``historie.json``.

    Jene Datei trägt bekannte Fehler. Die Rechnung dagegen sagt selbst, was
    offen bleibt — und was sie sagt, hat der Kunde gesehen.
    """

    async def test_liest_den_satz_der_vormonatsrechnung(self):
        bexio = FakeBexio([julirechnung()], {9: [uebertragssatz("3.5")]})
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8, toggl=FakeToggl(), workspace=1
        )
        assert stand.staende == {"beratung": 3.5}
        assert stand.herkunft == {"beratung": "2026-07"}
        assert stand.hinweise == []

    async def test_ohne_satz_ist_der_uebertrag_null(self):
        """«Nichts zu übertragen» und «kein Satz» sind dasselbe."""
        bexio = FakeBexio([julirechnung()], {9: POSITIONEN})
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8, toggl=FakeToggl(), workspace=1
        )
        assert stand.staende == {"beratung": 0.0}

    async def test_belegte_pause_traegt_den_stand_weiter(self):
        """Der echte Fall «Digitale Evolution mit KI»: Juli ist Sommerpause.

        Kein Monat, keine Rechnung, keine Leistung — und damit auch kein Mangel.
        Ein Dauerbefund an dieser Stelle lehrte, über die Liste hinwegzulesen.
        """
        bexio = FakeBexio(
            [julirechnung(is_valid_from="2026-06-30")], {9: [uebertragssatz("3.5")]}
        )
        toggl = FakeToggl([])  # kein Eintrag im Juli
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8, toggl=toggl, workspace=1
        )
        assert stand.staende == {"beratung": 3.5}
        assert stand.herkunft == {"beratung": "2026-06"}
        assert stand.hinweise == []
        assert toggl.spannen == [("2026-07-01", "2026-07-31")]

    async def test_stunden_ohne_rechnung_sind_eine_luecke_kein_stand(self):
        """Die Gegenprobe: im übersprungenen Monat wurde gearbeitet.

        Dann fehlt eine Rechnung, und der Stand ist nicht belegt. Ihn trotzdem
        weiterzutragen ergäbe eine plausible falsche Zahl statt einer Meldung.
        """
        bexio = FakeBexio(
            [julirechnung(is_valid_from="2026-06-30")], {9: [uebertragssatz("3.5")]}
        )
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8,
            toggl=FakeToggl([buchung("2026-07-14", 7200)]), workspace=1,
        )
        assert stand.staende == {}
        assert any("2026-07" in h and "2h" in h for h in stand.hinweise)

    async def test_ohne_toggl_bleibt_eine_pause_unbelegt(self):
        """Eine unbelegte Annahme ist schlechter als eine fehlende Angabe."""
        bexio = FakeBexio(
            [julirechnung(is_valid_from="2026-06-30")], {9: [uebertragssatz("3.5")]}
        )
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8
        )
        assert stand.staende == {}
        assert any("nicht belegt" in h for h in stand.hinweise)

    async def test_toggl_ausfall_bei_der_pausenpruefung_wird_gemeldet(self):
        bexio = FakeBexio(
            [julirechnung(is_valid_from="2026-06-30")], {9: [uebertragssatz("3.5")]}
        )
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8,
            toggl=FakeToggl(kaputt=True), workspace=1,
        )
        assert stand.staende == {}
        assert any("Toggl nicht erreichbar" in h for h in stand.hinweise)

    async def test_gar_keine_rechnung_im_rueckblick_wird_genannt(self):
        stand = await lauf.uebertrag_zu_monatsbeginn(
            FakeBexio([]), bestand(), jahr=2026, monat=8,
            toggl=FakeToggl(), workspace=1, rueckblick=3,
        )
        assert stand.staende == {}
        assert any("AGG Beratung" in h and "3" in h for h in stand.hinweise)

    async def test_beendeter_vertrag_bekommt_seinen_letzten_stand(self):
        """Der Fall Sympholio: endet per 31.08., die Augustrechnung läuft noch.

        Ein ruhender Vertrag aus der Suche zu nehmen sähe im Rücklauf wie ein
        unbelegter Übertrag aus — und blockierte die letzte Rechnung.
        """
        b = bestand()
        b.vertraege["beratung"] = replace(b.vertraege["beratung"], ruhend=True)
        bexio = FakeBexio([julirechnung()], {9: [uebertragssatz("3.5")]})
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, b, jahr=2026, monat=8, toggl=FakeToggl(), workspace=1
        )
        assert stand.staende == {"beratung": 3.5}

    async def test_ruhender_vertrag_ohne_rechnung_wird_nicht_bemaengelt(self):
        b = bestand()
        b.vertraege["beratung"] = replace(b.vertraege["beratung"], ruhend=True)
        stand = await lauf.uebertrag_zu_monatsbeginn(
            FakeBexio([]), b, jahr=2026, monat=8, toggl=FakeToggl(), workspace=1
        )
        assert stand.hinweise == []

    async def test_jahreswechsel_greift_auf_den_dezember(self):
        bexio = FakeBexio(
            [julirechnung(is_valid_from="2025-12-31")], {9: [uebertragssatz("2.0")]}
        )
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=1, toggl=FakeToggl(), workspace=1
        )
        assert stand.staende == {"beratung": 2.0}

    async def test_nicht_uebertragbare_vertraege_werden_uebergangen(self):
        """Für sie gibt es keinen Stand, und einer wäre irreführend."""
        bexio = FakeBexio(
            [julirechnung(title="CAS IBD Juli")], {9: [uebertragssatz("3.5")]}
        )
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8, toggl=FakeToggl(), workspace=1
        )
        assert "cas" not in stand.staende
        assert not any("CAS" in h for h in stand.hinweise)

    async def test_zwei_widersprechende_rechnungen_ergeben_keinen_stand(self):
        bexio = FakeBexio(
            [julirechnung(), julirechnung(id=10, document_nr="RE-0901")],
            {9: [uebertragssatz("3.5")], 10: [uebertragssatz("6.0")]},
        )
        stand = await lauf.uebertrag_zu_monatsbeginn(
            bexio, bestand(), jahr=2026, monat=8, toggl=FakeToggl(), workspace=1
        )
        assert "beratung" not in stand.staende
        assert "beratung" not in stand.herkunft
        assert any("verschiedene Überträge" in h for h in stand.hinweise)

    async def test_ausfall_beim_lesen_wird_gemeldet_nicht_verschluckt(self):
        class Kaputt:
            async def search_invoices(self, **kwargs):
                raise RuntimeError("503 vom Server")

        stand = await lauf.uebertrag_zu_monatsbeginn(
            Kaputt(), bestand(), jahr=2026, monat=8
        )
        assert stand.staende == {}
        assert any("503" in h for h in stand.hinweise)


# ── Auftragslücke ────────────────────────────────────────


def auftrag(**abweichung):
    """Eine Auftragszeile, wie ``bexio.auftraege.auftragszeilen`` sie liefert."""
    from auftraege import Auftrag

    grund = dict(
        auftrag_id=1, nummer="AU-00001", titel="AGG Beratung", kunden_id=5,
        datum="2026-01-31", status="offen", laeuft=True, wiederkehrend=True,
        total=4000.0, waehrung_id=1,
    )
    return Auftrag(**{**grund, **abweichung})


class TestAuftragslage:
    def test_laufender_dauerauftrag_ohne_rechnung_faellt_auf(self):
        befunde = lauf.auftragslage(
            [auftrag()], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden={"beratung"},
        )
        assert len(befunde) == 1
        assert befunde[0]["art"] == "auftrag_ohne_rechnung"
        assert befunde[0]["vertrag"] == "beratung"
        assert befunde[0]["stunden_vorhanden"] is True

    def test_mit_rechnung_im_monat_ist_nichts_zu_melden(self):
        befunde = lauf.auftragslage(
            [auftrag()], bestand(),
            vertraege_mit_rechnung={"beratung"}, vertraege_mit_stunden={"beratung"},
        )
        assert befunde == []

    def test_einmaliger_auftrag_erwartet_keine_monatsrechnung(self):
        """Ein Meilensteinauftrag monatlich anzumahnen hiesse, eine Erwartung
        zu erfinden, die nie vereinbart war."""
        befunde = lauf.auftragslage(
            [auftrag(wiederkehrend=False)], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
        )
        assert befunde == []

    def test_abgeschlossener_auftrag_mit_stunden_verlangt_verlaengerung(self):
        befunde = lauf.auftragslage(
            [auftrag(status="abgeschlossen", laeuft=False)], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden={"beratung"},
        )
        assert [b["art"] for b in befunde] == ["auftrag_abgelaufen"]
        assert "Stunden" in befunde[0]["grund"]

    def test_ruhender_vertrag_mit_laufendem_dauerauftrag(self):
        """Sympholio: Abo beendet, Auftrag AU-00026 läuft weiter.

        Gemeldet wird der offene Auftrag, nicht die fehlende Rechnung. Die eine
        Meldung ist erledigt, sobald der Auftrag geschlossen ist; die andere
        stünde jeden Monat neu da.
        """
        b = bestand()
        b.vertraege["beratung"] = replace(b.vertraege["beratung"], ruhend=True)
        befunde = lauf.auftragslage(
            [auftrag()], b,
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
        )
        assert [x["art"] for x in befunde] == ["auftrag_laeuft_weiter"]

    def test_ruhender_vertrag_braucht_keine_verlaengerung(self):
        """Integrix: Projekt beendet, Auftrag abgeschlossen — das ist die
        Übereinstimmung und nicht der Mangel."""
        b = bestand()
        b.vertraege["beratung"] = replace(b.vertraege["beratung"], ruhend=True)
        befunde = lauf.auftragslage(
            [auftrag(status="abgeschlossen", laeuft=False)], b,
            vertraege_mit_rechnung={"beratung"}, vertraege_mit_stunden={"beratung"},
        )
        assert befunde == []

    def test_abgeschlossener_auftrag_ohne_aktivitaet_schweigt(self):
        """Der Normalfall des Altbestands: 23 erledigte Aufträge seit 2020.
        Jeder davon als Befund wäre die Liste unbrauchbar."""
        befunde = lauf.auftragslage(
            [auftrag(status="abgeschlossen", laeuft=False)], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
        )
        assert befunde == []

    def test_laufender_auftrag_ohne_vertrag_ist_ein_stammdatenmangel(self):
        befunde = lauf.auftragslage(
            [auftrag(titel="Etwas ganz Neues")], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
        )
        assert [b["art"] for b in befunde] == ["auftrag_ohne_vertrag"]
        assert befunde[0]["titel"] == "Etwas ganz Neues"

    def test_erledigter_auftrag_ohne_vertrag_ist_geschichte(self):
        befunde = lauf.auftragslage(
            [auftrag(titel="Startup Academy 2020", laeuft=False)], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
        )
        assert befunde == []

    def test_quartalsvertrag_wird_im_zwischenmonat_nicht_angemahnt(self):
        """Der April 2026 bei ImpulsKöniz: die Juni-Rechnung deckt ihn ab."""
        from app.services.debitorenvertraege import Rhythmus

        b = bestand()
        b.vertraege["beratung"] = replace(
            b.vertraege["beratung"], rhythmus=Rhythmus.QUARTALSWEISE
        )
        befunde = lauf.auftragslage(
            [auftrag()], b,
            vertraege_mit_rechnung=set(), vertraege_mit_stunden={"beratung"},
            bis=date(2026, 4, 30),
        )
        assert befunde == []

    def test_quartalsvertrag_wird_im_quartalsmonat_angemahnt(self):
        from app.services.debitorenvertraege import Rhythmus

        b = bestand()
        b.vertraege["beratung"] = replace(
            b.vertraege["beratung"], rhythmus=Rhythmus.QUARTALSWEISE
        )
        befunde = lauf.auftragslage(
            [auftrag()], b,
            vertraege_mit_rechnung=set(), vertraege_mit_stunden={"beratung"},
            bis=date(2026, 6, 30),
        )
        assert [x["art"] for x in befunde] == ["auftrag_ohne_rechnung"]

    def test_nach_aufwand_ohne_stunden_ist_kein_befund(self):
        """AKV-Bot: Rechnung nur, wenn Leistung erbracht wurde."""
        from app.services.debitorenvertraege import Rhythmus

        b = bestand()
        b.vertraege["beratung"] = replace(
            b.vertraege["beratung"], rhythmus=Rhythmus.NACH_AUFWAND
        )
        ohne = lauf.auftragslage(
            [auftrag()], b,
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
            bis=date(2026, 4, 30),
        )
        mit = lauf.auftragslage(
            [auftrag()], b,
            vertraege_mit_rechnung=set(), vertraege_mit_stunden={"beratung"},
            bis=date(2026, 4, 30),
        )
        assert ohne == []
        assert [x["art"] for x in mit] == ["auftrag_ohne_rechnung"]

    def test_ein_spaeter_erfasster_auftrag_mahnt_nicht_rueckwirkend(self):
        """Der April 2026 mahnte drei Aufträge an, die erst im Juni entstanden."""
        befunde = lauf.auftragslage(
            [auftrag(datum="2026-06-14")], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
            bis=date(2026, 4, 30),
        )
        assert befunde == []

    def test_am_periodenende_erfasster_auftrag_zaehlt_noch(self):
        befunde = lauf.auftragslage(
            [auftrag(datum="2026-04-30")], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
            bis=date(2026, 4, 30),
        )
        assert [b["art"] for b in befunde] == ["auftrag_ohne_rechnung"]

    def test_auftrag_ohne_lesbares_datum_bleibt_in_der_pruefung(self):
        """Einen Mangel mit einem zweiten zuzudecken wäre die schlechtere Wahl."""
        befunde = lauf.auftragslage(
            [auftrag(datum=None)], bestand(),
            vertraege_mit_rechnung=set(), vertraege_mit_stunden=set(),
            bis=date(2026, 4, 30),
        )
        assert [b["art"] for b in befunde] == ["auftrag_ohne_rechnung"]

    def test_ein_laufender_auftrag_genuegt_gegen_die_verlaengerungsmeldung(self):
        """Zwei Aufträge auf denselben Vertrag: der alte erledigt, der neue läuft."""
        befunde = lauf.auftragslage(
            [auftrag(nummer="AU-1", laeuft=False, status="abgeschlossen"),
             auftrag(nummer="AU-2")],
            bestand(),
            vertraege_mit_rechnung={"beratung"}, vertraege_mit_stunden={"beratung"},
        )
        assert befunde == []


def test_unbekannter_auftragsstatus_gilt_als_laufend():
    """Die vorsichtige Richtung: ein übersehener laufender Auftrag kostet Geld,
    ein gemeldeter abgeschlossener kostet einen Blick."""
    from auftraege import status_beschriften

    beschriftung, laeuft = status_beschriften(99)
    assert beschriftung == "unbekannt_99"
    assert laeuft is True


def test_auftragszeilen_melden_unbekannte_status():
    from auftraege import auftragszeilen

    zeilen, befund = auftragszeilen([
        {"id": 1, "document_nr": "AU-1", "kb_item_status_id": 5},
        {"id": 2, "document_nr": "AU-2", "kb_item_status_id": 99},
    ])
    assert len(zeilen) == 2
    assert befund["unbekannte_status"] == {"unbekannt_99": 1}


# ── Gegen die echte Stammdatei ───────────────────────────


def test_laengster_treffer_gewinnt_gegen_den_echten_bestand():
    """Der teuerste Gleichstand der Stammdatei.

    «CAS IBD Dozent» ist eine Teilzeichenkette von «CAS IBD Dozent HS26». Ohne
    die Regel «längster Treffer gewinnt» entschiede die Reihenfolge im
    Wörterbuch, welcher Vertrag zugeordnet wird -- und zwar ohne Meldung. Zwei
    Verträge desselben Kunden mit unterschiedlichen Stunden, vertauscht.
    """
    from app.services.debitorenvertraege import laden

    pfad = Path(__file__).resolve().parents[3] / "docs" / "debitorenvertraege.yaml"
    echt, _ = laden(pfad)

    lang = echt.finden("CAS IBD Dozent HS26 — Teilrechnung 2")
    kurz = echt.finden("CAS IBD Dozent")
    assert lang is not None and lang.schluessel == "cas-ibd-hs26"
    assert kurz is not None and kurz.schluessel == "cas-ibd-dozent"


def test_rechnungstitel_mit_monatszusatz_findet_den_vertrag():
    """Toggl nennt das Projekt genau, Bexio hängt den Monat an den Titel."""
    from app.services.debitorenvertraege import laden

    pfad = Path(__file__).resolve().parents[3] / "docs" / "debitorenvertraege.yaml"
    echt, _ = laden(pfad)

    assert echt.finden("DigiAGG2426 August 2026").schluessel == "digiagg2426"
    assert echt.finden("Sympholio 08/2026").schluessel == "sympholio"
    assert echt.finden("Irgendein neues Projekt") is None


def test_die_echte_datei_traegt_einen_ibanpraefix():
    """Ohne ihn entfiele die IBAN-Regel stillschweigend für alle Rechnungen."""
    from app.services.debitorenvertraege import laden

    pfad = Path(__file__).resolve().parents[3] / "docs" / "debitorenvertraege.yaml"
    echt, _ = laden(pfad)
    assert (echt.vorgaben or {}).get("iban_praefix")

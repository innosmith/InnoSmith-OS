"""Tests für den Positionsvorschlag.

Zwei Dinge stehen hier auf dem Spiel, und nur eines davon ist offensichtlich.

Das offensichtliche: die richtige Zahl in der richtigen Zeile. Das andere: dass
bei jeder Unklarheit **nichts** vorgeschlagen wird. Ein Vorschlag, der in vier
von fünf Fällen stimmt, wird nach dem dritten Mal ungelesen bestätigt — und der
fünfte geht als Rechnung zur Kundschaft.
"""

from __future__ import annotations

from datetime import date

from app.services import debitoren_positionen as dp
from app.services import debitoren_uebertrag as ue
from app.services import debitoren_vorschlag as vs
from app.services.debitorenvertraege import Kunde, Vertragsdaten

KUNDE = Kunde(schluessel="k", empfaenger="re@example.ch")
STICHTAG = date(2026, 8, 31)


def vertrag(**abweichung) -> Vertragsdaten:
    grund = dict(schluessel="p", bezeichnung="Projekt", kunde=KUNDE, mwst=8.1)
    return Vertragsdaten(**{**grund, **abweichung})


def stundenzeile(menge: str, **abweichung) -> dict:
    grund = {
        "id": 11, "positionsart": "custom", "pos": 1, "unit_id": 2,
        "amount": menge, "unit_price": "250.000000", "tax_id": 36,
        "text": "<strong>OnboardingQS</strong><br />Effektiver Aufwand",
    }
    return {**grund, **abweichung}


def pauschalzeile(stunden: str = "14", preis: str = "3400.00") -> dict:
    return {
        "id": 21, "positionsart": "custom", "pos": 1, "unit_id": 1,
        "amount": "1.000000", "unit_price": preis, "tax_id": 36,
        "text": f"Unterst&uuml;tzung mit einer monatlichen Kapazit&auml;t von:"
                f"<br />- {stunden}h fix inklusive",
    }


def zusatzzeile(menge: str) -> dict:
    return {
        "id": 22, "positionsart": "custom", "pos": 2, "unit_id": 2,
        "amount": menge, "unit_price": "200.000000", "tax_id": 36,
        "text": "Variable Zusatzstunden",
    }


def uebertragszeile(stunden: str, datum: str = "31.08.2026") -> dict:
    return {
        "id": 23, "positionsart": "text", "pos": None,
        "text": f"Per {datum} sind {stunden}h verrechnet aber noch nicht "
                "geleistet, welche dem n&auml;chsten Monat angerechnet werden.",
    }


def vorschlag(positionen, *, v=None, geleistet=None, uebertrag=None) -> vs.Vorschlag:
    return vs.vorschlagen(
        vertrag=v or vertrag(),
        rechnung="RE-1001",
        deutung=dp.deuten(positionen),
        geleistet=geleistet,
        uebertrag_vormonat=uebertrag,
        stichtag=STICHTAG,
        formel=ue.Formel.KAPAZITAET,
    )


# ── Abrechnung nach Aufwand ──────────────────────────────


class TestNachAufwand:
    """Der Fall, den der Altbestand gar nicht bediente.

    Die Stunden standen in Toggl, die Zahl schrieb der Mensch ab. Im August
    2026 betraf das fünf von elf Rechnungen.
    """

    def test_die_stundenzeile_bekommt_die_monatsstunden(self):
        v = vorschlag([stundenzeile("19.000000")], geleistet=35.25)
        assert len(v.aenderungen) == 1
        a = v.aenderungen[0]
        assert (a.position_id, a.feld, a.alt, a.neu) == (11, "amount", "19", "35.25")
        assert a.handlung == "aendern" and a.positionsart == "custom"

    def test_der_stundensatz_bleibt_unangetastet(self):
        """Der teuerste mögliche Fehler: der Altbestand setzte notfalls 200.

        Bei OnboardingQS zu 250 CHF wären das 50 CHF je Stunde Schaden, ohne
        Meldung — und auf einer Rechnung, die hinausgeht.
        """
        a = vorschlag([stundenzeile("19.000000")], geleistet=35.25).aenderungen[0]
        assert a.nutzdaten["unit_price"] == "250.000000"
        assert a.nutzdaten["tax_id"] == 36
        assert a.nutzdaten["amount"] == "35.25"

    def test_stimmende_zeile_erzeugt_keine_aenderung(self):
        v = vorschlag([stundenzeile("40.000000")], geleistet=40.0)
        assert v.aenderungen == [] and v.hindernisse == []
        assert not v.offen

    def test_ohne_stunden_wird_nicht_genullt_sondern_gemeldet(self):
        """«Keine Leistung» heisst «keine Rechnung», nicht «Rechnung über null»."""
        v = vorschlag([stundenzeile("19.000000")], geleistet=0.0)
        assert v.aenderungen == []
        assert any("keine Rechnung erzeugt" in h for h in v.hindernisse)

    def test_ohne_stundenzeile_wird_keine_angelegt(self):
        """Anlegen hiesse, den Stundensatz zu erfinden."""
        zeile = stundenzeile("1.0", unit_id=1, text="Pauschale Umsetzung")
        v = vorschlag([zeile], geleistet=12.0)
        assert v.aenderungen == []
        assert any("Stundensatz zu erfinden" in h for h in v.hindernisse)

    def test_mehrere_stundenzeilen_bleiben_von_hand(self):
        """Der echte Fall «CAS TCM FS26 Lektionen»: drei Module, drei Zeilen.

        Wie sich die Stunden darauf verteilen, weiss nur der Mensch. Eine
        Maschine, die hier wählt, hat meistens recht — und schweigt im
        Ausnahmefall.
        """
        v = vorschlag(
            [stundenzeile("8.0", id=11, pos=1, text="Modul 15"),
             stundenzeile("24.0", id=12, pos=2, text="Modul 20 Review"),
             stundenzeile("8.0", id=13, pos=3, text="Modul 20 Pitch")],
            geleistet=17.25,
        )
        assert v.aenderungen == []
        assert any("3 Stundenzeilen" in h for h in v.hindernisse)

    def test_ohne_toggl_stunden_gibt_es_keinen_vorschlag(self):
        v = vorschlag([stundenzeile("19.0")], geleistet=None)
        assert v.aenderungen == []
        assert any("keine Toggl-Stunden" in h for h in v.hindernisse)

    def test_vertrag_ohne_stundenpruefung_bleibt_unberuehrt(self):
        """Die BFH-Lehrgänge: der Preis ist vereinbart, nicht gerechnet."""
        v = vorschlag(
            [stundenzeile("8.0")], v=vertrag(stunden_pruefen=False), geleistet=17.25
        )
        assert v.aenderungen == [] and v.hindernisse == []


# ── Monatsabo mit Fixstunden ─────────────────────────────


class TestPauschale:
    """Die Hälfte, die ``D2_updateDraftRechnungen.py`` schon konnte."""

    FIX = dict(fix_stunden=14.0, uebertragbar=True)

    def test_zusatzstunden_folgen_der_kapazitaet(self):
        """14h fix, 3h Übertrag, 20h geleistet -> 3h über der Kapazität."""
        v = vorschlag(
            [pauschalzeile(), zusatzzeile("0.0")],
            v=vertrag(**self.FIX), geleistet=20.0, uebertrag=3.0,
        )
        mengen = [a for a in v.aenderungen if a.feld == "amount"]
        assert [(a.position_id, a.neu) for a in mengen] == [(22, "3")]

    def test_zusatzzeile_wird_entfernt_wenn_nichts_uebersteht(self):
        v = vorschlag(
            [pauschalzeile(), zusatzzeile("2.0")],
            v=vertrag(**self.FIX), geleistet=10.0, uebertrag=0.0,
        )
        entfernen = [a for a in v.aenderungen if a.handlung == "entfernen"]
        assert [a.position_id for a in entfernen] == [22]

    def test_fehlende_zusatzzeile_wird_gemeldet_nicht_angelegt(self):
        v = vorschlag(
            [pauschalzeile()], v=vertrag(**self.FIX), geleistet=20.0, uebertrag=0.0
        )
        assert v.aenderungen == []
        assert any("Variable Zusatzstunden" in h for h in v.hindernisse)

    def test_uebertragssatz_wird_im_wortlaut_fortgeschrieben(self):
        """Der Satz, den die Kundschaft liest — und den der Folgemonat liest."""
        v = vorschlag(
            [pauschalzeile(), uebertragszeile("7", datum="31.07.2026")],
            v=vertrag(**self.FIX), geleistet=10.0, uebertrag=3.0,
        )
        text = [a for a in v.aenderungen if a.feld == "text"]
        assert len(text) == 1
        assert text[0].neu == (
            "Per 31.08.2026 sind 7h verrechnet aber noch nicht geleistet, "
            "welche dem nächsten Monat angerechnet werden."
        )
        # Und der Folgemonat liest ihn wieder: die Deutung muss ihn erkennen.
        gelesen = dp.deuten([{"positionsart": "text", "text": text[0].neu}])
        assert gelesen.uebertrag_angabe == 7.0

    def test_gleicher_satz_erzeugt_keine_aenderung(self):
        """Auch mit HTML-Entitäten im Bestand — sonst schriebe jeder Lauf."""
        v = vorschlag(
            [pauschalzeile(), uebertragszeile("7")],
            v=vertrag(**self.FIX), geleistet=10.0, uebertrag=3.0,
        )
        assert [a for a in v.aenderungen if a.feld == "text"] == []

    def test_uebertragssatz_verschwindet_wenn_nichts_offen_bleibt(self):
        v = vorschlag(
            [pauschalzeile(), uebertragszeile("7")],
            v=vertrag(**self.FIX), geleistet=17.0, uebertrag=3.0,
        )
        entfernen = [a for a in v.aenderungen if a.positionsart == "text"]
        assert [a.handlung for a in entfernen] == ["entfernen"]

    def test_ohne_belegten_vormonatsuebertrag_wird_nichts_vorgeschlagen(self):
        """Der Fall «Digitale Evolution mit KI» nach der Sommerpause.

        Mit null zu rechnen ergäbe eine plausible falsche Zahl — genau die
        Fehlerart, die dieser Prozess nicht erzeugen darf.
        """
        v = vorschlag(
            [pauschalzeile(), zusatzzeile("0.0")],
            v=vertrag(**self.FIX), geleistet=20.0, uebertrag=None,
        )
        assert v.aenderungen == []
        assert any("nicht belegt" in h for h in v.hindernisse)

    def test_nicht_uebertragbarer_vertrag_bekommt_keinen_satz(self):
        v = vorschlag(
            [pauschalzeile("7"), zusatzzeile("0.25")],
            v=vertrag(fix_stunden=7.0, uebertragbar=False),
            geleistet=7.25, uebertrag=None,
        )
        assert all(a.positionsart != "text" for a in v.aenderungen)
        assert v.hindernisse == []

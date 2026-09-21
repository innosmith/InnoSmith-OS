"""Die Rechnungsprüfung vor dem Versand und die zwei Übertragslesarten.

Die Testfälle sind **nicht** aus ``archive/testValidierung.py`` übernommen. Jene
Datei trägt «Test» im Namen, enthält aber keine Testfälle: sie druckt eine
Regelübersicht, zählt Statusmeldungen in der Historie und lässt fünf echte PDFs
durch die Validierung laufen, ohne eine einzige Erwartung zu prüfen. Sie ist ein
Diagnosewerkzeug, kein Prüfnetz. Die Fälle hier sind aus der Fachlogik von
``admin_core`` und ``D2_updateDraftRechnungen`` abgeleitet.
"""

from datetime import date

import pytest

from app.services import debitoren_uebertrag as ue
from app.services.debitoren_pruefung import (
    REGELN,
    Befund,
    Gewicht,
    Massstab,
    Periode,
    Rechnung,
    Vertrag,
    Vertragsart,
    Zustand,
    pruefen,
)
from app.services.debitoren_uebertrag import Formel


def befund(ergebnis, regel: str) -> Befund:
    return next(b for b in ergebnis.befunde if b.regel == regel)


# ── Die beiden Übertragslesarten ─────────────────────────

class TestUebertragsformeln:
    """Wo die Fassungen von D2 und admin_core übereinstimmen und wo nicht."""

    def test_ausserhalb_des_fensters_gleicher_uebertrag(self):
        """Unterhalb und oberhalb der Kapazität sind sich beide einig."""
        for geleistet in (18.0, 30.0):
            beide = ue.vergleichen(fix_stunden=20.0, geleistet=geleistet, uebertrag_vormonat=5.0)
            assert (
                beide[Formel.KAPAZITAET].neuer_uebertrag
                == beide[Formel.VERFALL].neuer_uebertrag
            ), f"bei {geleistet}h geleistet"

    def test_im_fenster_verschiedener_uebertrag(self):
        """``fix < geleistet < fix + Übertrag`` ist der einzige strittige Bereich.

        Bei 20h fix, 5h Übertrag und 22h geleistet trägt D2 3h weiter, während
        admin_core den Übertrag löscht und damit die Rechnung als fehlerhaft
        meldet — obwohl D2 in sich stimmig gerechnet hat.
        """
        assert ue.im_streitfenster(fix_stunden=20.0, geleistet=22.0, uebertrag_vormonat=5.0)
        beide = ue.vergleichen(fix_stunden=20.0, geleistet=22.0, uebertrag_vormonat=5.0)
        assert beide[Formel.KAPAZITAET].neuer_uebertrag == 3.0
        assert beide[Formel.VERFALL].neuer_uebertrag == 0.0

    def test_kein_fenster_ohne_uebertrag(self):
        """Ohne Übertrag aus dem Vormonat gibt es nichts zu streiten."""
        assert not ue.im_streitfenster(fix_stunden=20.0, geleistet=22.0, uebertrag_vormonat=0.0)

    def test_verrechneter_betrag_weicht_auch_ausserhalb_ab(self):
        """Der zweite, nie gemeldete Widerspruch: der Rechnungsbetrag.

        Unterhalb der vereinbarten Stunden stellt D2 die Fixstunden in Rechnung,
        admin_core erwartet die geleisteten abzüglich Übertrag. Bei 20h fix, 0h
        Übertrag und 18h geleistet sind das 20h gegen 18h — bei jedem
        Fixstundenkunden in jedem unterausgelasteten Monat. Gemeldet wurde es nie,
        weil ``validate_invoice`` Differenzen bei Vertragsart A zum Detail
        herabstuft und bei Vertragsart B dieselbe Zahl mit sich selbst vergleicht.
        """
        beide = ue.vergleichen(fix_stunden=20.0, geleistet=18.0, uebertrag_vormonat=0.0)
        assert beide[Formel.KAPAZITAET].verrechnet_erwartet == 20.0
        assert beide[Formel.VERFALL].verrechnet_erwartet == 18.0
        assert beide[Formel.KAPAZITAET].neuer_uebertrag == beide[Formel.VERFALL].neuer_uebertrag

    def test_verfall_gibt_oberhalb_keinen_betrag_vor(self):
        """``admin_core`` liest die Zusatzstunden dort aus der Rechnung.

        Eine Formel zu erfinden, die plausibel aussieht, wäre schlimmer als die
        Lücke zu benennen — sie stünde als Vorschrift da, ohne je eine gewesen zu sein.
        """
        ergebnis = ue.rechnen(
            fix_stunden=20.0, geleistet=30.0, uebertrag_vormonat=5.0, formel=Formel.VERFALL
        )
        assert ergebnis.verrechnet_erwartet is None
        assert ergebnis.zusatz_stunden is None
        assert ergebnis.neuer_uebertrag == 0.0

    def test_verfall_ergibt_unterhalb_negative_zusatzmenge(self):
        """Eine Menge, die auf keiner Rechnung darstellbar ist — sichtbar statt weggerundet."""
        ergebnis = ue.rechnen(
            fix_stunden=20.0, geleistet=18.0, uebertrag_vormonat=5.0, formel=Formel.VERFALL
        )
        assert ergebnis.zusatz_stunden is not None and ergebnis.zusatz_stunden < 0

    def test_rechnen_verlangt_eine_lesart(self):
        """Kein Vorgabewert: keine Lesart darf gewinnen, weil sie zuerst im Code steht."""
        with pytest.raises(TypeError):
            ue.rechnen(fix_stunden=20.0, geleistet=18.0, uebertrag_vormonat=0.0)  # type: ignore[call-arg]

    def test_bestaetigte_lesart_entspricht_den_beiden_antworten(self):
        """Hält den Entscheid vom 20.09.2026 fest, nicht bloss den Namen der Konstanten.

        Bestätigt wurde: bei 18h von 20h vereinbart gehen die **20h** in Rechnung
        (nicht 13h), und bei 22h von 20h plus 5h Übertrag wandern **3h** weiter
        (der Übertrag verfällt nicht). Würde jemand ``BESTAETIGT`` umstellen,
        fiele es hier auf — und nicht erst an einer Kundenrechnung.
        """
        unterauslastung = ue.rechnen(
            fix_stunden=20.0, geleistet=18.0, uebertrag_vormonat=5.0, formel=ue.BESTAETIGT
        )
        assert unterauslastung.verrechnet_erwartet == 20.0

        teilauslastung = ue.rechnen(
            fix_stunden=20.0, geleistet=22.0, uebertrag_vormonat=5.0, formel=ue.BESTAETIGT
        )
        assert teilauslastung.neuer_uebertrag == 3.0


# ── Aufbau des Ergebnisses ───────────────────────────────

VERTRAG_FIX = Vertrag(
    projekt="AGG Beratung", kunde="AGG", mwst_satz=8.1,
    fix_stunden=20.0, uebertragbar=True, referenz_pflicht=True,
)
MASSSTAB = Massstab(iban_erwartet="CH93 0076", uebertragsformel=Formel.KAPAZITAET)


def rechnung(**abweichung) -> Rechnung:
    grund = dict(
        nummer="RE-1001", datum=date(2026, 8, 31), mwst_satz=8.1,
        iban="CH93 0076 2011 6238 5295 7", referenz="Auftrag 4711",
        fix_stunden=20.0, zusatz_stunden=0.0, uebertrag_angabe=3.0,
        verrechnete_stunden=20.0,
    )
    return Rechnung(**{**grund, **abweichung})


def periode(**abweichung) -> Periode:
    grund = dict(
        jahr=2026, monat=8, geleistet=22.0, uebertrag_vormonat=5.0,
        dokumente_erzeugt=True, leistungsrapport_vorhanden=True,
    )
    return Periode(**{**grund, **abweichung})


class TestAufbau:
    def test_jede_regel_liefert_genau_einen_befund(self):
        """Sonst entstünde eine Spalte ohne Inhalt — wie «Verrechnet (h)» im Excel,
        die immer leer blieb, weil ``verrechnet_tatsaechlich`` nie abgelegt wurde."""
        ergebnis = pruefen(VERTRAG_FIX, rechnung(), periode(), MASSSTAB)
        assert [b.regel for b in ergebnis.befunde] == list(REGELN)

    def test_sauberer_lauf_ist_versandbereit(self):
        ergebnis = pruefen(VERTRAG_FIX, rechnung(), periode(), MASSSTAB)
        auffaellig = [b.regel for b in ergebnis.befunde if b.auffaellig]
        assert auffaellig == [], auffaellig
        assert ergebnis.gesamt is None
        assert ergebnis.versandbereit

    def test_vertragsart_kommt_aus_dem_vertrag(self):
        ergebnis = pruefen(VERTRAG_FIX, rechnung(), periode(), MASSSTAB)
        assert ergebnis.vertragsart is Vertragsart.FIX_UEBERTRAGBAR

    def test_festpreis_ohne_fixstunden(self):
        vertrag = Vertrag(
            projekt="CAS IBD", kunde="BFH", mwst_satz=0.0,
            stunden_pruefen=False, leistungsrapport_erwartet=False,
        )
        ergebnis = pruefen(
            vertrag,
            rechnung(mwst_satz=0.0, referenz=None, fix_stunden=None,
                     uebertrag_angabe=None, verrechnete_stunden=None),
            periode(geleistet=None, uebertrag_vormonat=None, leistungsrapport_vorhanden=False),
            MASSSTAB,
        )
        assert ergebnis.vertragsart is Vertragsart.FESTPREIS
        for regel in ("stunden", "uebertrag", "referenz", "leistungsrapport"):
            assert befund(ergebnis, regel).zustand is Zustand.ENTFAELLT, regel
        assert ergebnis.versandbereit


# ── Die offene Entscheidung ──────────────────────────────

class TestUnbestaetigteLesart:
    def test_stunden_und_uebertrag_bleiben_offen(self):
        """Ohne bestätigte Lesart wird nicht geraten, sondern gemeldet."""
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(), periode(),
            Massstab(iban_erwartet="CH93 0076", uebertragsformel=None),
        )
        for regel in ("stunden", "uebertrag"):
            b = befund(ergebnis, regel)
            assert b.zustand is Zustand.OFFEN, regel
            assert b.gewicht is Gewicht.FEHLER, regel
        assert not ergebnis.versandbereit

    def test_meldung_nennt_ob_der_monat_strittig_ist(self):
        """Ein Monat im Streitfenster ist dringender als einer daneben."""
        strittig = pruefen(
            VERTRAG_FIX, rechnung(), periode(geleistet=22.0),
            Massstab(uebertragsformel=None),
        )
        unstrittig = pruefen(
            VERTRAG_FIX, rechnung(), periode(geleistet=30.0),
            Massstab(uebertragsformel=None),
        )
        assert "strittigen Bereich" in befund(strittig, "uebertrag").begruendung
        assert "dasselbe Ergebnis" in befund(unstrittig, "uebertrag").begruendung

    def test_variable_abrechnung_braucht_die_lesart_nicht(self):
        vertrag = Vertrag(projekt="GSW", kunde="GSW", mwst_satz=8.1)
        ergebnis = pruefen(
            vertrag,
            rechnung(fix_stunden=None, uebertrag_angabe=None, verrechnete_stunden=14.5),
            periode(geleistet=14.5, uebertrag_vormonat=None),
            Massstab(uebertragsformel=None),
        )
        assert ergebnis.vertragsart is Vertragsart.VARIABEL
        assert befund(ergebnis, "stunden").zustand is Zustand.RICHTIG


# ── Die einzelnen Regeln ─────────────────────────────────

class TestStunden:
    def test_nicht_verrechnete_zusatzstunden_fallen_auf(self):
        """Der Fall, den die alte Prüfung strukturell nicht finden konnte.

        Läuft ``D2_updateDraftRechnungen`` nicht durch — etwa weil die
        Positionserkennung über ``text.startswith('variable zusatzstunden')``
        danebengreift —, bleibt die Zusatzposition auf null stehen. Bei 20h fix
        und 25h geleistet gingen so 5h nicht in Rechnung.

        ``admin_core`` konnte das nicht melden, weil es die Erwartung aus
        derselben Rechnung ableitete: ``verrechnet_erwartet = fix + zusatz`` und
        ``verrechnet_tatsaechlich = fix + zusatz`` sind dieselbe Zahl, die
        Differenz also immer exakt null. Hier wird die Summe der
        Stundenpositionen gelesen statt rekonstruiert, und die Lücke wird sichtbar.
        """
        vertrag = Vertrag(projekt="MBA", kunde="MBA", mwst_satz=8.1, fix_stunden=20.0)
        ergebnis = pruefen(
            vertrag,
            rechnung(zusatz_stunden=0.0, uebertrag_angabe=None, verrechnete_stunden=20.0),
            periode(geleistet=25.0, uebertrag_vormonat=None),
            MASSSTAB,
        )
        b = befund(ergebnis, "stunden")
        assert b.zustand is Zustand.FALSCH
        assert b.erwartet == "25h" and b.ist == "20h"
        assert not ergebnis.versandbereit

    def test_toleranz_deckt_rundung_ab(self):
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(verrechnete_stunden=20.08), periode(), MASSSTAB
        )
        assert befund(ergebnis, "stunden").zustand is Zustand.RICHTIG


class TestFixstunden:
    def test_abweichung_vom_vertrag_ist_ein_befund(self):
        """Bisher gewann der Positionstext stillschweigend über den Vertrag."""
        ergebnis = pruefen(VERTRAG_FIX, rechnung(fix_stunden=16.0), periode(), MASSSTAB)
        b = befund(ergebnis, "fixstunden")
        assert b.zustand is Zustand.FALSCH
        assert b.erwartet == "20h" and b.ist == "16h"

    def test_halbe_stunden_sind_darstellbar(self):
        """``(\\d+)h fix`` liess nur ganze Zahlen zu; 7.5h waren nicht abbildbar."""
        vertrag = Vertrag(projekt="T+R", kunde="T+R", mwst_satz=8.1, fix_stunden=7.5)
        ergebnis = pruefen(
            vertrag,
            rechnung(fix_stunden=7.5, uebertrag_angabe=None, verrechnete_stunden=7.5),
            periode(geleistet=7.5, uebertrag_vormonat=None),
            MASSSTAB,
        )
        assert befund(ergebnis, "fixstunden").zustand is Zustand.RICHTIG


class TestUebertrag:
    def test_andere_lesart_anderes_urteil(self):
        """Dieselbe Rechnung, zwei Lesarten, zwei Ergebnisse — der Kern der Frage."""
        mit_kapazitaet = pruefen(
            VERTRAG_FIX, rechnung(), periode(),
            Massstab(uebertragsformel=Formel.KAPAZITAET),
        )
        mit_verfall = pruefen(
            VERTRAG_FIX, rechnung(), periode(),
            Massstab(uebertragsformel=Formel.VERFALL),
        )
        assert befund(mit_kapazitaet, "uebertrag").zustand is Zustand.RICHTIG
        assert befund(mit_verfall, "uebertrag").zustand is Zustand.FALSCH

    def test_ohne_uebertragbarkeit_entfaellt_die_regel(self):
        vertrag = Vertrag(projekt="MBA", kunde="MBA", mwst_satz=8.1, fix_stunden=20.0)
        ergebnis = pruefen(
            vertrag, rechnung(uebertrag_angabe=None), periode(), MASSSTAB
        )
        assert befund(ergebnis, "uebertrag").zustand is Zustand.ENTFAELLT

    def test_nichts_zu_uebertragen_braucht_keinen_satz(self):
        """Wer «0h werden angerechnet» auf eine Rechnung schreibt, macht sie
        schlechter. Im Rücklauf gegen den August traf die frühere Warnung zwei
        von sechs Pauschalrechnungen — beide korrekt."""
        ergebnis = pruefen(
            VERTRAG_FIX,
            rechnung(uebertrag_angabe=None, verrechnete_stunden=25.0),
            periode(geleistet=25.0),  # 20h fix + 5h Übertrag genau ausgeschöpft
            MASSSTAB,
        )
        b = befund(ergebnis, "uebertrag")
        assert b.zustand is Zustand.RICHTIG and b.gewicht is Gewicht.HINWEIS

    def test_fehlender_satz_bei_offenem_rest_bleibt_eine_warnung(self):
        ergebnis = pruefen(
            VERTRAG_FIX,
            rechnung(uebertrag_angabe=None),
            periode(geleistet=22.0),  # 3h bleiben stehen
            MASSSTAB,
        )
        b = befund(ergebnis, "uebertrag")
        assert b.zustand is Zustand.OFFEN and b.gewicht is Gewicht.WARNUNG
        assert "3h" in b.begruendung


class TestMehrwertsteuer:
    def test_falscher_satz(self):
        ergebnis = pruefen(VERTRAG_FIX, rechnung(mwst_satz=7.7), periode(), MASSSTAB)
        b = befund(ergebnis, "mwst")
        assert b.zustand is Zustand.FALSCH
        assert b.erwartet == "8.1%" and b.ist == "7.7%"

    def test_null_prozent_ist_ein_gueltiger_satz(self):
        """Bei der BFH ist 0 % vereinbart — das darf nicht wie «fehlt» aussehen."""
        vertrag = Vertrag(
            projekt="CAS IBD", kunde="BFH", mwst_satz=0.0,
            fix_stunden=20.0, uebertragbar=True,
        )
        ergebnis = pruefen(vertrag, rechnung(mwst_satz=0.0), periode(), MASSSTAB)
        assert befund(ergebnis, "mwst").zustand is Zustand.RICHTIG


class TestBankverbindung:
    def test_falsche_bank(self):
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(iban="CH56 0483 5012 3456 7800 9"), periode(), MASSSTAB
        )
        assert befund(ergebnis, "iban").zustand is Zustand.FALSCH

    def test_unlesbare_iban_ist_offen_und_nicht_gruen(self):
        """Der Kern des Umbaus: fehlender Fehler war bisher gleichbedeutend mit grün.

        Im Excel erzeugte eine nicht lesbare IBAN keinen Eintrag in der
        Fehlerliste, und die Einfärbung fragte nur die Fehlerliste ab. Die Zelle
        war grün, obwohl nie etwas geprüft worden war.
        """
        ergebnis = pruefen(VERTRAG_FIX, rechnung(iban=None), periode(), MASSSTAB)
        b = befund(ergebnis, "iban")
        assert b.zustand is Zustand.OFFEN
        assert b.zustand is not Zustand.RICHTIG

    def test_leerzeichen_stoeren_nicht(self):
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(iban="CH930076201162385295 7"), periode(), MASSSTAB
        )
        assert befund(ergebnis, "iban").zustand is Zustand.RICHTIG


class TestReferenz:
    def test_pflicht_ohne_referenz(self):
        ergebnis = pruefen(VERTRAG_FIX, rechnung(referenz="   "), periode(), MASSSTAB)
        assert befund(ergebnis, "referenz").zustand is Zustand.FALSCH

    def test_ohne_pflicht_entfaellt(self):
        vertrag = Vertrag(
            projekt="GSW", kunde="GSW", mwst_satz=8.1,
            fix_stunden=20.0, uebertragbar=True, referenz_pflicht=False,
        )
        ergebnis = pruefen(vertrag, rechnung(referenz=None), periode(), MASSSTAB)
        assert befund(ergebnis, "referenz").zustand is Zustand.ENTFAELLT


class TestRechnungsdatum:
    def test_letzter_tag_des_leistungsmonats(self):
        ergebnis = pruefen(VERTRAG_FIX, rechnung(datum=date(2026, 8, 31)), periode(), MASSSTAB)
        assert befund(ergebnis, "rechnungsdatum").zustand is Zustand.RICHTIG

    def test_erster_des_folgemonats_ist_falsch(self):
        """Der Lauf findet im Folgemonat statt; das Datum darf nicht mitwandern,
        sonst rutscht der Umsatz in die falsche Periode."""
        ergebnis = pruefen(VERTRAG_FIX, rechnung(datum=date(2026, 9, 1)), periode(), MASSSTAB)
        b = befund(ergebnis, "rechnungsdatum")
        assert b.zustand is Zustand.FALSCH
        assert b.erwartet == "31.08.2026"

    def test_schaltjahr(self):
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(datum=date(2028, 2, 29)),
            periode(jahr=2028, monat=2), MASSSTAB,
        )
        assert befund(ergebnis, "rechnungsdatum").zustand is Zustand.RICHTIG


class TestLeistungsrapport:
    def test_zusatzstunden_ohne_rapport_blockieren(self):
        ergebnis = pruefen(
            VERTRAG_FIX,
            rechnung(zusatz_stunden=4.0, verrechnete_stunden=24.0, uebertrag_angabe=0.0),
            # 29h geleistet, 25h gedeckt (20h fix + 5h Uebertrag) -- erst darueber
            # entstehen Zusatzstunden. Bei 25h deckte der Uebertrag sie ab.
            periode(geleistet=29.0, leistungsrapport_vorhanden=False),
            MASSSTAB,
        )
        b = befund(ergebnis, "leistungsrapport")
        assert b.zustand is Zustand.FALSCH and b.gewicht is Gewicht.FEHLER
        assert not ergebnis.versandbereit

    def test_fehlender_rapport_ohne_zusatzstunden_ist_nur_eine_warnung(self):
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(), periode(leistungsrapport_vorhanden=False), MASSSTAB
        )
        b = befund(ergebnis, "leistungsrapport")
        assert b.zustand is Zustand.OFFEN and b.gewicht is Gewicht.WARNUNG
        assert ergebnis.gesamt is Gewicht.WARNUNG
        assert ergebnis.versandbereit

    def test_vor_dem_erzeugen_ist_der_rapport_nicht_beurteilbar(self):
        """«Noch nicht da» ist nicht «fehlt».

        Die Prüfung läuft vor dem Erzeugen der Dokumente und danach. Ohne diese
        Unterscheidung meldete der erste Durchgang jede Rechnung mit
        Zusatzstunden als fehlerhaft -- und wer lernt, über rote Zeilen
        hinwegzulesen, liest auch über die echten hinweg.
        """
        ergebnis = pruefen(
            VERTRAG_FIX,
            rechnung(zusatz_stunden=4.0, verrechnete_stunden=24.0, uebertrag_angabe=0.0),
            periode(geleistet=29.0, dokumente_erzeugt=False, leistungsrapport_vorhanden=False),
            MASSSTAB,
        )
        b = befund(ergebnis, "leistungsrapport")
        assert b.zustand is Zustand.OFFEN and b.gewicht is Gewicht.HINWEIS
        assert ergebnis.versandbereit

    def test_vor_dem_erzeugen_ist_auch_die_iban_nicht_beurteilbar(self):
        """Dieselbe Tatsache, dieselbe Folge: beide Regeln lesen aus der PDF.

        Im Rücklauf gegen den August trug jede der acht Rechnungen eine
        IBAN-Warnung, weil die PDF zu diesem Zeitpunkt nicht existiert. Acht von
        acht heisst: die Meldung unterscheidet nichts mehr.
        """
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(iban=None), periode(dokumente_erzeugt=False), MASSSTAB
        )
        b = befund(ergebnis, "iban")
        assert b.zustand is Zustand.OFFEN and b.gewicht is Gewicht.HINWEIS
        assert ergebnis.versandbereit

    def test_nach_dem_erzeugen_ist_eine_fehlende_iban_eine_warnung(self):
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(iban=None), periode(dokumente_erzeugt=True), MASSSTAB
        )
        b = befund(ergebnis, "iban")
        assert b.zustand is Zustand.OFFEN and b.gewicht is Gewicht.WARNUNG

    def test_ohne_erwarteten_rapport_entfaellt_die_regel_auch_vorher(self):
        vertrag = Vertrag(
            projekt="CAS IBD", kunde="BFH", mwst_satz=0.0,
            stunden_pruefen=False, leistungsrapport_erwartet=False,
        )
        ergebnis = pruefen(
            vertrag,
            rechnung(mwst_satz=0.0, referenz=None, fix_stunden=None,
                     uebertrag_angabe=None, verrechnete_stunden=None),
            periode(geleistet=None, uebertrag_vormonat=None, dokumente_erzeugt=False,
                    leistungsrapport_vorhanden=False),
            MASSSTAB,
        )
        assert befund(ergebnis, "leistungsrapport").zustand is Zustand.ENTFAELLT


class TestGesamturteil:
    def test_warnung_ist_sichtbar_und_nicht_gruen(self):
        """Im Excel färbte eine Warnung nichts ein — die Zeile blieb durchgehend grün."""
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(), periode(leistungsrapport_vorhanden=False), MASSSTAB
        )
        assert ergebnis.gesamt is Gewicht.WARNUNG

    def test_ein_fehler_faerbt_nur_sein_eigenes_feld(self):
        """Ein Übertragsfehler färbte im Excel vier fremde Spalten rot, weil die
        Einfärbung die Fehlerliste nach Stichworten durchsuchte und das Ergebnis
        auf die Spalten 4 bis 8 anwendete."""
        ergebnis = pruefen(
            VERTRAG_FIX, rechnung(uebertrag_angabe=99.0), periode(), MASSSTAB
        )
        falsch = [b.regel for b in ergebnis.befunde if b.zustand is Zustand.FALSCH]
        assert falsch == ["uebertrag"]

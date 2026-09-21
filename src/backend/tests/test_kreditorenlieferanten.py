"""Prueft die Deklaration der Kreditorenlieferanten -- und die Waechter darum.

Die Tests gegen die **echte** Datei stehen bewusst neben denen mit Kunstdaten:
eine Deklaration, die nur in Beispielen stimmt, traegt nichts. Sie sind
absichtlich unempfindlich gegen den Bestand (keine festen Zahlen), damit ein
neuer Lieferant sie nicht rot macht.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from app.services import kreditorenlieferanten as kl


def _schreiben(tmp_path: Path, inhalt: str) -> Path:
    ziel = tmp_path / "kreditorenlieferanten.yaml"
    ziel.write_text(inhalt, encoding="utf-8")
    return ziel


class TestLaden:
    def test_ein_vollstaendiger_eintrag_wird_gelesen(self, tmp_path):
        pfad = _schreiben(tmp_path, """
version: 1
stand: 2026-09-21
lieferanten:
  - schluessel: cursor
    ordner: Cursor
    sollkonto: "6570"
    zahlweg: karte
    steuer:
      - ab: 2024-10-01
        behandlung: bezugssteuer
    rhythmus: monatlich
    aktiv: true
    bestaetigt: true
""")
        bestand = kl.laden(pfad)
        cursor = bestand.lieferanten["cursor"]

        assert cursor.ordner == "Cursor"
        assert cursor.sollkonto == "6570"
        assert cursor.zahlweg == ("karte",)
        assert cursor.rhythmus == "monatlich"
        assert cursor.bestaetigt is True
        assert not cursor.entscheid_je_rechnung
        assert bestand.maengel == ()

    def test_eine_fehlende_datei_ist_kein_absturz(self, tmp_path):
        bestand = kl.laden(tmp_path / "gibtsnicht.yaml")
        assert bestand.lieferanten == {}

    def test_kaputtes_yaml_ist_kein_absturz(self, tmp_path):
        bestand = kl.laden(_schreiben(tmp_path, "lieferanten: [ das: ist: kaputt"))
        assert bestand.lieferanten == {}


class TestVerworfeneWerte:
    """Ein unbekannter Wert wird verworfen und gemeldet, nie still uebernommen.

    Sonst entstuende eine Erwartung, gegen die spaeter nie etwas passt -- und
    der Abgleich meldete einen Befund, dessen Ursache in der Datei liegt.
    """

    def test_unbekannte_steuerbehandlung_wird_verworfen(self, tmp_path):
        pfad = _schreiben(tmp_path, """
lieferanten:
  - schluessel: x
    ordner: X
    steuer:
      - ab: null
        behandlung: irgendwas
""")
        bestand = kl.laden(pfad)
        assert bestand.lieferanten["x"].steuer == ()
        assert any("irgendwas" in m for m in bestand.maengel)

    def test_unbekannter_zahlweg_wird_verworfen(self, tmp_path):
        pfad = _schreiben(tmp_path, """
lieferanten:
  - schluessel: x
    ordner: X
    zahlweg: [karte, bargeld]
""")
        bestand = kl.laden(pfad)
        assert bestand.lieferanten["x"].zahlweg == ("karte",)
        assert any("bargeld" in m for m in bestand.maengel)

    def test_konto_und_kandidaten_zugleich_ist_ein_widerspruch(self, tmp_path):
        pfad = _schreiben(tmp_path, """
lieferanten:
  - schluessel: x
    ordner: X
    sollkonto: "6570"
    sollkonto_kandidaten: ["4200", "6512"]
""")
        bestand = kl.laden(pfad)
        assert any("zugleich" in m for m in bestand.maengel)

    def test_zwei_lieferanten_auf_denselben_ordner_werden_gemeldet(self, tmp_path):
        """Sonst wuesste die Ablage nicht, welcher Lieferant gemeint ist."""
        pfad = _schreiben(tmp_path, """
lieferanten:
  - schluessel: a
    ordner: Google
  - schluessel: b
    ordner: Google
""")
        bestand = kl.laden(pfad)
        assert any("Google" in m and "mehreren" in m for m in bestand.maengel)


class TestSteuerAmTag:
    """Die Behandlung gilt ab einem Datum, weil Lieferanten ihr Verhalten aendern."""

    def test_die_juengste_gueltige_zeile_gewinnt(self, tmp_path):
        pfad = _schreiben(tmp_path, """
lieferanten:
  - schluessel: x
    ordner: X
    steuer:
      - ab: null
        behandlung: bezugssteuer
      - ab: 2026-07-01
        behandlung: inland_mwst
""")
        x = kl.laden(pfad).lieferanten["x"]

        assert x.steuer_am(date(2026, 6, 30)) == "bezugssteuer"
        assert x.steuer_am(date(2026, 7, 1)) == "inland_mwst"
        assert x.steuer_am(date(2027, 1, 1)) == "inland_mwst"

    def test_ohne_zeile_bleibt_es_unbekannt(self, tmp_path):
        pfad = _schreiben(tmp_path, "lieferanten:\n  - schluessel: x\n    ordner: X\n")
        assert kl.laden(pfad).lieferanten["x"].steuer_am(date.today()) == "unbekannt"

    def test_inlaendisch_befreit_ist_weder_bezugssteuer_noch_vorsteuer(self, tmp_path):
        """Der vierte Wert, ohne den vier richtige Faelle als Frage landeten.

        Die Ausgleichskasse weist auf 77 von 77 Rechnungen keine MWST aus, VZ
        auf 17 von 17. Mit nur drei Werten war das entweder «bezugssteuer»
        (falsch, beide sind inlaendisch) oder eine offene Frage (falsch, es
        ist Art. 21 MWSTG und voellig eindeutig).
        """
        pfad = _schreiben(tmp_path, """
lieferanten:
  - schluessel: ausgleichskasse
    ordner: Ausgleichskasse
    steuer:
      - ab: 2019-05-09
        behandlung: ohne_mwst
""")
        lf = kl.laden(pfad).lieferanten["ausgleichskasse"]
        assert lf.steuer_am(date(2026, 1, 1)) == "ohne_mwst"
        assert lf.steuer_am(date(2019, 1, 1)) == "unbekannt"


class TestEchteDatei:
    """Gegen den gepflegten Bestand, ohne feste Zahlen."""

    @pytest.fixture(scope="class")
    def bestand(self):
        return kl.laden()

    def test_die_datei_laedt_ohne_maengel(self, bestand):
        assert bestand.maengel == (), f"Mängel in der Deklaration: {bestand.maengel}"
        assert bestand.lieferanten, "kein einziger Lieferant gelesen"

    def test_jeder_ordner_gehoert_genau_einem_lieferanten(self, bestand):
        ordner = [l.ordner for l in bestand.lieferanten.values()]
        assert len(ordner) == len(set(ordner))

    def test_kein_bezeichner_traegt_umlaute(self, bestand):
        """Schluessel sind ASCII -- sie loest eine Maschine auf, kein Mensch."""
        krumm = [s for s in bestand.lieferanten if not s.isascii()]
        assert krumm == [], f"Schlüssel mit Umlauten: {krumm}"

    def test_ein_mehrdeutiges_konto_verlangt_eine_entscheidung(self, bestand):
        """Wo die Historie mehrere Konten zeigt, wird nicht geraten.

        Hosttech und Metanet fuehren dieselbe Domain einmal als eigenen
        Aufwand (6512) und einmal als weiterverrechnete Leistung (4200).
        """
        mehrdeutig = [
            l for l in bestand.lieferanten.values() if len(l.sollkonto_kandidaten) > 1
        ]
        assert mehrdeutig, "keine mehrdeutigen Lieferanten — Vorschlag prüfen"
        for l in mehrdeutig:
            assert l.sollkonto is None
            assert l.entscheid_je_rechnung

    def test_keine_erwartung_behauptet_inlandsteuer_aus_fehlender_buchung(self, bestand):
        """Der Fehler, der den Kontrollprozess entwerten wuerde.

        Aus «keine Zeile auf 2203» folgt nicht «es faellt keine Bezugssteuer
        an». Wer das als Erwartung hinschreibt, laesst den Abgleich genau den
        Fehler absegnen, den er finden soll. Die Gegenprobe: `inland_mwst`
        darf nur mit einem `ab`-Datum stehen, also nur als bewusste Aussage
        ueber einen Zeitpunkt -- nie als Rueckfall ohne Datum.
        """
        blind = [
            l.schluessel
            for l in bestand.lieferanten.values()
            for z in l.steuer
            if z.behandlung == "inland_mwst" and z.ab is None
        ]
        assert blind == [], f"undatierte Inlandsteuer-Erwartung bei: {blind}"

    def test_eine_deklaration_ohne_datum_ist_keine(self, bestand):
        """Die allgemeine Fassung der Regel oben.

        Sie galt zuerst nur fuer `inland_mwst`, weil dort der Schaden am
        groessten war. Seit die Steuerbehandlung aus den Rechnungen kommt,
        gilt sie fuer alle drei Aussagen: eine Behandlung ohne Geltungsbeginn
        behauptet «immer schon und fuer immer», und das weiss niemand. Ein
        Lieferant kann sich registrieren oder abmelden -- genau deshalb ist
        `steuer` eine Liste und kein Feld.

        `unbekannt` darf undatiert stehen: es ist keine Aussage, sondern das
        Eingestaendnis, keine zu haben.
        """
        undatiert = [
            f"{l.schluessel}: {z.behandlung}"
            for l in bestand.lieferanten.values()
            for z in l.steuer
            if z.ab is None and z.behandlung != "unbekannt"
        ]
        assert undatiert == [], f"Behandlung ohne Geltungsbeginn: {undatiert}"

    def test_offene_fragen_stehen_im_wortlaut(self, bestand):
        """Ein Etikett wie «unklar» erreicht niemanden."""
        assert bestand.offen, "keine offenen Fragen — wurde das Vorschlagen ausgeführt?"
        for frage in bestand.offen:
            assert len(frage) > 60, f"zu knapp, um eine Frage zu sein: {frage!r}"

    def test_aktive_lieferanten_sind_ueberwiegend_deklariert(self, bestand):
        """Ein aktiver Lieferant ohne Kontoerwartung faellt still durch."""
        aktive = [l for l in bestand.lieferanten.values() if l.aktiv]
        assert aktive, "kein aktiver Lieferant"
        luecken = bestand.unvollstaendig()
        assert len(luecken) / len(aktive) < 0.2, (
            f"{len(luecken)} von {len(aktive)} aktiven Lieferanten ohne "
            f"Kontoerwartung: {[l.schluessel for l in luecken]}"
        )

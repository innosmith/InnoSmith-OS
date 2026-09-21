"""Tests für den Leistungsrapport.

Zwei Ebenen, und die Trennung ist Absicht: die **Zahlen** werden vollständig
geprüft (``aufbereiten`` ist eine reine Funktion), das **PDF** nur darauf, dass
es eines ist und die Zahlen enthält. Ob es gut aussieht, entscheidet nicht der
Test, sondern der Mensch, der es der Kundschaft schickt.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.services import leistungsrapport as lr


def eintrag(tag: int, beschreibung: str, stunden: float, **rest) -> dict:
    return {
        "datum": date(2026, 8, tag),
        "beschreibung": beschreibung,
        "dauer_stunden": stunden,
        **rest,
    }


# ── Die Zahlen ──────────────────────────────────────────────────────────────


def test_summe_und_reihenfolge():
    """Die Zeilen bleiben, wie sie kommen — die Reihenfolge ist die des Rapports."""
    r = lr.aufbereiten(
        [eintrag(3, "Workshop", 2.5), eintrag(1, "Konzept", 4.0)],
        jahr=2026, monat=8,
    )

    assert r.zeitraum == "August 2026"
    assert [z.taetigkeit for z in r.zeilen] == ["Workshop", "Konzept"]
    assert [z.datum for z in r.zeilen] == ["03.08.2026", "01.08.2026"]
    assert r.summe == 6.5


def test_bereich_und_beschreibung_werden_verbunden():
    r = lr.aufbereiten(
        [eintrag(1, "Interviews geführt", 3.0, task_id=7)],
        jahr=2026, monat=8, bereiche={7: "Analyse"},
    )

    assert r.zeilen[0].taetigkeit == "Analyse: Interviews geführt"


def test_unbekannter_bereich_wird_nicht_geraten():
    """Ein erratener Name sähe aus wie eine Zuordnung und wäre keine."""
    r = lr.aufbereiten(
        [eintrag(1, "Arbeit", 1.0, task_id=999)],
        jahr=2026, monat=8, bereiche={7: "Analyse"},
    )

    assert r.zeilen[0].taetigkeit == "Arbeit"
    assert r.nach_bereich == []   # nur (Allgemein) — keine Aufschlüsselung


def test_allgemein_steht_am_ende_der_aufschluesselung():
    """Es ist kein Bereich, sondern dessen Abwesenheit."""
    r = lr.aufbereiten(
        [
            eintrag(1, "ohne Bereich", 9.0),
            eintrag(2, "mit Bereich", 2.0, task_id=7),
        ],
        jahr=2026, monat=8, bereiche={7: "Analyse"},
    )

    # Trotz 9 gegen 2 Stunden steht (Allgemein) zuletzt.
    assert r.nach_bereich == [("Analyse", 2.0), ("(Allgemein)", 9.0)]


def test_eine_person_steht_im_kopf_und_nicht_an_den_zeilen():
    r = lr.aufbereiten(
        [eintrag(1, "Konzept", 4.0, user_id=1), eintrag(2, "Bau", 2.0, user_id=1)],
        jahr=2026, monat=8, personen={1: "Anthony Smith"},
    )

    assert r.mitarbeitende == "Anthony Smith"
    assert r.zeilen[0].taetigkeit == "Konzept"   # kein Kürzel
    assert r.nach_person == []                   # keine Aufschlüsselung


def test_mehrere_personen_stehen_an_den_zeilen_und_nicht_im_kopf():
    """Ein Kopf mit einem Namen behauptete sonst eine Zuordnung, die die
    Zeilen widerlegen."""
    r = lr.aufbereiten(
        [
            eintrag(1, "Konzept", 4.0, user_id=1),
            eintrag(2, "Bau", 6.0, user_id=2),
        ],
        jahr=2026, monat=8,
        personen={1: "Anthony Smith", 2: "Hans-Peter Müller"},
    )

    assert r.mitarbeitende is None
    assert r.zeilen[0].taetigkeit == "Konzept (AS)"
    assert r.zeilen[1].taetigkeit == "Bau (HPM)"
    assert r.nach_person == [("Hans-Peter Müller (HPM)", 6.0), ("Anthony Smith (AS)", 4.0)]


def test_unbekannte_person_wird_benannt_statt_verschwiegen():
    r = lr.aufbereiten(
        [eintrag(1, "A", 1.0, user_id=1), eintrag(2, "B", 2.0, user_id=99)],
        jahr=2026, monat=8, personen={1: "Anthony Smith"},
    )

    assert ("(Unbekannt)", 2.0) in r.nach_person


def test_leerer_eintrag_bekommt_einen_platzhalter():
    """Eine leere Zeile im Rapport sähe aus wie ein Darstellungsfehler."""
    r = lr.aufbereiten([eintrag(1, "", 1.0)], jahr=2026, monat=8)

    assert r.zeilen[0].taetigkeit == "(Keine Angabe)"


def test_kuerzel():
    assert lr.kuerzel("Anthony Smith") == "AS"
    assert lr.kuerzel("Hans-Peter Müller") == "HPM"
    assert lr.kuerzel("") == "??"


def test_ohne_eintraege_bleibt_die_summe_null():
    r = lr.aufbereiten([], jahr=2026, monat=8)

    assert r.summe == 0.0
    assert r.zeilen == []
    assert r.mitarbeitende is None


# ── Das PDF ─────────────────────────────────────────────────────────────────


def test_pdf_traegt_die_signatur_und_die_zahlen():
    """Geprüft wird, dass es ein PDF ist und die Beträge darin stehen.

    Nicht geprüft wird, ob es gut aussieht — das entscheidet der Mensch, der es
    verschickt.
    """
    from pypdf import PdfReader
    from io import BytesIO

    r = lr.aufbereiten(
        [
            eintrag(1, "Konzeptarbeit", 4.25, task_id=7),
            eintrag(15, "Workshop mit dem Team", 3.5, task_id=8),
        ],
        jahr=2026, monat=8, bereiche={7: "Analyse", 8: "Durchführung"},
    )
    roh = lr.erzeugen(r)

    assert roh.startswith(b"%PDF")

    text = PdfReader(BytesIO(roh)).pages[0].extract_text()
    assert "Leistungsrapport August 2026" in text
    assert "Konzeptarbeit" in text
    assert "7.75h" in text          # die Summe
    assert "Analyse" in text        # die Aufschlüsselung


def test_umlaute_ueberstehen_den_satz():
    """Die Vorlage wich bei fehlender Schrift auf Helvetica aus — dort sind
    Umlaute in Latin-1 und brechen bei «€» oder «–» weg."""
    from pypdf import PdfReader
    from io import BytesIO

    r = lr.aufbereiten(
        [eintrag(1, "Prüfung der Grössenordnung – Begleitung", 1.0)],
        jahr=2026, monat=8,
    )
    text = PdfReader(BytesIO(lr.erzeugen(r))).pages[0].extract_text()

    assert "Prüfung der Grössenordnung" in text


def test_fehlende_schrift_bricht_ab(monkeypatch, tmp_path):
    """Ein Rapport, der *fast* wie die bisherigen aussieht, ist die
    schlechteste Variante: er fällt niemandem auf und stimmt nicht."""
    monkeypatch.setattr(lr, "SCHRIFT_NORMAL", tmp_path / "gibt-es-nicht.ttf")

    with pytest.raises(FileNotFoundError, match="Schrift"):
        lr.erzeugen(lr.aufbereiten([eintrag(1, "A", 1.0)], jahr=2026, monat=8))


def test_langer_text_bricht_um_und_die_stunden_bleiben_oben():
    """Nach einem ``multi_cell`` steht der Zeiger weiter unten; ohne die
    gemerkte Marke landete die Stundenzahl auf Höhe der letzten Textzeile."""
    from pypdf import PdfReader
    from io import BytesIO

    lang = "Sehr ausführliche Beschreibung einer Tätigkeit, " * 6
    r = lr.aufbereiten([eintrag(1, lang, 2.0)], jahr=2026, monat=8)
    text = PdfReader(BytesIO(lr.erzeugen(r))).pages[0].extract_text()

    assert "2.00" in text
    assert "2.00h" in text   # Summe


def test_viele_zeilen_ergeben_mehrere_seiten():
    from pypdf import PdfReader
    from io import BytesIO

    r = lr.aufbereiten(
        [eintrag(1, f"Eintrag {i}", 0.5) for i in range(90)],
        jahr=2026, monat=8,
    )
    seiten = PdfReader(BytesIO(lr.erzeugen(r))).pages

    assert len(seiten) > 1
    assert "Seite 2 von" in seiten[1].extract_text()

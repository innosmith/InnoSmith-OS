"""Die Norm für Buchungstext und Dateiname -- mit dem Beispiel vom 25.09.2026."""

from __future__ import annotations

from datetime import date

import pytest
from app.services import kreditorennorm as norm

RECHNUNG = date(2026, 9, 22)


class TestBuchungstext:
    def test_das_vereinbarte_beispiel(self):
        assert norm.buchungstext("RapidAPI", "Monatsabo", RECHNUNG) == "RapidAPI, Monatsabo 2026.09"

    def test_die_periode_ist_der_monat_des_rechnungsdatums(self):
        assert norm.buchungstext("Toggl", "Track Jahresabo", date(2026, 1, 3)).endswith("2026.01")

    def test_der_rand_aus_dem_journal_entsteht_nicht(self):
        """Das Journal führt « Rapid API Monatsabo » mit Leerzeichen am Rand."""
        assert norm.buchungstext(" RapidAPI ", " Monatsabo ", RECHNUNG) == "RapidAPI, Monatsabo 2026.09"


class TestDateiname:
    def test_das_vereinbarte_beispiel(self):
        assert (
            norm.dateiname("RapidAPI", "Monatsabo", RECHNUNG, "karte")
            == "RapidAPI Monatsabo 22.09.2026 KK.pdf"
        )

    def test_kk_nur_bei_karte(self):
        assert norm.dateiname("Mobiliar", "KTG", RECHNUNG, "rechnung") == "Mobiliar KTG 22.09.2026.pdf"
        assert norm.dateiname("Mobiliar", "KTG", RECHNUNG, None) == "Mobiliar KTG 22.09.2026.pdf"

    def test_der_ordnername_behaelt_seine_leerzeichen(self):
        assert norm.dateiname("Rara Theme", "Jahresabo", RECHNUNG, "karte").startswith("Rara Theme ")

    def test_ein_schraegstrich_legte_einen_unterordner_an(self):
        with pytest.raises(norm.NormVerletzt, match="/"):
            norm.dateiname("Hosttech", "Domain a/b", RECHNUNG, "karte")


class TestLeistung:
    @pytest.mark.parametrize(
        ("roh", "erwartet"),
        [
            ("Monats-Abo", "Monatsabo"),
            ("Webhosting Jahres-Abo", "Webhosting Jahresabo"),
            ("  Claude  Pro   Monatsabo ", "Claude Pro Monatsabo"),
            ("", None),
            (None, None),
        ],
    )
    def test_normieren(self, roh, erwartet):
        assert norm.leistung_normieren(roh) == erwartet

    def test_ohne_leistung_keine_norm(self):
        """Entschieden am 25.09.2026: immer mit Leistung."""
        with pytest.raises(norm.NormVerletzt, match="keine Leistung"):
            norm.buchungstext("Hosttech", None, RECHNUNG)
        with pytest.raises(norm.NormVerletzt, match="keine Leistung"):
            norm.dateiname("Hosttech", "  ", RECHNUNG, "karte")

    def test_ohne_name_keine_norm(self):
        with pytest.raises(norm.NormVerletzt, match="Lieferantenname"):
            norm.buchungstext("", "Monatsabo", RECHNUNG)

"""Die Suchanfrage muss im Verlauf stehen, nicht nur im Suchverlauf.

Bis hierher zeigte der Chat waehrend eines Laufs nur den Werkzeugnamen an --
«web_search». Die Anfrage selbst landete zwar in ``web_searches`` und war damit
hinterher pruefbar, aber im Moment des Geschehens stand dort nichts, woran sich
haette erkennen lassen, was das Haus verlaesst.

Der Unterschied faellt genau dann auf, wenn er zaehlt: Ein Sprachmodell nimmt
Namen aus dem Zusammenhang von sich aus in eine Suchanfrage auf, auch wenn
niemand darum gebeten hat. Gemessen im GSW-Cockpit: Gefragt war nach dem
Mehrwertsteuersatz, gesucht wurde nach dem Treuhandbuero des Mandanten. Wer das
erst im Nachhinein im Suchverlauf sieht, sieht es zu spaet.
"""

from __future__ import annotations

import json

from app.routers.chat import _suchanfrage


class TestSuchanfrageLesen:
    """Hermes reicht die Argumente in zwei Formen durch. Beide kommen vor."""

    def test_woerterbuch(self):
        assert _suchanfrage({"query": "Mehrwertsteuersatz Schweiz 2026"}) == (
            "Mehrwertsteuersatz Schweiz 2026"
        )

    def test_json_zeichenkette(self):
        roh = json.dumps({"query": "Revision OR Abschluss", "limit": 5})
        assert _suchanfrage(roh) == "Revision OR Abschluss"

    def test_umlaute_bleiben_erhalten(self):
        """Sonst stuende «Grundstuckgewinnsteuer» im Verlauf."""
        assert _suchanfrage({"query": "Grundstückgewinnsteuer Solothurn"}) == (
            "Grundstückgewinnsteuer Solothurn"
        )


class TestNieDurchfallen:
    """Die Anzeige darf den Agenten unter keinen Umstaenden anhalten.

    Ein Fehler beim Lesen der Argumente ist ein Schoenheitsfehler; ein Absturz
    mitten im Lauf kostet die ganze Antwort. Darum faengt der Helfer alles ab
    und liefert im Zweifel nichts.
    """

    def test_kaputtes_json(self):
        assert _suchanfrage("{nicht wirklich json") == ""

    def test_fehlende_anfrage(self):
        assert _suchanfrage({"limit": 5}) == ""

    def test_nichts(self):
        assert _suchanfrage(None) == ""

    def test_leere_anfrage_bleibt_leer(self):
        assert _suchanfrage({"query": ""}) == ""

    def test_unerwarteter_typ(self):
        assert _suchanfrage(42) == ""

    def test_zeichenkette_ohne_klammer(self):
        """Eine blosse Zeichenkette ist kein JSON und wird nicht geraten."""
        assert _suchanfrage("Mehrwertsteuer") == ""

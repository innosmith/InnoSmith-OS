"""Was der Ablauf eines Agentenlaufs festhalten muss.

Der Anlass ist eine Fehlersuche, die ins Leere lief: Am 02.09.2026 meldete der
Agent eine Kundenrangliste aus Pipedrive, deren Gesamtsumme stimmte und deren
Aufschluesselung nicht. Welche Abfrage die Zahlen erzeugt hatte, liess sich nicht
mehr feststellen -- 140 Denkschritte hatten den gemeinsamen Deckel von 200
Ereignissen gefuellt, bevor der erste Werkzeugaufruf kam, und der ``execute_code``
lag dahinter.

Geprueft wird deshalb die Behauptung: **Ein Werkzeugaufruf geht nie verloren,
egal wie viel gedacht wurde.** Denkschritte sind billig und duerfen abgeschnitten
werden; Werkzeuge tragen die Beweislast.
"""

from app.routers.chat import Ablaufspeicher, denkmodus_hinweis, zeitlimit_grund


def _denken(n: int) -> list[dict]:
    return [{"type": "thinking", "text": f"Schritt {i}"} for i in range(n)]


class TestGetrennteBudgets:
    def test_viel_denken_verdraengt_kein_werkzeug(self):
        speicher = Ablaufspeicher(max_denken=5, max_werkzeug=10)
        for ereignis in _denken(500):
            speicher.anhaengen(ereignis)
        speicher.anhaengen({"type": "tool_start", "name": "mcp_sandbox_execute_code"})
        speicher.anhaengen({"type": "tool_complete", "name": "mcp_sandbox_execute_code"})

        werkzeuge = [e for e in speicher.ereignisse if e["type"] != "thinking"]
        assert len(werkzeuge) == 2, "Der entscheidende Aufruf darf nicht verdrängt werden"
        assert len([e for e in speicher.ereignisse if e["type"] == "thinking"]) == 5

    def test_denken_wird_gedeckelt(self):
        speicher = Ablaufspeicher(max_denken=3, max_werkzeug=10)
        for ereignis in _denken(50):
            speicher.anhaengen(ereignis)
        assert len(speicher.ereignisse) == 3

    def test_werkzeuge_werden_ebenfalls_gedeckelt(self):
        """Auch das Werkzeugbudget ist endlich -- eine Schleife darf die Zeile nicht sprengen."""
        speicher = Ablaufspeicher(max_denken=3, max_werkzeug=4)
        for i in range(20):
            speicher.anhaengen({"type": "tool_start", "name": f"werkzeug_{i}"})
        assert len(speicher.ereignisse) == 4

    def test_reihenfolge_bleibt_erhalten(self):
        speicher = Ablaufspeicher(max_denken=2, max_werkzeug=2)
        speicher.anhaengen({"type": "thinking", "text": "erst"})
        speicher.anhaengen({"type": "tool_start", "name": "a"})
        speicher.anhaengen({"type": "thinking", "text": "dann"})
        speicher.anhaengen({"type": "tool_complete", "name": "a"})
        assert [e.get("name") or e.get("text") for e in speicher.ereignisse] == [
            "erst", "a", "dann", "a",
        ]

    def test_voreinstellung_haelt_den_fall_vom_02_09_aus(self):
        """140 Denkschritte vor dem ersten Aufruf -- genau die Lage, die scheiterte."""
        speicher = Ablaufspeicher()
        for ereignis in _denken(140):
            speicher.anhaengen(ereignis)
        speicher.anhaengen({"type": "tool_start", "name": "mcp_datenraum_datenraum_katalog"})
        speicher.anhaengen({"type": "tool_start", "name": "skill_view"})
        for ereignis in _denken(60):
            speicher.anhaengen(ereignis)
        speicher.anhaengen({"type": "tool_start", "name": "mcp_sandbox_execute_code"})

        namen = [e["name"] for e in speicher.ereignisse if e["type"] == "tool_start"]
        assert "mcp_sandbox_execute_code" in namen


class TestDenkmodusHinweis:
    """Gewarnt wird am gescheiterten Lauf, nicht am Wortlaut der Frage."""

    def test_ohne_denkmodus_und_mit_codefehler_wird_gewarnt(self):
        hinweis = denkmodus_hinweis("aus", 18)
        assert "Denkmodus" in hinweis
        assert "18" in hinweis

    def test_einzahl_bleibt_lesbar(self):
        assert "1 Code-Ausführung ist dabei gescheitert" in denkmodus_hinweis("aus", 1)

    def test_ohne_codefehler_kein_hinweis(self):
        """Ein Lauf ohne Denkmodus, der durchlief, braucht keine Belehrung."""
        assert denkmodus_hinweis("aus", 0) == ""

    def test_mit_denkmodus_kein_hinweis(self):
        assert denkmodus_hinweis("kurz", 5) == ""
        assert denkmodus_hinweis("lang", 18) == ""


class TestZeitlimitGrund:
    """Ein Zeitlimit ohne Grund laesst den Nutzer die falsche Konsequenz ziehen."""

    def test_festgefahren_wird_benannt(self):
        meldung = zeitlimit_grund(600, 15)
        assert "600s" in meldung
        assert "15 Code-Ausführungen" in meldung
        assert "enger" in meldung

    def test_ohne_haeufung_bleibt_die_meldung_schlicht(self):
        """Ein Lauf, der schlicht zu gross war, soll nicht als Fehlschlag dastehen."""
        meldung = zeitlimit_grund(600, 1)
        assert meldung == "InnoPilot hat das Zeitlimit überschritten (600s)"

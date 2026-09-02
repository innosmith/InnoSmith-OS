"""Prüft, dass Prompt, Werkzeugbeschreibungen und Registrierung dasselbe sagen.

Agentenverhalten entsteht aus drei Quellen: dem Chat-Prompt, den Beschreibungen der
MCP-Werkzeuge und der Toolset-Registrierung. Widersprechen sie sich, entscheidet das
Modell -- und keiner der drei Orte ist für sich falsch, weshalb es kein Test bemerkt,
der nur einen davon liest.

Konkret abgesichert: Der Chat-Prompt wies bis zum 02.09.2026 für Buchhaltungsfragen
auf ``list_invoices`` und ``search_invoices``. Beide liefern nur eine Seite, und
``list_invoices`` nahm zusätzlich einen wirkungslosen Kundenfilter entgegen. Der
Agent folgte der Anweisung korrekt und bekam trotzdem eine falsche Zahl.
"""

import os
import re
import sys

import pytest

BACKEND = os.path.join(os.path.dirname(__file__), "..")
SRC = os.path.join(BACKEND, "..")
sys.path.insert(0, BACKEND)


def _werkzeugnamen(server_pfad: str) -> set[str]:
    """Toolnamen aus einer MCP-Server-Datei lesen, ohne sie zu importieren.

    Ein Import zöge die MCP-Bibliothek und die Zugangsdaten nach; für einen
    Konsistenztest genügt der Quelltext.
    """
    with open(server_pfad, encoding="utf-8") as f:
        return set(re.findall(r'name="([a-z_]+)"', f.read()))


class TestRegistrierung:
    def test_datenraum_ist_als_mcp_server_bekannt(self):
        from app.services.hermes_worker import _KNOWN_MCP_SERVERS

        assert "datenraum" in _KNOWN_MCP_SERVERS

    def test_datenraum_steht_in_der_hermes_config(self):
        from app.services.hermes_config import build_config_dict

        server = build_config_dict()["mcp_servers"]
        assert "datenraum" in server
        assert server["datenraum"]["args"][0].endswith("mcp-datenraum/server.py")

    def test_recherchelauf_kennt_datenraum_und_sandbox(self):
        """Der Sammel-Lauf soll Zahlen aus dem Datenraum holen, nicht aus dem Mailarchiv."""
        from app.services.hermes_worker import _GATHER_MCP_SERVERS_WIDE

        assert {"datenraum", "sandbox"} <= set(_GATHER_MCP_SERVERS_WIDE)


class TestWerkzeugnamen:
    """Was der Prompt nennt, muss es auch geben."""

    def test_datenraum_werkzeuge_existieren(self):
        namen = _werkzeugnamen(os.path.join(SRC, "mcp-datenraum", "server.py"))
        assert namen == {"datenraum_katalog", "datenraum_auffrischen"}

    def test_beschreibungen_nennen_nur_echte_bexio_werkzeuge(self):
        from app.routers.chat import MCP_SERVER_DESCRIPTIONS

        echte = _werkzeugnamen(os.path.join(SRC, "mcp-bexio", "server.py"))
        text = MCP_SERVER_DESCRIPTIONS["bexio"]["tools"]
        genannte = set(re.findall(r"\b([a-z_]+)\(", text))
        assert genannte <= echte, f"nicht vorhanden: {sorted(genannte - echte)}"

    def test_beschreibungen_nennen_nur_echte_datenraum_werkzeuge(self):
        from app.routers.chat import MCP_SERVER_DESCRIPTIONS

        echte = _werkzeugnamen(os.path.join(SRC, "mcp-datenraum", "server.py"))
        text = MCP_SERVER_DESCRIPTIONS["datenraum"]["tools"]
        genannte = {n for n in re.findall(r"\b(datenraum_[a-z_]+)\(", text)}
        assert genannte <= echte


class TestPromptLeitplanken:
    """Geprüft wird die Vorlage im Quelltext.

    ``_build_agent_prompt`` ist asynchron und lädt Regeln und Rückschau aus der
    Datenbank; für die Frage, was in der Anweisung steht, ist das unnötiger Aufbau.
    """

    @pytest.fixture(scope="class")
    def prompt(self):
        with open(os.path.join(BACKEND, "app", "routers", "chat.py"), encoding="utf-8") as f:
            quelle = f.read()
        anfang = quelle.index("Du bist InnoPilot")
        return quelle[anfang:anfang + 6000]

    def test_prompt_lenkt_zahlen_in_den_datenraum(self, prompt):
        assert "/daten/" in prompt
        assert "datenraum_katalog" in prompt

    def test_prompt_nennt_den_stand_als_pflicht(self, prompt):
        """Ohne Datumsangabe ist eine gecachte Zahl von einer frischen ununterscheidbar."""
        assert "Stand" in prompt

    def test_prompt_bietet_list_invoices_nicht_fuer_umsatz_an(self, prompt):
        """list_invoices liefert eine Seite und filtert nicht nach Kunde."""
        for zeile in prompt.splitlines():
            if "list_invoices" in zeile:
                assert "Umsatz" not in zeile


class TestSandboxWeissVomDatenraum:
    def test_execute_code_beschreibt_daten_verzeichnis(self):
        with open(os.path.join(SRC, "mcp-sandbox", "server.py"), encoding="utf-8") as f:
            quelle = f.read()
        assert "/daten/" in quelle
        assert "duckdb" in quelle

    def test_executor_haengt_datenraum_bei_jedem_lauf_ein(self):
        """Ohne Bedingung auf workspace_key -- der Schlüssel kommt vom Modell."""
        with open(os.path.join(SRC, "sandbox-executor", "executor.py"), encoding="utf-8") as f:
            quelle = f.read()
        assert '"-v", f"{DATENRAUM_DIR}:/daten:ro"' in quelle
        assert "CONV_TTL_SECONDS" in quelle, "persistente Scopes brauchen eine Frist"

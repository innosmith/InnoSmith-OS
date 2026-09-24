"""Werkzeugnamen werden von Hermes abgeleitet, nie abgeschrieben.

Hermes 0.21 stellte von ``mcp_<server>_<tool>`` auf ``mcp__<server>__<tool>`` um.
Prompt, Skills, Callback-Konstanten und Tests trugen die alte Form als Abschrift,
und weil die Tests dieselbe Abschrift pruefen, blieb alles gruen, waehrend die
Triage drei Wochen lang am Schleifenwaechter abbrach. Diese Tests pruefen darum
gegen Hermes und gegen die Server, die die Werkzeuge tatsaechlich anbieten.
"""

import re
from pathlib import Path

import pytest
from tools.mcp_tool_schema import mcp_prefixed_tool_name

from app.services.hermes_worker import _KNOWN_MCP_SERVERS
from app.services.tool_names import mcp_server_of, mcp_tool

_SRC = Path(__file__).resolve().parents[2]
_APP = _SRC / "backend" / "app"

# Alte Form: genau ein Unterstrich zwischen Praefix, Server und Werkzeug.
_ABGESCHRIEBEN = re.compile(r"\bmcp_(?:" + "|".join(_KNOWN_MCP_SERVERS) + r")_[a-z*]")
_GENANNT = re.compile(r"\bmcp__([A-Za-z]+)__([a-z_]+)")


def _server_dir(server: str) -> Path:
    return _SRC / ("mcp-graph" if server == "graphAdmin" else f"mcp-{server}")


def _angebotene_werkzeuge(server: str) -> set[str] | None:
    quelle = _server_dir(server) / "server.py"
    if not quelle.exists():
        return None
    return set(re.findall(r'^\s*name="([a-z_]+)",', quelle.read_text(), re.M))


class TestAbleitung:
    @pytest.mark.parametrize("server", _KNOWN_MCP_SERVERS)
    def test_gleich_wie_hermes(self, server):
        assert mcp_tool(server, "get_email") == mcp_prefixed_tool_name(server, "get_email")

    @pytest.mark.parametrize("server", _KNOWN_MCP_SERVERS)
    def test_server_zurueckgewinnbar(self, server):
        assert mcp_server_of(mcp_tool(server, "get_email")) == server

    def test_server_auch_bei_gekuerztem_namen(self):
        """Hermes kuerzt Namen ueber 64 Zeichen mit Hash-Suffix -- der Server bleibt."""
        lang = mcp_tool("taskpilot", "x" * 80)
        assert len(lang) <= 64
        assert mcp_server_of(lang) == "taskpilot"

    @pytest.mark.parametrize("name", ["web_search", "skill_view", "mcp_graph_get_email", ""])
    def test_andere_werkzeuge_haben_keinen_server(self, name):
        assert mcp_server_of(name) is None


class TestKeineAbschrift:
    def test_backend_code(self):
        treffer = [
            f"{p.relative_to(_SRC)}:{i}"
            for p in _APP.rglob("*.py")
            for i, zeile in enumerate(p.read_text().splitlines(), 1)
            if _ABGESCHRIEBEN.search(zeile)
        ]
        assert not treffer, f"Abgeschriebene Werkzeugnamen: {treffer}"

    def test_ausgerollte_skills(self):
        from app.services.hermes_config import get_hermes_home

        skills = get_hermes_home() / "skills"
        if not skills.is_dir():
            pytest.skip("Keine Skills ausgerollt")
        treffer = [
            f"{p.relative_to(skills)}:{i}"
            for p in skills.rglob("*.md")
            for i, zeile in enumerate(p.read_text().splitlines(), 1)
            if _ABGESCHRIEBEN.search(zeile)
        ]
        assert not treffer, f"Abgeschriebene Werkzeugnamen in Skills: {treffer}"


class TestGenannteWerkzeugeExistieren:
    """Ein richtig gebildeter Name nuetzt nichts, wenn der Server das Werkzeug nicht anbietet."""

    def _pruefe(self, texte: dict[str, str]):
        fehlend = []
        for herkunft, text in texte.items():
            for server, werkzeug in _GENANNT.findall(text):
                angeboten = _angebotene_werkzeuge(server)
                if angeboten is not None and werkzeug not in angeboten:
                    fehlend.append(f"{herkunft}: mcp__{server}__{werkzeug}")
        assert not fehlend, f"Nicht angebotene Werkzeuge: {fehlend}"

    def test_ausgerollte_skills(self):
        from app.services.hermes_config import get_hermes_home

        skills = get_hermes_home() / "skills"
        if not skills.is_dir():
            pytest.skip("Keine Skills ausgerollt")
        self._pruefe({str(p.relative_to(skills)): p.read_text() for p in skills.rglob("*.md")})

    def test_konstanten_im_worker(self):
        import app.services.hermes_worker as hw

        namen = {*hw._CREATE_DRAFT_TOOLS, *hw._CONTEXT_SEARCH_TOOLS, hw._MOVE_EMAIL_TOOL}
        self._pruefe({"hermes_worker": " ".join(namen)})

"""Das Tor vor der Cloud im Chat-Pfad.

Der Anlass ist ein Leck, kein Wunsch: Bis zum 25.08.2026 hatte der Worker eine
Schleuse und der Chat keine. Wer im Chat ein Cloud-Modell waehlte, schickte
Frage, Verlauf und angeheftete Dokumente im Klartext hinaus -- ueber genau den
Weg, den ein Mensch am haeufigsten benutzt.

Geprueft wird die Behauptung des Tors, nicht seine Bequemlichkeit: **Kein Weg
traegt Klartext zu einem fremden Modell.** Dazu die zwei Unterscheidungen, die
das Tor ausmachen -- Verlauf und Korpus gehen mit, und ein Restbestand wird hier
gemeldet statt abgebrochen, weil ein Mensch davor sitzt.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.routers.chat import _tor_nach_draussen

pytestmark = pytest.mark.asyncio

KLARTEXT = "Was schuldet die Muster AG?"
DOSSIER = {"role": "user", "content": "Angepinnt: Kreditorenliste der Muster AG"}
VERLAUF = [DOSSIER, {"role": "assistant", "content": "Die Muster AG hat 4200 offen."}]


def _maskierung(**kw):
    """Patcht die Anonymisierungsstrecke, ueber die die Schleuse geht."""
    return patch("app.services.anon_politik.maskiere", **kw)


def _echte_maskierung(text, sitzung="S1", reste=()):
    """Eine Attrappe, die den Trenner respektiert -- so wie contentConverter."""
    return AsyncMock(return_value=(text.replace("Muster AG", "Hess & Partner"), sitzung, [], list(reste)))


class TestLokal:
    async def test_lokales_modell_geht_unveraendert_durch(self):
        maskieren = AsyncMock()
        with _maskierung(new=maskieren):
            prompt, verlauf, sitzung, reste = await _tor_nach_draussen(
                modell="ollama/qwen3.6:latest", prompt=KLARTEXT,
                verlauf=VERLAUF, freigegeben=False,
            )
        assert prompt == KLARTEXT
        assert verlauf == VERLAUF
        assert sitzung is None and reste == []
        maskieren.assert_not_awaited()

    async def test_platzhalter_gilt_als_lokal(self):
        """``hermes`` ist kein Modellname, sondern «das eingestellte lokale»."""
        maskieren = AsyncMock()
        with _maskierung(new=maskieren):
            _, _, sitzung, _ = await _tor_nach_draussen(
                modell="hermes", prompt=KLARTEXT, verlauf=[], freigegeben=False,
            )
        assert sitzung is None
        maskieren.assert_not_awaited()


class TestAuswaerts:
    async def test_verlauf_und_korpus_gehen_mit_durch_die_maskierung(self):
        """Wer nur die Frage maskiert, schickt das Dossier trotzdem hinaus.

        Das angepinnte Dokument steht als erste Runde im Verlauf -- genau dort,
        wo eine Maskierung, die nur den Prompt kennt, nicht hinsieht.
        """
        with _maskierung(new=_echte_maskierung(f"{KLARTEXT}\u241e{DOSSIER['content']}\u241eegal")):
            prompt, verlauf, sitzung, _ = await _tor_nach_draussen(
                modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                verlauf=VERLAUF, freigegeben=False,
            )
        assert "Muster AG" not in prompt
        assert all("Muster AG" not in m["content"] for m in verlauf)
        assert sitzung == "S1"

    async def test_die_rollen_bleiben_erhalten(self):
        with _maskierung(new=_echte_maskierung(f"a\u241eb\u241ec")):
            _, verlauf, _, _ = await _tor_nach_draussen(
                modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                verlauf=VERLAUF, freigegeben=False,
            )
        assert [m["role"] for m in verlauf] == ["user", "assistant"]

    async def test_gescheiterte_maskierung_laesst_nichts_hinaus(self):
        """Zeigen kann man nur, was es gibt -- hier gibt es keinen Text."""
        with _maskierung(side_effect=RuntimeError("Erkennung weg")):
            with pytest.raises(HTTPException) as exc:
                await _tor_nach_draussen(
                    modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                    verlauf=[], freigegeben=False,
                )
        assert exc.value.status_code == 503

    async def test_verschluckter_trenner_bricht_ab(self):
        """Ein falsch zugeordneter Verlauf ist schlimmer als eine Absage.

        Die Maskierung koennte den Trenner treffen. Welcher Abschnitt dann
        welcher ist, waere geraten -- und die Antwort bezoege sich auf ein
        Gespraech, das so nie gefuehrt wurde.
        """
        with _maskierung(new=AsyncMock(return_value=("alles zusammen", "S1", [], []))):
            with pytest.raises(HTTPException) as exc:
                await _tor_nach_draussen(
                    modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                    verlauf=VERLAUF, freigegeben=False,
                )
        assert exc.value.status_code == 503


class TestRestbestaende:
    """Beaufsichtigt heisst: der Mensch entscheidet, nicht das Tor."""

    async def test_ohne_freigabe_wird_gefragt(self):
        with _maskierung(new=_echte_maskierung(f"a\u241eb", reste=["Muster"])):
            with pytest.raises(HTTPException) as exc:
                await _tor_nach_draussen(
                    modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                    verlauf=[VERLAUF[0]], freigegeben=False,
                )
        assert exc.value.status_code == 409
        assert exc.value.detail["code"] == "anon_review"
        assert exc.value.detail["residuals"] == ["Muster"]
        assert exc.value.detail["preview"] == "a"

    async def test_mit_freigabe_geht_es_durch(self):
        with _maskierung(new=_echte_maskierung(f"a\u241eb", reste=["Muster"])):
            prompt, _, sitzung, reste = await _tor_nach_draussen(
                modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                verlauf=[VERLAUF[0]], freigegeben=True,
            )
        assert prompt == "a"
        assert sitzung == "S1"
        assert reste == ["Muster"]

    async def test_freigabe_hebelt_den_harten_fehler_nicht_aus(self):
        """Freigegeben wird ein Befund, nicht eine ausgefallene Maskierung."""
        with _maskierung(side_effect=RuntimeError("Erkennung weg")):
            with pytest.raises(HTTPException) as exc:
                await _tor_nach_draussen(
                    modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                    verlauf=[], freigegeben=True,
                )
        assert exc.value.status_code == 503


@pytest.mark.parametrize("freigegeben", [True, False])
@pytest.mark.parametrize("reste", [[], ["Muster"]])
async def test_kein_weg_traegt_klartext_zu_einem_fremden_modell(freigegeben, reste):
    """Die eigentliche Behauptung dieses Moduls, ueber alle Zustaende geprueft."""
    with _maskierung(new=_echte_maskierung(f"{KLARTEXT}\u241e{DOSSIER['content']}", reste=reste)):
        try:
            prompt, verlauf, _, _ = await _tor_nach_draussen(
                modell="anthropic/claude-opus-5", prompt=KLARTEXT,
                verlauf=[DOSSIER], freigegeben=freigegeben,
            )
        except HTTPException:
            return  # abgewiesen ist immer sicher
    assert "Muster AG" not in prompt
    assert all("Muster AG" not in m["content"] for m in verlauf)

"""Die Abbildung der drei Denkstufen auf das, was ein Anbieter versteht.

Geprueft wird nicht, dass die Werte «richtig» sind -- das entscheidet der
Anbieter --, sondern dass kein Anbieter einen Parameter bekommt, den er
zurueckweist, und dass ein unbekannter Anbieter gar keinen bekommt.
"""

import pytest

from app.services import denkstufen


class TestNormalisieren:
    @pytest.mark.parametrize("eingabe", [None, "", "quatsch", "AN"])
    def test_unbekanntes_wird_zur_vorgabe(self, eingabe):
        assert denkstufen.normalisiere(eingabe) == denkstufen.STANDARD

    @pytest.mark.parametrize("stufe", ["aus", "kurz", "lang"])
    def test_gueltiges_bleibt(self, stufe):
        assert denkstufen.normalisiere(stufe) == stufe

    def test_grossschreibung_und_leerzeichen(self):
        assert denkstufen.normalisiere("  Kurz ") == "kurz"


class TestOllama:
    """Beide Schalter, weil keiner bei allen Laufzeiten wirkt."""

    def test_lang_setzt_beide_schalter(self):
        ov = denkstufen.request_overrides("lang", "qwen3.6:latest")
        assert ov["reasoning_effort"] == "high"
        assert ov["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True

    def test_aus_setzt_beide_schalter(self):
        ov = denkstufen.request_overrides("aus", "qwen3.6:latest")
        assert ov["reasoning_effort"] == "none"
        assert ov["extra_body"]["chat_template_kwargs"]["enable_thinking"] is False

    def test_praefix_zaehlt_auch_als_ollama(self):
        assert denkstufen.request_overrides("kurz", "ollama/qwen3.6:latest") == (
            denkstufen.request_overrides("kurz", "qwen3.6:latest")
        )

    def test_platzhalter_gelten_als_lokal(self):
        """``hermes`` und ``nanobot`` meinen «das eingestellte lokale Modell»."""
        assert denkstufen.request_overrides("kurz", "hermes")["reasoning_effort"] == "low"


class TestAnthropic:
    def test_denken_verlangt_temperatur_eins(self):
        """Extended Thinking mit einem anderen Wert wird abgelehnt, nicht ignoriert."""
        assert denkstufen.request_overrides("lang", "anthropic/claude-opus-5")["temperature"] == 1.0

    def test_aus_setzt_nichts(self):
        assert denkstufen.request_overrides("aus", "anthropic/claude-opus-5") == {}

    def test_thinking_steht_nicht_auf_oberster_ebene(self):
        """Der Weg zu Anthropic führt über den LiteLLM-Proxy und damit über das
        OpenAI-SDK. Stand ``thinking`` dort oben, brach jeder Lauf sofort mit
        ``unexpected keyword argument 'thinking'`` ab -- vor dem ersten
        Werkzeugaufruf, also genau bei einer Vorführung."""
        for stufe in ("kurz", "lang"):
            ov = denkstufen.request_overrides(stufe, "anthropic/claude-sonnet-5")
            assert "thinking" not in ov

    @pytest.mark.parametrize("modell", [
        "anthropic/claude-opus-5", "anthropic/claude-sonnet-5",
        "anthropic/claude-fable-5-1", "anthropic/claude-opus-4-8",
        "anthropic/claude-opus-4-7", "anthropic/claude-sonnet-4-6",
        "anthropic/claude-modell-das-es-noch-nicht-gibt",
    ])
    def test_aktuelle_modelle_bekommen_die_neue_form(self, modell):
        """Am 03.09.2026 gemessen: alles ab 4.6 lehnt das Token-Budget ab."""
        ov = denkstufen.request_overrides("kurz", modell)["extra_body"]
        assert ov["thinking"]["type"] == "adaptive"
        assert ov["output_config"]["effort"] == "low"

    @pytest.mark.parametrize("modell", [
        "anthropic/claude-opus-4-5-20251101",
        "anthropic/claude-haiku-4-5-20251001",
        "anthropic/claude-sonnet-4-5-20250929",
    ])
    def test_auslaufende_modelle_bekommen_die_alte_form(self, modell):
        """Sie lehnen umgekehrt die neue Form ab -- deshalb überhaupt zwei Wege."""
        ov = denkstufen.request_overrides("lang", modell)["extra_body"]
        assert ov["thinking"] == {"type": "enabled", "budget_tokens": 8192}


class TestJederParameterIstZustellbar:
    """Der Wächter gegen die Gattung Fehler, die diese Datei still brechen lässt.

    Alle drei Wege -- lokal wie Cloud -- münden im OpenAI-SDK, weil Ollama eine
    kompatible Schnittstelle anbietet und der LiteLLM-Proxy ebenfalls. Dessen
    ``Completions.create()`` nimmt ausschliesslich die Parameter entgegen, die es
    kennt; alles andere gehört in ``extra_body`` und wandert von dort unverändert
    in den Rumpf der Anfrage.

    Die alte Fassung dieses Tests prüfte bloss, dass ``thinking`` gesetzt wird --
    sie war grün, während jeder Anthropic-Lauf mit Denken sofort abbrach. Eine
    Behauptung über die Form genügt nicht, wenn niemand prüft, ob die Form
    zustellbar ist.
    """

    @staticmethod
    def _erlaubte_parameter() -> set[str]:
        import inspect

        from openai.resources.chat.completions import Completions

        return set(inspect.signature(Completions.create).parameters) - {"self"}

    @pytest.mark.parametrize("modell", [
        "qwen3.6:latest",
        "ollama/qwen3.6:latest",
        "anthropic/claude-opus-5",
        "anthropic/claude-sonnet-5",
        "openai/gpt-5.6-sol",
        "gemini/gemini-3.8-flash",
        "perplexity/sonar",
    ])
    @pytest.mark.parametrize("stufe", ["aus", "kurz", "lang"])
    def test_kein_parameter_den_das_sdk_zurueckweist(self, modell, stufe):
        erlaubt = self._erlaubte_parameter()
        ov = denkstufen.request_overrides(stufe, modell)
        unbekannt = set(ov) - erlaubt
        assert not unbekannt, (
            f"{modell}/{stufe}: {sorted(unbekannt)} kennt das OpenAI-SDK nicht -- "
            "gehört in extra_body"
        )


class TestOpenAI:
    def test_nicht_abschaltbar(self):
        """Ein Schalter, der nichts tut, waere ein Versprechen ohne Deckung."""
        assert not denkstufen.abschaltbar("openai/gpt-5.5")

    def test_aus_sendet_keinen_wirkungslosen_wert(self):
        assert denkstufen.request_overrides("aus", "openai/gpt-5.5") == {}

    def test_stufe_wird_zu_reasoning_effort(self):
        assert denkstufen.request_overrides("lang", "openai/gpt-5.5") == {
            "reasoning_effort": "high"
        }


class TestUnbekannt:
    @pytest.mark.parametrize("modell", ["gemini/pro-3", "perplexity/sonar", "was/auch/immer"])
    @pytest.mark.parametrize("stufe", ["aus", "kurz", "lang"])
    def test_kein_geratener_parameter(self, modell, stufe):
        """Ein erfundener Parameter wird entweder abgelehnt oder still verworfen.

        Im zweiten Fall glaubt die Oberflaeche an eine Wirkung, die es nicht
        gibt -- und das ist die schlimmere der beiden Moeglichkeiten.
        """
        assert denkstufen.request_overrides(stufe, modell) == {}

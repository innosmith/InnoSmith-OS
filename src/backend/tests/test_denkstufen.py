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
        ov = denkstufen.request_overrides("lang", "anthropic/claude-opus-5")
        assert ov["temperature"] == 1.0
        assert ov["thinking"]["budget_tokens"] == 8192

    def test_kurz_bekommt_ein_kleineres_budget(self):
        ov = denkstufen.request_overrides("kurz", "anthropic/claude-opus-5")
        assert ov["thinking"]["budget_tokens"] < 8192

    def test_aus_setzt_nichts(self):
        assert denkstufen.request_overrides("aus", "anthropic/claude-opus-5") == {}


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

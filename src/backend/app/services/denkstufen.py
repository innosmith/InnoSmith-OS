"""Wie ein Modell zum Denken gebracht wird -- die Abbildung fuer den Chat.

## Geltungsbereich

Diese Datei bedient die **Stufe, die ein Mensch waehlt**: den Denkmodus einer
Unterhaltung. Sie ist bewusst nicht die einzige Stelle im Backend, die
Reasoning-Parameter setzt, und das ist kein Versehen.

Der Schreib-Pass (``_draft_sampling_overrides``) und der lokale Job-Override
(``_local_override_request``) im Hermes-Worker fahren eigene, **gemessene**
Werte -- ``draft_reasoning_effort``, ``draft_top_k``, Temperatur und
Presence-Penalty aus der Config, ermittelt an konkreten Modellen auf der GX10.
Sie hier einzugemeinden hiesse, Messwerte durch eine allgemeine Abbildung zu
ersetzen und Konfigurierbarkeit gegen Einheitlichkeit zu tauschen. Dieselbe
Ueberlegung gilt fuer ``analysis.py``, das ueber LiteLLM statt ueber Hermes geht.

Wenn diese Wege irgendwann dieselbe Frage stellen wie der Chat -- «wie stark
soll gedacht werden» statt «welche Sampling-Werte haben sich bewaehrt» --,
gehoeren sie hierher. Vorher nicht.

## Warum kein Anbieter dieselbe Sprache spricht

Es gibt keinen gemeinsamen Parameter. Ollama versteht ``reasoning_effort``
inklusive des Werts ``none``, den kein Cloud-Anbieter kennt. vLLM versteht
``chat_template_kwargs.enable_thinking`` und Ollama ignoriert es stillschweigend
-- weshalb der Schreib-Pass **beide** setzen muss. Anthropic verlangt ein
Budget in Tokens statt einer Stufe. OpenAIs Reasoning-Modelle lassen sich
ueberhaupt nicht abschalten.

Die Oberflaeche darf davon nichts wissen. Sie kennt drei Stufen, und was daraus
wird, entscheidet diese Datei.

## Unbekannt heisst nichts setzen

Ein Anbieter, der hier nicht steht, bekommt ein leeres Woerterbuch und damit das
Verhalten seiner eigenen Vorgabe. Geraten wird nicht: Ein erfundener Parameter
wird entweder mit einem Fehler quittiert -- laut, aber laestig -- oder
stillschweigend verworfen, und dann glaubt die Oberflaeche an eine Wirkung, die
es nicht gibt.
"""

from __future__ import annotations

from typing import Literal

Stufe = Literal["aus", "kurz", "lang"]

STUFEN: tuple[str, ...] = ("aus", "kurz", "lang")

STANDARD: Stufe = "lang"
"""Vorgabe fuer neue Unterhaltungen.

Denken ist in TaskPilot sichtbar und ein Teil dessen, was das Produkt zeigt --
darum an, bis jemand es abschaltet.
"""

_OLLAMA_EFFORT: dict[str, str] = {"aus": "none", "kurz": "low", "lang": "high"}
"""Deckungsgleich mit ``ai9.hermes.DENKSTUFEN``.

Bewusst hier wiederholt statt importiert: Dieses Modul muss auch ohne das
Hermes-Extra ladbar sein, und die Abbildung ist drei Zeilen -- der Import waere
teurer als die Wiederholung.
"""

_ANTHROPIC_BUDGET: dict[str, int] = {"kurz": 2048, "lang": 8192}
"""Denkbudget in Tokens -- die alte Form, bis Claude 4.5."""

_ANTHROPIC_EFFORT: dict[str, str] = {"kurz": "low", "lang": "high"}
"""Denkstaerke als Stufe -- die neue Form, ab Claude 4.6.

Am 03.09.2026 gegen den Proxy gemessen: 4.5 und aelter nehmen nur die alte Form,
4.7 und neuer nur die neue, 4.6 beide. Die Grenze liegt also zwischen 4.5 und 4.6,
nicht beim Generationswechsel -- opus-4-7 verhaelt sich wie opus-5.
"""


def _anbieter(modell: str) -> str:
    """Der Anbieter aus dem Modellstring (``anthropic/claude-…`` -> ``anthropic``).

    Ohne Praefix gilt das Modell als lokal: So heissen die Modelle, die direkt
    gegen Ollama laufen (``qwen3.6:latest``), und so heissen die Platzhalter
    ``hermes``/``nanobot``.
    """
    name = (modell or "").strip()
    if "/" not in name:
        return "ollama"
    return name.split("/", 1)[0].lower()


def _anthropic_version(modell: str) -> tuple[int, int]:
    """Version aus ``claude-opus-4-7`` bzw. ``claude-sonnet-5``.

    Unbekanntes gilt als neu: die neue Form nehmen alle aktuellen Modelle an, die
    alte nur die auslaufenden. Wer raet, raet damit in die haltbare Richtung.
    """
    import re

    treffer = re.search(r"claude-[a-z]+-(\d+)(?:-(\d+))?", modell or "")
    if not treffer:
        return (99, 0)
    return (int(treffer.group(1)), int(treffer.group(2) or 0))


def normalisiere(stufe: str | None) -> Stufe:
    """Macht aus einem beliebigen Eingabewert eine gueltige Stufe."""
    wert = (stufe or "").strip().lower()
    return wert if wert in STUFEN else STANDARD  # type: ignore[return-value]


def abschaltbar(modell: str) -> bool:
    """Ob sich das Denken bei diesem Modell ueberhaupt abschalten laesst.

    OpenAIs Reasoning-Modelle denken immer. Ein Schalter, der das verspricht,
    ist schlimmer als keiner -- die Oberflaeche muss den Unterschied zeigen
    koennen, statt ihn zu behaupten.
    """
    return _anbieter(modell) != "openai"


def request_overrides(stufe: str | None, modell: str) -> dict:
    """Was in ``request_overrides`` muss, damit dieses Modell so denkt.

    Liefert ein Woerterbuch, das direkt auf ``agent.request_overrides`` gelegt
    oder in einen LLM-Aufruf gemischt werden kann. Leer heisst: Vorgabe des
    Anbieters gilt.
    """
    gewaehlt = normalisiere(stufe)
    anbieter = _anbieter(modell)

    if anbieter == "ollama":
        # Beide Schalter, weil keiner bei allen Laufzeiten wirkt: Ollama wertet
        # ``reasoning_effort`` aus und ignoriert ``chat_template_kwargs``, vLLM
        # genau andersherum.
        out: dict = {"reasoning_effort": _OLLAMA_EFFORT[gewaehlt]}
        out["extra_body"] = {"chat_template_kwargs": {"enable_thinking": gewaehlt != "aus"}}
        return out

    if anbieter == "anthropic":
        if gewaehlt == "aus":
            return {}
        # Extended Thinking verlangt temperature=1 -- ein anderer Wert wird mit
        # einem Fehler quittiert, nicht ignoriert.
        #
        # Beides muss in ``extra_body``: Der Weg zu Anthropic fuehrt ueber den
        # LiteLLM-Proxy und damit ueber das OpenAI-SDK, dessen
        # ``Completions.create()`` nur bekannte Schluesselwoerter annimmt. Stand
        # ``thinking`` oben, brach jeder Lauf sofort ab -- vor dem ersten
        # Werkzeugaufruf, also genau bei einer Vorfuehrung.
        if _anthropic_version(modell) >= (4, 6):
            zusatz = {"thinking": {"type": "adaptive"},
                      "output_config": {"effort": _ANTHROPIC_EFFORT[gewaehlt]}}
        else:
            zusatz = {"thinking": {"type": "enabled",
                                   "budget_tokens": _ANTHROPIC_BUDGET[gewaehlt]}}
        return {"temperature": 1.0, "extra_body": zusatz}

    if anbieter == "openai":
        if gewaehlt == "aus":
            # Nicht abschaltbar. Statt einen wirkungslosen Wert zu senden, wird
            # nichts gesetzt -- ``abschaltbar()`` sagt der Oberflaeche vorher,
            # dass hier nichts zu holen ist.
            return {}
        return {"reasoning_effort": "low" if gewaehlt == "kurz" else "high"}

    # Gemini, Perplexity und alles Kuenftige: keine Annahme.
    return {}


def prosa_ohne_denken(modell: str) -> dict:
    """Denken aus, fuer Passagen, die schreiben statt ueberlegen sollen.

    Kurzform fuer den Schreib-Pass und aehnliche Faelle. Steht hier und nicht
    beim Aufrufer, damit die Begruendung an der Abbildung klebt: Ohne
    Abschaltung erzeugt qwen fuer eine E-Mail von 150 Tokens mehrere Tausend
    Reasoning-Tokens und kann das Budget aufbrauchen, bis die Antwort leer ist.
    """
    return request_overrides("aus", modell)

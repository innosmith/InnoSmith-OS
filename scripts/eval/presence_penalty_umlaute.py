#!/usr/bin/env python3
"""Isoliertes Experiment: Verursacht ``presence_penalty`` fehlende Umlaute?

Hintergrund: Im Zwei-Pass-Entwurf setzt TaskPilot nur im Schreib-Pass
``presence_penalty=1.5`` (``config.draft_presence_penalty``). Genau dieser Pass
lieferte am 30.07.2026 einen Entwurf ohne jeden Umlaut («fr», «Rckmeldung»,
«Eintrge»), während die Klassifikation desselben Laufs korrektes Deutsch schrieb.
Qwen dokumentiert für ``presence_penalty`` ausdrücklich «language mixing and a
slight decrease in model performance».

Das Skript vergleicht zwei Arme, die sich AUSSCHLIESSLICH in
``presence_penalty`` unterscheiden (1.5 gegen 0.0). Beide Arme laufen mit
identischen Seeds, damit Unterschiede nicht auf Sampling-Zufall beruhen.

Zwei Messungen:

- **Test A (exakt)**: Das Modell soll einen vorgegebenen Text wortgetreu
  wiedergeben. Für jedes erwartete Umlaut-Wort wird geprüft, ob es korrekt oder
  in verstümmelter Form («für» -> «fr») erscheint. Kein Wörterbuch, keine
  Heuristik -- die Zielwörter sind bekannt.
- **Test B (Dichte)**: Freier Antwort-Entwurf wie in der Praxis. Gemessen wird
  die Umlaut-Dichte pro 100 Wörter und die Anzahl Samples ganz ohne Umlaut.

Bewusst ohne Import aus ``app`` und nur mit der Standardbibliothek, damit das
Experiment unabhängig vom Backend läuft.

Aufruf:
    python3 scripts/eval/presence_penalty_umlaute.py [--samples 15] [--model qwen3.6:latest]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

UMLAUTE = "äöüÄÖÜ"
_UMLAUT_RE = re.compile(f"[{UMLAUTE}]")
_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]+")

# ── Test A: wortgetreue Reproduktion ────────────────────────────────────────
# Der Zieltext ist bekannt, damit pro Wort exakt gemessen werden kann.

SOURCE_TEXT = (
    "Besten Dank für die Rückmeldung. Die DNS-Einträge sind möglich, "
    "natürlich prüfe ich das persönlich. Wir müssen die Anhänge zurückschicken "
    "und die Verfügbarkeit klären. Grüsse aus Bern und schöne Wochenmitte."
)

EXPECTED_WORDS = [
    "für", "Rückmeldung", "Einträge", "möglich", "natürlich", "prüfe",
    "persönlich", "müssen", "Anhänge", "zurückschicken", "Verfügbarkeit",
    "klären", "Grüsse", "schöne",
]

TEST_A_SYSTEM = (
    "Du gibst Text wortgetreu wieder. Keine Kommentare, keine Einleitung, "
    "keine Anführungszeichen -- nur der Text selbst."
)
TEST_A_USER = f"Gib den folgenden Text exakt und unverändert wieder:\n\n{SOURCE_TEXT}"

# ── Test B: freier Entwurf (Praxisfall) ─────────────────────────────────────

TEST_B_SYSTEM = (
    "Du schreibst E-Mail-Antworten auf Deutsch (Schweizer Hochdeutsch, ss statt "
    "ß, korrekte Umlaute ä/ö/ü). Antworte nur mit dem E-Mail-Text."
)
TEST_B_USER = (
    "Schreibe eine kurze, natürliche Antwort auf diese E-Mail.\n\n"
    "Von: Gabriel\n"
    "Betreff: Client-Zugang auf Server\n\n"
    "Hoi Anthony,\n"
    "Die AI-Hilfe vom DNS-Provider antwortet folgendes: Aktuell bieten wir keine "
    "DNS-API an, mit der du ein API-Token für die DNS-01-Challenge bei Let's "
    "Encrypt generieren könntest. Wir wissen, dass diese Funktion besonders "
    "wichtig ist.\n\n"
    "Inhalt der Antwort: Danke für die Rückmeldung, da die API-Variante nicht "
    "möglich ist, nutzen wir die CNAME-Alternative. Erkläre kurz, welche "
    "DNS-Einträge Gabriel auf seiner Seite setzen muss."
)


def strip_umlaute(word: str) -> str:
    """Entfernt Umlaut-Zeichen ersatzlos -- genau das beobachtete Schadensbild."""
    return _UMLAUT_RE.sub("", word)


@dataclass
class Sample:
    seed: int
    text: str
    elapsed_s: float
    finish_reason: str = ""
    error: str | None = None


@dataclass
class ArmResult:
    presence_penalty: float
    samples: list[Sample] = field(default_factory=list)


def call_ollama(
    base_url: str,
    model: str,
    system: str,
    user: str,
    presence_penalty: float,
    seed: int,
    max_tokens: int,
    timeout: int,
) -> tuple[str, str, float]:
    """Ein nicht-gestreamter Chat-Completion-Call gegen Ollamas OpenAI-Endpunkt.

    Die Parameter spiegeln ``_draft_sampling_overrides`` aus dem Backend:
    ``temperature``/``top_p``/``presence_penalty`` als Standardfelder, ``top_k``
    und ``chat_template_kwargs`` als provider-spezifische Zusätze (die der
    OpenAI-Client aus ``extra_body`` ebenfalls auf oberster Ebene sendet).

    Hinweis: ``chat_template_kwargs.enable_thinking`` wird von Ollamas
    OpenAI-Endpunkt nachweislich ignoriert -- das Modell liefert weiterhin einen
    ``reasoning``-Block. Das Feld bleibt trotzdem im Payload, weil das Backend es
    ebenso sendet; gemessen wird ausschliesslich ``content``. ``max_tokens`` ist
    entsprechend grosszügig, damit die Antwort nicht vom Reasoning verdrängt wird.
    """
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "presence_penalty": presence_penalty,
        "seed": seed,
        "max_tokens": max_tokens,
        "stream": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = (data.get("choices") or [{}])[0]
    content = (choice.get("message") or {}).get("content") or ""
    return content.strip(), choice.get("finish_reason") or "", time.time() - t0


def run_arm(
    label: str,
    presence_penalty: float,
    system: str,
    user: str,
    seeds: list[int],
    *,
    base_url: str,
    model: str,
    max_tokens: int,
    timeout: int,
) -> ArmResult:
    arm = ArmResult(presence_penalty=presence_penalty)
    for i, seed in enumerate(seeds, start=1):
        try:
            text, finish, elapsed = call_ollama(
                base_url, model, system, user, presence_penalty, seed, max_tokens, timeout
            )
            arm.samples.append(
                Sample(seed=seed, text=text, elapsed_s=elapsed, finish_reason=finish)
            )
            umlaute = len(_UMLAUT_RE.findall(text))
            status = f"ok ({len(text)} Zeichen, {umlaute} Umlaute, {finish}, {elapsed:.0f}s)"
            if finish == "length":
                status += "  ACHTUNG: abgeschnitten"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            arm.samples.append(Sample(seed=seed, text="", elapsed_s=0.0, error=str(exc)))
            status = f"FEHLER: {exc}"
        print(f"  [{label}] Sample {i}/{len(seeds)} (seed={seed}): {status}", flush=True)
    return arm


# ── Auswertung ──────────────────────────────────────────────────────────────


def analyse_test_a(sample: Sample) -> dict:
    """Exakte Wortprüfung: korrekt, verstümmelt oder gar nicht vorhanden."""
    text = sample.text
    correct, mutilated, missing = [], [], []
    for word in EXPECTED_WORDS:
        if word in text:
            correct.append(word)
            continue
        stripped = strip_umlaute(word)
        # Wortgrenzen, damit "fr" nicht in "frei" trifft.
        if re.search(rf"(?<![A-Za-zÄÖÜäöüß]){re.escape(stripped)}(?![A-Za-zÄÖÜäöüß])", text):
            mutilated.append(word)
        else:
            missing.append(word)
    return {
        "seed": sample.seed,
        "correct": len(correct),
        "mutilated": len(mutilated),
        "missing": len(missing),
        "mutilated_words": mutilated,
        "umlaut_count": len(_UMLAUT_RE.findall(text)),
    }


def analyse_test_b(sample: Sample) -> dict:
    """Dichte-Messung auf freiem Text: Umlaute pro 100 Wörter."""
    words = _WORD_RE.findall(sample.text)
    umlauts = len(_UMLAUT_RE.findall(sample.text))
    density = (umlauts / len(words) * 100) if words else 0.0
    return {
        "seed": sample.seed,
        "words": len(words),
        "umlaut_count": umlauts,
        "umlauts_per_100_words": round(density, 2),
        "zero_umlauts": umlauts == 0 and len(words) >= 20,
    }


def summarise(rows: list[dict], keys: list[str]) -> dict:
    out = {}
    for key in keys:
        values = [r[key] for r in rows if isinstance(r.get(key), (int, float))]
        out[key] = round(statistics.mean(values), 2) if values else 0.0
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=20, help="Samples pro Arm (Test A)")
    parser.add_argument("--samples-b", type=int, default=8, help="Samples pro Arm (Test B)")
    parser.add_argument("--model", default="qwen3.6:latest")
    parser.add_argument("--base-url", default="http://localhost:11434")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    print(f"Modell: {args.model} | Test A: {args.samples} Samples/Arm, Test B: {args.samples_b}")
    print("Identische Seeds in beiden Armen -- einziger Unterschied: presence_penalty\n")

    results: dict = {
        "model": args.model,
        "samples_a": args.samples,
        "samples_b": args.samples_b,
        "tests": {},
    }

    # Test B laeuft mit Reasoning (Ollama ignoriert enable_thinking auf /v1) und
    # braucht darum ein grosszuegiges Budget -- so wie in Produktion, wo kein
    # max_tokens gesetzt ist. Test A ist kurz und dicht an Umlauten.
    for test_name, system, user, max_tokens, n, analyse, keys in (
        ("A_wortgetreu", TEST_A_SYSTEM, TEST_A_USER, 2500, args.samples, analyse_test_a,
         ["correct", "mutilated", "missing", "umlaut_count"]),
        ("B_freier_entwurf", TEST_B_SYSTEM, TEST_B_USER, 8000, args.samples_b, analyse_test_b,
         ["words", "umlaut_count", "umlauts_per_100_words"]),
    ):
        seeds = list(range(1, n + 1))
        print(f"── Test {test_name} ({n} Samples/Arm) ──")
        results["tests"][test_name] = {}
        for pp in (1.5, 0.0):
            label = f"pp={pp}"
            arm = run_arm(
                label, pp, system, user, seeds,
                base_url=args.base_url, model=args.model,
                max_tokens=max_tokens, timeout=args.timeout,
            )
            rows = [analyse(s) for s in arm.samples if not s.error]
            errors = [s.error for s in arm.samples if s.error]
            summary = summarise(rows, keys)
            if test_name == "A_wortgetreu":
                summary["samples_mit_verstuemmelung"] = sum(1 for r in rows if r["mutilated"] > 0)
            else:
                summary["samples_ohne_umlaut"] = sum(1 for r in rows if r["zero_umlauts"])
            summary["fehler"] = len(errors)
            summary["abgeschnitten"] = sum(
                1 for s in arm.samples if s.finish_reason == "length"
            )
            results["tests"][test_name][label] = {
                "summary": summary,
                "rows": rows,
                "texts": {s.seed: s.text for s in arm.samples if not s.error},
            }
            print(f"  -> {label}: {summary}\n", flush=True)

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"presence_penalty_umlaute_{int(time.time())}.json"
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    print("═══ ERGEBNIS ═══")
    for test_name, arms in results["tests"].items():
        print(f"\n{test_name}:")
        for label, data in arms.items():
            print(f"  {label:9s} {data['summary']}")
    print(f"\nRohdaten: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

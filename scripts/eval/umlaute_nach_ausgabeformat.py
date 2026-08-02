#!/usr/bin/env python3
"""Experiment: Verliert das Modell Umlaute, wenn es Deutsch in HTML schreibt?

Ausgangslage: Im Job 89dfb9ea (30.07.2026) schrieb dasselbe Modell im selben
Lauf einwandfreies Deutsch als Fliesstext (``rationale``: «DNS/CNAME-Einträge
für …»), während der Antwort-Entwurf jeden Umlaut verlor («fr», «Rckmeldung»,
«Eintrge»). Der Entwurf ist HTML mit ``<p>``/``<strong>``/``<code>``.

Bereits ausgeschlossen (siehe ``check_sampling_params_honored.py``):
``presence_penalty`` ist bei diesem Modell/Ollama wirkungslos -- der Unterschied
liegt nicht an den Sampling-Parametern des Schreib-Passes.

Verbleibende Hypothese: Das geforderte Ausgabeformat verschiebt die Verteilung.
In Markup-/Code-Kontexten ist Nicht-ASCII in Trainingsdaten oft maskiert oder
vermieden; deutscher Fliesstext innerhalb von HTML könnte darum Umlaute
verlieren.

Drei Bedingungen, identische Aufgabe, identische Seeds, Produktions-Sampling
(temperature 0.7, top_p 0.8, top_k 20):

- ``plain``:    reiner Fliesstext
- ``html``:     HTML-Body wie im Schreib-Pass (Produktionsfall)
- ``markdown``: Markdown -- die Alternative, falls HTML der Auslöser ist

Metrik: Umlaute pro 100 Wörter sowie Anzahl Samples, in denen ein erwartetes
Umlaut-Wort in verstümmelter Form auftaucht («für» -> «fr»). Die Zielwörter sind
vorgegeben, es braucht also kein Wörterbuch.

Aufruf:
    python3 scripts/eval/umlaute_nach_ausgabeformat.py [--samples 12]
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
from pathlib import Path

_UMLAUT_RE = re.compile("[äöüÄÖÜ]")
_WORD_RE = re.compile(r"[A-Za-zÄÖÜäöüß]+")
_TAG_RE = re.compile(r"<[^>]+>")

# Wörter, die eine deutsche Antwort auf diese Mail mit hoher Wahrscheinlichkeit
# enthält. Geprüft wird jeweils korrekte gegen verstümmelte Schreibweise.
PROBE_WORDS = [
    "für", "Rückmeldung", "Einträge", "möglich", "natürlich", "müssen",
    "können", "würde", "prüfen", "zurück", "Grüsse", "schön", "hätte",
    "Verfügbarkeit", "benötigst", "änderst",
]

MAIL_KONTEXT = (
    "Von: Gabriel\n"
    "Betreff: Client-Zugang auf GX10 Server\n\n"
    "Hoi Anthony,\n"
    "Die AI-Hilfe vom DNS-Provider antwortet folgendes: Aktuell bieten wir keine "
    "DNS-API an, mit der du ein API-Token für die DNS-01-Challenge bei Let's "
    "Encrypt generieren könntest. Wir wissen, dass diese Funktion besonders "
    "wichtig ist, gerade für automatisierte Zertifikate.\n"
)

AUFGABE = (
    "Schreibe die Antwort von Anthony. Inhalt: Dank für die Rückmeldung; da die "
    "API-Variante nicht möglich ist, nutzt er die CNAME-Alternative. Nenne die "
    "DNS-Einträge, die Gabriel auf seiner Seite setzen muss, und erkläre kurz, "
    "was danach passiert."
)

SYSTEM_BASE = (
    "Du schreibst E-Mail-Antworten im Namen von Anthony Smith. Sprache: Deutsch "
    "(Schweizer Hochdeutsch, ss statt ß, korrekte Umlaute ä/ö/ü)."
)

BEDINGUNGEN = {
    "plain": (
        SYSTEM_BASE + " Antworte ausschliesslich mit dem reinen E-Mail-Text, "
        "ohne Formatierung und ohne Markup.",
        AUFGABE,
    ),
    "html": (
        SYSTEM_BASE + " Antworte ausschliesslich mit dem HTML-Body der E-Mail "
        "(<p>, <strong>, <code> wo sinnvoll), ohne Kommentar.",
        AUFGABE + "\n\nGib den vollständigen HTML-Body aus.",
    ),
    "markdown": (
        SYSTEM_BASE + " Antworte ausschliesslich mit dem E-Mail-Text in Markdown, "
        "ohne Kommentar.",
        AUFGABE + "\n\nGib den Text als Markdown aus.",
    ),
}

BASE_URL = "http://localhost:11434"


def strip_umlaute(word: str) -> str:
    return _UMLAUT_RE.sub("", word)


def sichtbarer_text(raw: str) -> str:
    """Markup entfernen, damit Tag-Namen die Wortzählung nicht verfälschen."""
    return _TAG_RE.sub(" ", raw)


def call(model: str, system: str, user: str, seed: int, timeout: int) -> tuple[str, str]:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "seed": seed,
        "max_tokens": 8000,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{BASE_URL}/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    choice = (data.get("choices") or [{}])[0]
    return ((choice.get("message") or {}).get("content") or "").strip(), choice.get(
        "finish_reason"
    ) or ""


def analysiere(raw: str) -> dict:
    text = sichtbarer_text(raw)
    woerter = _WORD_RE.findall(text)
    umlaute = len(_UMLAUT_RE.findall(text))
    verstuemmelt = []
    for word in PROBE_WORDS:
        if word in text:
            continue
        stripped = strip_umlaute(word)
        if re.search(
            rf"(?<![A-Za-zÄÖÜäöüß]){re.escape(stripped)}(?![A-Za-zÄÖÜäöüß])", text
        ):
            verstuemmelt.append(word)
    return {
        "woerter": len(woerter),
        "umlaute": umlaute,
        "umlaute_pro_100": round(umlaute / len(woerter) * 100, 2) if woerter else 0.0,
        "verstuemmelte_woerter": verstuemmelt,
        "ohne_umlaut": umlaute == 0 and len(woerter) >= 20,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--model", default="qwen3.6:latest")
    parser.add_argument("--timeout", type=int, default=600)
    args = parser.parse_args()

    seeds = list(range(1, args.samples + 1))
    print(f"Modell {args.model} | {args.samples} Samples je Bedingung | Seeds 1..{args.samples}")
    print("Identische Aufgabe und Seeds -- einziger Unterschied: das Ausgabeformat.\n")

    ergebnisse: dict = {"model": args.model, "samples": args.samples, "bedingungen": {}}

    for name, (system, user) in BEDINGUNGEN.items():
        print(f"── {name} ──")
        zeilen, texte = [], {}
        for i, seed in enumerate(seeds, start=1):
            try:
                raw, finish = call(args.model, system, user, seed, args.timeout)
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                print(f"  {i}/{len(seeds)} seed={seed}: FEHLER {exc}", flush=True)
                continue
            row = analysiere(raw)
            row["seed"] = seed
            row["finish_reason"] = finish
            zeilen.append(row)
            texte[seed] = raw
            marker = "  <-- VERSTUEMMELT" if row["verstuemmelte_woerter"] else ""
            print(
                f"  {i}/{len(seeds)} seed={seed}: {row['woerter']} Wörter, "
                f"{row['umlaute']} Umlaute ({row['umlaute_pro_100']}/100){marker}",
                flush=True,
            )
        dichten = [r["umlaute_pro_100"] for r in zeilen]
        zusammenfassung = {
            "samples": len(zeilen),
            "umlaute_pro_100_mittel": round(statistics.mean(dichten), 2) if dichten else 0.0,
            "umlaute_pro_100_min": min(dichten) if dichten else 0.0,
            "samples_mit_verstuemmelung": sum(1 for r in zeilen if r["verstuemmelte_woerter"]),
            "samples_ohne_umlaut": sum(1 for r in zeilen if r["ohne_umlaut"]),
        }
        ergebnisse["bedingungen"][name] = {
            "summary": zusammenfassung,
            "rows": zeilen,
            "texte": texte,
        }
        print(f"  -> {zusammenfassung}\n", flush=True)

    out_dir = Path(__file__).resolve().parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"umlaute_nach_ausgabeformat_{int(time.time())}.json"
    out_path.write_text(json.dumps(ergebnisse, ensure_ascii=False, indent=2), encoding="utf-8")

    print("═══ ERGEBNIS ═══")
    for name, data in ergebnisse["bedingungen"].items():
        print(f"  {name:9s} {data['summary']}")
    print(f"\nRohdaten: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

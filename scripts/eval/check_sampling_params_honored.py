#!/usr/bin/env python3
"""Kontrolltest: Beachtet Ollama die gesendeten Sampling-Parameter überhaupt?

Vorbefund: Bei identischem Seed liefert ``/v1/chat/completions`` mit
``presence_penalty=1.5`` und ``presence_penalty=0.0`` byte-identischen Text.
Entweder ist der Parameter wirkungslos in dieser Aufgabe -- oder Ollamas
OpenAI-Schicht verwirft ihn.

Der Test nutzt einen Prompt, der Wiederholung provoziert (dort wirkt eine
Presence-Penalty am stärksten) und vergleicht drei Zugänge bei identischem Seed:

1. ``/v1/chat/completions`` mit ``presence_penalty`` als Top-Level-Feld
   (genau so sendet es das Backend über ``request_overrides``).
2. ``/v1/chat/completions`` mit ``temperature`` als Referenz -- ein Parameter,
   den Ollama sicher beachtet. Zeigt der keine Wirkung, ist der Seed dominant
   und der Test taugt nicht.
3. ``/api/chat`` mit ``options.presence_penalty`` -- Ollamas nativer Weg.

Unterscheiden sich 1. und 3. im Verhalten, verwirft die OpenAI-Schicht den
Parameter, und ``config.draft_presence_penalty`` ist in Produktion wirkungslos.
"""

from __future__ import annotations

import json
import sys
import urllib.request

BASE = "http://localhost:11434"
MODEL = "qwen3.6:latest"
SEED = 42

# Wiederholungsfreudiger Prompt: hier wirkt eine Presence-Penalty maximal.
PROMPT = (
    "Schreibe genau acht kurze Sätze, die alle mit «Der Hund» beginnen. "
    "Nur die Sätze, keine Einleitung."
)


def post(path: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer ollama"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def via_openai(pp: float | None = None, temperature: float = 0.7) -> str:
    payload: dict = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "temperature": temperature,
        "top_p": 0.8,
        "seed": SEED,
        "max_tokens": 4000,
        "stream": False,
    }
    if pp is not None:
        payload["presence_penalty"] = pp
    data = post("/v1/chat/completions", payload)
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


def via_native(pp: float, temperature: float = 0.7) -> str:
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": PROMPT}],
        "stream": False,
        "think": False,
        "options": {
            "temperature": temperature,
            "top_p": 0.8,
            "seed": SEED,
            "presence_penalty": pp,
            "num_predict": 4000,
        },
    }
    data = post("/api/chat", payload)
    return (data.get("message") or {}).get("content") or ""


def main() -> int:
    print(f"Modell {MODEL}, Seed {SEED} -- alle Läufe mit identischem Seed.\n")

    print("1) /v1 mit presence_penalty 0.0 vs 2.0")
    a = via_openai(pp=0.0)
    b = via_openai(pp=2.0)
    print(f"   identisch: {a == b}")
    if a != b:
        print(f"   pp=0.0: {a[:120]!r}")
        print(f"   pp=2.0: {b[:120]!r}")

    print("\n2) Referenz: /v1 mit temperature 0.1 vs 1.5 (Parameter, den Ollama sicher kennt)")
    c = via_openai(temperature=0.1)
    d = via_openai(temperature=1.5)
    print(f"   identisch: {c == d}")

    print("\n3) /api/chat (nativ) mit options.presence_penalty 0.0 vs 2.0")
    try:
        e = via_native(0.0)
        f = via_native(2.0)
        print(f"   identisch: {e == f}")
        if e != f:
            print(f"   pp=0.0: {e[:120]!r}")
            print(f"   pp=2.0: {f[:120]!r}")
    except Exception as exc:  # noqa: BLE001
        print(f"   FEHLER: {exc}")

    print("\n═══ Deutung ═══")
    print("Wenn 1) identisch und 3) verschieden -> die OpenAI-Schicht verwirft")
    print("presence_penalty; die Einstellung im Backend ist wirkungslos.")
    print("Wenn 2) identisch -> der Seed dominiert, der Test taugt nicht.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

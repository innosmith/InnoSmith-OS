#!/usr/bin/env python3
"""Vergleicht lokale Ollama-Modelle auf der TaskPilot-Triage-Aufgabe.

Reproduzierbarer Benchmark, damit bei neuen lokalen Modellen (monatlich) faktisch
entschieden werden kann, welches die E-Mail-Triage am besten beherrscht. Misst
pro Modell:

1. **JSON-Contract-Compliance** -- liefert das Modell den maschinenlesbaren
   Pflicht-Block? (Kernursache des frueheren ~11%-Stilldrops.)
2. **Kategorisierungs-Genauigkeit** -- stimmt ``triage_class`` mit dem vom
   Berater bestaetigten Ground Truth ueberein?
3. **Schweizer Rechtschreibung** -- keine ``ß``, keine ``ae/oe/ue``-Ersatzformen.
4. **Draft-Aehnlichkeit** -- wie nah ist der generierte Entwurf an der real
   versendeten Antwort (nur fuer Datensaetze mit ``gold_reply_html``)?
5. **Platzhalter-Quote** -- Anteil der Entwuerfe ohne Inhalt zwischen Anrede und
   Schlussformel. Genau dieser Fall ging am 03.08.2026 als fertiger Vorschlag in
   die Freigabe, ohne dass irgendeine Messung ihn sichtbar gemacht haette.

Der Schreib-Auftrag stammt aus ``app.services.draft_prompt`` -- derselben Quelle,
die das Backend im Produktiv-Schreib-Pass verwendet. Fruehere Fassungen pflegten
hier einen eigenen, kuerzeren Prompt und massen damit ein System, das so nie lief.

Nur Standardbibliothek (urllib/difflib/json) -- keine zusaetzlichen Dependencies.

Beispiel:
    python scripts/eval/run_llm_eval.py \
        --models "qwen3.6:latest,gemma4:31b,deepseek-r1:70b,gpt-oss:20b" \
        --golden scripts/eval/golden_set.jsonl --limit 80
"""

from __future__ import annotations

import argparse
import ast
import csv
import json
import re
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

# Produktions-Bausteine importieren statt nachbauen: der Schreib-Auftrag und die
# Platzhalter-Erkennung muessen mit dem Backend identisch sein, sonst misst das
# Eval wieder ein Parallelsystem.
_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.services.draft_prompt import render_draft_task  # noqa: E402
from app.services.text_style import (  # noqa: E402
    has_content_between_greeting_and_closing,
)

VALID_CLASSES = {"auto_reply", "task", "fyi"}

# ── Robuster JSON-Parser (gespiegelt aus hermes_worker, bewusst self-contained) ──


def _loads_lenient(raw: str) -> dict | None:
    if not raw:
        return None
    s = raw.strip().strip("`").strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        obj = json.loads(re.sub(r",(\s*[}\]])", r"\1", s))
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, TypeError):
        pass
    try:
        py = re.sub(r"\btrue\b", "True", s)
        py = re.sub(r"\bfalse\b", "False", py)
        py = re.sub(r"\bnull\b", "None", py)
        obj = ast.literal_eval(py)
        if isinstance(obj, dict):
            return obj
    except (ValueError, SyntaxError, TypeError):
        pass
    return None


def _iter_balanced_objects(text: str):
    depth = 0
    start = -1
    in_str = False
    escape = False
    quote = ""
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == quote:
                in_str = False
            continue
        if ch in ('"', "'"):
            in_str = True
            quote = ch
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:i + 1]
                    start = -1


def extract_json_block(content: str) -> dict | None:
    if not content:
        return None
    candidates = [obj for raw in _iter_balanced_objects(content)
                  if (obj := _loads_lenient(raw)) is not None]
    if not candidates:
        return None
    for obj in reversed(candidates):
        if "triage_class" in obj:
            return obj
    return candidates[-1]


# ── Schweizer-Rechtschreibung-Heuristik ──────────────────────────────────────

_ASCII_UMLAUT = [
    r"\bfuer\b", r"\bueber\b", r"\bmuessen\b", r"\bkoennen\b", r"\bmoechte\b",
    r"\bgruesse\b", r"\baendern\b", r"\boeffnen\b", r"\bwuerde\b", r"\bzurueck\b",
]


def swiss_violations(text: str) -> int:
    """Zaehlt Verstoesse gegen Schweizer Schreibregeln (ß + ae/oe/ue-Ersatz)."""
    if not text:
        return 0
    count = text.count("ß")
    for pat in _ASCII_UMLAUT:
        count += len(re.findall(pat, text, re.IGNORECASE))
    return count


def strip_thinking(text: str) -> str:
    """Entfernt <think>...</think>-Bloecke (Reasoning-Modelle)."""
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL).strip()


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    txt = re.sub(r"(?i)<br\s*/?>", "\n", html)
    txt = re.sub(r"(?i)</p>", "\n", txt)
    txt = re.sub(r"<[^>]+>", "", txt)
    txt = txt.replace("&nbsp;", " ").replace("&amp;", "&").replace("&#39;", "'")
    return re.sub(r"[ \t]+", " ", txt).strip()


# ── Prompts ──────────────────────────────────────────────────────────────────

TRIAGE_INSTRUCTION = (
    "Du bist der E-Mail-Triage-Agent von Anthony Smith (InnoSmith GmbH, Schweiz).\n"
    "Klassifiziere die E-Mail in genau eine Klasse:\n"
    "- auto_reply: einfache, formelhafte Antwort moeglich (Bestaetigung/Dank), kein Risiko.\n"
    "- task: erfordert Arbeit, Entscheidung, Recherche oder eine inhaltliche Antwort.\n"
    "- fyi: rein informativ, kein Handlungsbedarf (System, Newsletter, CC ohne Bezug).\n"
    "Im Zweifel: task.\n\n"
    "Gib AUSSCHLIESSLICH einen JSON-Block aus (```json ... ```), Pflichtfelder: "
    "triage_class, label, reply_expected (true/false), confidence (0..1), rationale."
)

# Das Eval hat weder MCP-Tools noch Skills. Der Systemteil ersetzt nur das, was in
# Produktion aus dem email-style-Skill kommt; der eigentliche AUFTRAG stammt aus
# ``render_draft_task`` -- identisch mit dem Produktiv-Schreib-Pass.
DRAFT_SYSTEM = (
    "Du schreibst im Namen von Anthony Smith (InnoSmith GmbH, Schweiz) "
    "Antwort-Entwuerfe auf eingehende E-Mails.\n"
    "Sprache: Schweizer Hochdeutsch. Verbindlich 'ss' statt 'ß' und 'ä/ö/ü' statt "
    "'ae/oe/ue'. Ton: freundlich, klar, direkt, nicht forsch. Spiegle das Register "
    "des Absenders (Du/Sie und Grussform: 'Hallo' -> 'Hallo', nicht automatisch "
    "'Lieber/Liebe'); Anrede passend zum Geschlecht. Schreibe natuerlich und "
    "fluessig, kurz und konkret, keine KI-Floskeln.\n"
    "Du hast in diesem Lauf KEINE Tools. Gib statt eines Tool-Aufrufs den fertigen "
    "Antworttext direkt aus (keine Erklaerung, kein JSON)."
)


def build_draft_user_prompt(ex: dict) -> str:
    """Baut den Schreib-Auftrag aus dem Produktions-Template.

    Die dynamischen Kontextbloecke der Produktion (Stil-Anker, Absenderprofil,
    Abwesenheiten) fehlen hier notwendigerweise -- sie stammen aus der Datenbank.
    Der Auftragstext selbst ist aber Wort fuer Wort derselbe.
    """
    return render_draft_task(
        today=datetime.now().strftime("%A, %d.%m.%Y"),
        email_id=ex.get("email_message_id") or "EVAL",
        subject=ex.get("subject") or "",
        from_name=ex.get("from_name") or "",
        from_addr=ex.get("from_address") or "",
        body_block=(ex.get("body_preview") or "")[:2000],
    )


def build_email_block(ex: dict) -> str:
    return (
        f"Von: {ex.get('from_name','')} <{ex.get('from_address','')}>\n"
        f"Betreff: {ex.get('subject','')}\n"
        f"Empfaenger-Typ: {ex.get('recipient_type','')}\n"
        f"Microsoft-Inference: {ex.get('inference_classification','')}\n"
        f"Body-Vorschau: {(ex.get('body_preview') or '')[:500]}"
    )


# ── Ollama-Aufruf ────────────────────────────────────────────────────────────


# JSON-Schema fuer schema-constrained Decoding (Ollama native ``format``). Spiegelt
# das Schema aus hermes_worker._structured_triage_reask (rationale ZUERST).
TRIAGE_FORMAT_SCHEMA = {
    "type": "object",
    "properties": {
        "rationale": {"type": "string"},
        "label": {"type": "string"},
        "triage_class": {"type": "string", "enum": ["auto_reply", "task", "fyi"]},
        "reply_expected": {"type": "boolean"},
        "confidence": {"type": "number"},
    },
    "required": ["rationale", "triage_class", "reply_expected"],
}


def ollama_chat(base_url: str, model: str, system: str, user: str, timeout: int,
                fmt: dict | None = None, think: bool | None = None,
                options: dict | None = None) -> tuple[str, float]:
    opts: dict = {"temperature": 0.2}
    if options:
        opts.update(options)
    payload: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": opts,
    }
    # Schema-constrained Decoding (parse-garantiert) und/oder Thinking-Schalter --
    # exakt die zwei Hebel, die wir in Prod evaluieren.
    if fmt is not None:
        payload["format"] = fmt
    if think is not None:
        payload["think"] = think
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/chat", data=data,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    elapsed = time.monotonic() - t0
    return (body.get("message", {}) or {}).get("content", "") or "", elapsed


# ── Scoring ──────────────────────────────────────────────────────────────────


def eval_model(base_url: str, model: str, examples: list[dict], do_drafts: bool,
               timeout: int, structured: bool = False, no_think: bool = False,
               draft_sampling: bool = False) -> dict:
    from collections import defaultdict

    triage_fmt = TRIAGE_FORMAT_SCHEMA if structured else None
    think_flag = False if no_think else None
    # Prosa-Sampling fuer den Draft-Pass -- spiegelt die Defaults aus
    # ``config.draft_*``. presence_penalty steht auf 0.0: 1.5 hatte dem Modell
    # Umlaute weggestrichen ("fr" statt "für"), siehe Commit 8dfafc4.
    draft_opts = (
        {"temperature": 0.7, "top_p": 0.8, "top_k": 20, "presence_penalty": 0.0}
        if draft_sampling else None
    )

    n = 0
    contract_ok = 0
    class_correct = 0
    swiss_viol = 0
    latencies: list[float] = []
    draft_sims: list[float] = []
    draft_swiss_viol = 0
    draft_placeholders = 0
    draft_n = 0
    errors = 0
    gold_total: dict[str, int] = defaultdict(int)
    gold_correct: dict[str, int] = defaultdict(int)

    for ex in examples:
        email = build_email_block(ex)
        # 1) Triage
        try:
            raw, dt = ollama_chat(base_url, model, TRIAGE_INSTRUCTION, email, timeout,
                                   fmt=triage_fmt, think=think_flag)
        except Exception as exc:  # noqa: BLE001
            errors += 1
            print(f"  [{model}] Fehler bei Triage: {exc}")
            continue
        n += 1
        latencies.append(dt)
        if n % 5 == 0 or n == len(examples):
            print(f"  [{model}] {n}/{len(examples)} verarbeitet "
                  f"(contract {contract_ok}/{max(n - 1, 1)})", flush=True)
        content = strip_thinking(raw)
        parsed = extract_json_block(content)
        gold = ex.get("gold_triage_class")
        gold_total[gold] += 1
        if parsed and parsed.get("triage_class") in VALID_CLASSES:
            contract_ok += 1
            if parsed.get("triage_class") == gold:
                class_correct += 1
                gold_correct[gold] += 1
        swiss_viol += swiss_violations(content)

        # 2) Draft (nur wenn Referenz-Antwort vorhanden)
        if do_drafts and ex.get("gold_reply_html"):
            try:
                draft_raw, _ = ollama_chat(base_url, model, DRAFT_SYSTEM,
                                           build_draft_user_prompt(ex), timeout,
                                           think=think_flag, options=draft_opts)
            except Exception:  # noqa: BLE001
                continue
            draft = strip_thinking(draft_raw)
            gold = html_to_text(ex["gold_reply_html"])
            import difflib
            sim = difflib.SequenceMatcher(None, draft.lower(), gold.lower()).ratio()
            draft_sims.append(sim)
            draft_swiss_viol += swiss_violations(draft)
            if not has_content_between_greeting_and_closing(draft):
                draft_placeholders += 1
            draft_n += 1

    # Balanced Accuracy: Mittel der Per-Klasse-Trefferquoten -- robust gegen die
    # starke fyi-Schieflage (ein "immer fyi"-Modell faellt hier durch).
    per_class = {
        c: round(gold_correct[c] / gold_total[c], 3)
        for c in sorted(gold_total) if gold_total[c]
    }
    balanced_acc = round(sum(per_class.values()) / len(per_class), 3) if per_class else 0.0
    per_class_str = ", ".join(f"{c}:{v:.2f}" for c, v in per_class.items())

    return {
        "model": model,
        "n": n,
        "errors": errors,
        "contract_rate": round(contract_ok / n, 3) if n else 0.0,
        "class_accuracy": round(class_correct / n, 3) if n else 0.0,
        "balanced_accuracy": balanced_acc,
        "per_class_accuracy": per_class_str,
        "swiss_viol_per_mail": round(swiss_viol / n, 2) if n else 0.0,
        "avg_latency_s": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
        "draft_n": draft_n,
        "avg_draft_similarity": round(sum(draft_sims) / len(draft_sims), 3) if draft_sims else None,
        "draft_swiss_viol_per_mail": round(draft_swiss_viol / draft_n, 2) if draft_n else None,
        # Anteil Entwuerfe ohne Inhalt zwischen Anrede und Schlussformel. Muss 0
        # sein -- jeder Wert darueber bedeutet, dass ein Modell leere Huellen
        # produziert, die im Cockpit wie fertige Vorschlaege aussehen.
        "draft_placeholder_rate": round(draft_placeholders / draft_n, 3) if draft_n else None,
    }


def write_scoreboard(results: list[dict], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    csv_path = out_dir / f"scoreboard_{stamp}.csv"
    md_path = out_dir / f"scoreboard_{stamp}.md"

    fields = ["model", "n", "errors", "contract_rate", "class_accuracy",
              "balanced_accuracy", "per_class_accuracy", "swiss_viol_per_mail",
              "avg_latency_s", "draft_n", "avg_draft_similarity",
              "draft_swiss_viol_per_mail", "draft_placeholder_rate"]
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)

    # Sortiere nach (contract_rate, balanced_accuracy) absteigend -- Balanced
    # Accuracy ist wegen der fyi-Schieflage die ehrlichere Kategorisierungs-Metrik.
    ranked = sorted(results, key=lambda r: (r["contract_rate"], r["balanced_accuracy"]), reverse=True)
    lines = [
        f"# LLM-Eval Scoreboard ({stamp})",
        "",
        "Sortiert nach Contract-Compliance, dann Balanced Accuracy.",
        "`balanced_accuracy` = Mittel der Per-Klasse-Trefferquoten (robust gegen die fyi-Schieflage).",
        "`draft_placeholder` = Anteil Entwuerfe ohne Inhalt zwischen Anrede und Schlussformel; Sollwert 0.",
        "",
        "| Modell | n | Contract | Acc | Balanced-Acc | Per-Klasse (fyi/task/auto_reply) | Swiss/Mail | Draft-Sim | Draft-Swiss/Mail | Platzhalter | Latenz (s) |",
        "|--------|---|----------|-----|--------------|----------------------------------|-----------|-----------|------------------|-------------|------------|",
    ]
    for r in ranked:
        sim = "-" if r["avg_draft_similarity"] is None else f"{r['avg_draft_similarity']:.3f}"
        dsv = "-" if r["draft_swiss_viol_per_mail"] is None else f"{r['draft_swiss_viol_per_mail']:.2f}"
        ph = "-" if r.get("draft_placeholder_rate") is None else f"{r['draft_placeholder_rate']:.3f}"
        lines.append(
            f"| {r['model']} | {r['n']} | {r['contract_rate']:.3f} | {r['class_accuracy']:.3f} "
            f"| {r['balanced_accuracy']:.3f} | {r['per_class_accuracy']} | {r['swiss_viol_per_mail']:.2f} "
            f"| {sim} | {dsv} | {ph} | {r['avg_latency_s']:.1f} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nScoreboard: {md_path}\n           {csv_path}")
    print("\n".join(lines))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", required=True, help="Komma-separierte Ollama-Modellnamen")
    parser.add_argument("--golden", default="scripts/eval/golden_set.jsonl")
    parser.add_argument("--out-dir", default="scripts/eval/results")
    parser.add_argument("--limit", type=int, default=80, help="Max. Datensaetze pro Modell (wenn --per-class 0)")
    parser.add_argument("--per-class", type=int, default=0,
                        help="Stratifiziert: max. N Faelle je Ground-Truth-Klasse (0 = aus, nutzt --limit)")
    parser.add_argument("--ollama-url", default="http://localhost:11434")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--no-drafts", action="store_true", help="Draft-Generierung ueberspringen")
    parser.add_argument("--structured", action="store_true",
                        help="Triage mit schema-constrained Decoding (Ollama format) -- zeigt den contract_rate-Gewinn")
    parser.add_argument("--no-think", action="store_true",
                        help="Thinking deaktivieren (think=false) -- zeigt class_accuracy-/Latenz-Delta")
    parser.add_argument("--draft-sampling", action="store_true",
                        help="Draft-Pass mit Prosa-Sampling (temp0.7/top_p0.8/top_k20/presence1.5) statt temp0.2 -- Prod-Zwei-Pass-Schreiber")
    args = parser.parse_args()

    golden = Path(args.golden)
    if not golden.exists():
        raise SystemExit(f"Golden-Set fehlt: {golden}. Zuerst export_golden_set.py ausfuehren.")
    all_examples = [json.loads(line) for line in golden.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not all_examples:
        raise SystemExit("Golden-Set ist leer.")

    if args.per_class > 0:
        from collections import defaultdict
        buckets: dict[str, list[dict]] = defaultdict(list)
        for ex in all_examples:
            buckets[ex.get("gold_triage_class")].append(ex)
        examples = []
        for cls in sorted(buckets):
            examples.extend(buckets[cls][: args.per_class])
        # Draft-Referenzen sind rar -> sicherstellen, dass alle mit gold_reply dabei sind.
        have = {id(e) for e in examples}
        examples.extend(e for e in all_examples if e.get("gold_reply_html") and id(e) not in have)
    else:
        examples = all_examples[: args.limit]

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    mode = []
    if args.structured:
        mode.append("schema-constrained")
    if args.no_think:
        mode.append("no-think")
    if args.draft_sampling:
        mode.append("draft-sampling")
    mode_str = f" [Modus: {', '.join(mode)}]" if mode else ""
    print(f"Eval gegen {len(examples)} Datensaetze, {len(models)} Modelle.{mode_str}\n")
    results = []
    for model in models:
        print(f"== {model} ==")
        results.append(eval_model(args.ollama_url, model, examples, not args.no_drafts,
                                  args.timeout, structured=args.structured, no_think=args.no_think,
                                  draft_sampling=args.draft_sampling))

    write_scoreboard(results, Path(args.out_dir))


if __name__ == "__main__":
    main()

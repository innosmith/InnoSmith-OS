# LLM-Eval-Suite (lokale Triage-Modelle)

Reproduzierbarer Benchmark, um bei neuen lokalen Modellen daten- statt
bauchbasiert zu entscheiden, welches die E-Mail-Triage am besten beherrscht.

## Warum

Die E-Mail-Triage laeuft auf einem lokalen Ollama-Modell (`triage_model`, Default
`qwen3.6:latest`). Der Tag `:latest` ist gleitend, und ca. monatlich erscheinen
neue Modelle. Diese Suite misst die fuer den Anwendungsfall entscheidenden
Dimensionen gegen ein Golden-Set aus echten, vom Berater bestaetigten Faellen.

## Gemessene Metriken

| Metrik | Bedeutung |
|--------|-----------|
| `contract_rate` | Anteil mit verwertbarem Pflicht-JSON-Block (Kernursache des frueheren ~11%-Stilldrops) |
| `class_accuracy` | Trefferquote `triage_class` vs. Ground Truth (`email_triage.status='acted'`) |
| `swiss_viol_per_mail` | Verstoesse gegen Schweizer Rechtschreibung (ß, ae/oe/ue) im Output |
| `avg_draft_similarity` | Aehnlichkeit des generierten Entwurfs zur real versendeten Antwort |
| `draft_swiss_viol_per_mail` | Schweizer-Verstoesse im Draft |
| `avg_latency_s` | Mittlere Antwortzeit pro Mail |

## Ablauf

```bash
# 1) Golden-Set aus der Prod-DB ziehen (bleibt lokal, ist gitignored)
python scripts/eval/export_golden_set.py \
    --container taskpilot-postgres-prod --limit 300

# 2) Kandidaten-Modelle vergleichen (muessen in `ollama list` vorhanden sein)
python scripts/eval/run_llm_eval.py \
    --models "qwen3.6:latest,gemma4:31b,deepseek-r1:70b,gpt-oss:20b" \
    --limit 80

# Ergebnis: scripts/eval/results/scoreboard_<ts>.md + .csv
```

## A/B-Schalter: Structured Output und Thinking

Zwei Flags quantifizieren die beiden Prod-Hebel daten- statt bauchbasiert:

```bash
# Schema-constrained Decoding (Ollama `format`) -- erwarteter Effekt: contract_rate -> ~1.0
python scripts/eval/run_llm_eval.py --models "qwen3.6:latest" --limit 80 --structured

# Thinking aus (think=false) -- erwarteter Effekt: tiefere avg_latency_s,
# class_accuracy im Idealfall gleich/besser
python scripts/eval/run_llm_eval.py --models "qwen3.6:latest" --limit 80 --no-think

# Prosa-Sampling fuer den Draft-Pass (temp0.7/top_p0.8/top_k20/presence1.5 statt
# temp0.2) -- spiegelt den Prod-Zwei-Pass-Schreiber; Effekt auf avg_draft_similarity
# und draft_swiss_viol_per_mail beobachten (natuerlicheres Deutsch vs. Referenz)
python scripts/eval/run_llm_eval.py --models "qwen3.6:latest" --limit 80 --draft-sampling
```

Vorgehen: einmal ohne Flags (Baseline), dann je einzeln laufen lassen und die
Deltas in `contract_rate` / `class_accuracy` / `avg_latency_s` /
`avg_draft_similarity` vergleichen.

**Zum Draft-Sampling:** `avg_draft_similarity` misst die Aehnlichkeit zur *einen*
real versendeten Antwort. Natuerlicheres, frei formuliertes Deutsch kann diese
String-Aehnlichkeit sogar leicht senken, obwohl die Qualitaet steigt -- die
Metrik ist ein grobes Regressionsnetz, kein Qualitaetsurteil. Entscheidend fuer
den Prod-Zwei-Pass ist die menschliche HITL-Freigabe.

**Caveat (wichtig):** Diese Suite ruft Ollama direkt auf und misst nur den
rohen Klassifikations-/Draft-Pfad. Sie bildet NICHT die agentische Tool-
Zuverlaessigkeit ab (Mails verschieben/kategorisieren via MCP). `--no-think`
kann die Klassifikation verbessern und trotzdem die Tool-Wahl kleiner Modelle im
echten Agenten-Loop verschlechtern -- deshalb bleibt `triage_disable_thinking`
im Backend Default aus und wird erst nach einem bewussten Prod-Test aktiviert.
`--structured` entspricht dem Backend-Fallback `triage_structured_fallback`.

Empfohlene Kandidaten (Stand der lokalen Maschine, Thinking-faehig priorisieren):
`qwen3.6:latest` (Baseline), `deepseek-r1:70b` (Reasoning), `gemma4:31b`
(DE-Qualitaet), `gpt-oss:20b`, ggf. `apertus:70b` (Schweizer Modell).

## Gewinner uebernehmen

Das beste Modell als FIXEN Tag in `.env` setzen (reproduzierbar, kein `:latest`):

```
TP_TRIAGE_MODEL=ollama/<modell>:<fixer-tag>
```

## Hermes-Library-Sonde (0.21)

Beim Agent-Upgrade zuerst die isolierte API-Sonde, noch bevor ein Image
gebaut wird. Scheitert sie, gilt der 0.19-Fallback, nicht ein ``uv``-Bypass.

```
/pfad/zum/probe-venv/bin/python scripts/eval/check_hermes_021_api.py
```

## Datenschutz

`scripts/eval/golden_set.jsonl` und `scripts/eval/results/` enthalten echte
E-Mail-Inhalte und sind via `.gitignore` ausgeschlossen. Nicht committen, nicht an
Cloud-Dienste schicken. Der Benchmark laeuft ausschliesslich lokal gegen Ollama.

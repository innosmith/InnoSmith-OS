# GX10-Freezes: Befund vom 24. August 2026

Festgehalten, damit die widerlegten Hypothesen nicht erneut zu Änderungen an
TaskPilot führen. Die Umsetzung der abgeleiteten Massnahmen liegt in
[ops/gx10/](../ops/gx10/README.md).

> **Stand nach dem dritten Freeze:** Die Ursache ist gefunden und belegt — siehe
> [Die Ursache](#die-ursache-gemessen-am-2408-um-1427). Die widerlegten
> Hypothesen bleiben dokumentiert, damit sie nicht erneut aufgegriffen werden.

## Die drei Ereignisse

Am 24.08.2026 fror die GX10 dreimal hart ein, jedes Mal mit geladenem
`qwen3.8:27b-q8_0`:

- **00:46:31** — mitten in einer laufenden Generierung. Beendet durch
  Power-Cycle um 00:51, rund fünf Stunden Ausfall.
- **11:56:33** — im Leerlauf, unmittelbar nach einer sauber abgeschlossenen
  Anfrage. Beendet durch Power-Cycle um 12:29, 33 Minuten Ausfall.
- **14:27:31** — mitten in einer E-Mail-Triage, nach elf Minuten
  ununterbrochener Volllast. Erstes Ereignis **mit** laufender Telemetrie.

## Was widerlegt ist

### Es war kein Speicherproblem

Die `sar`-Werte im letzten Messpunkt vor dem jeweiligen Freeze:

| Zeitpunkt | verfügbar | belegt | Swap |
|---|---|---|---|
| 00:40 (6 min vor Nacht-Freeze) | 71.6 GiB | 37.78 % | 6.5 MB |
| 11:50 (6 min vor Vormittags-Freeze) | 66.3 GiB | 41.88 % | 0 |

Dazu im gesamten Boot: **null** OOM-Kills, **null** `Killed process`, keine
einzige `NVRM:`- oder XID-Fehlermeldung im Kernel-Log.

### Es war kein Lastproblem

Innerhalb desselben Boots, der drei Tage durchhielt (20.08. 21:17 bis 24.08.
00:46):

| Tag | Chat-Completions | Checkpoint-Restores | Freeze |
|---|---|---|---|
| 21.08. | 196 | 278 | nein |
| 22.08. | 18 | 13 | nein |
| 23.08. | 14 | 10 | nein |
| 24.08. | 4 | 9 | **ja** |

Der mit Abstand lastreichste Tag lief durch. Am Crash-Tag genügten vier Läufe.
Auch die Lauflänge trägt nicht: Läufe bis 9m07s blieben folgenlos.

### Ein Token- oder Kontextlimit hätte nichts geändert

Die letzten Zeilen vor dem Vormittags-Freeze:

```
11:56:33 slot release: id 0 | task 12240 | stop processing: n_tokens = 16965, truncated = 0
11:56:33 srv update_slots: all slots are idle
11:56:33 [GIN] | 200 | 11.845195587s | POST "/v1/chat/completions"
```

Zum Zeitpunkt des Freezes wurde nichts generiert. Ein `max_tokens`-Deckel, ein
kleineres `num_ctx` oder abgeschaltetes Thinking hätten hier nicht gegriffen.

Beim Nacht-Freeze lief die Generierung mit metronomisch stabilen 7.29 t/s bis
zur letzten Zeile — kein Absacken, kein Stottern, keine Fehlermeldung.

### Ein cgroup-Speicherlimit greift nicht

Naheliegend wäre `MemoryMax=` auf `ollama.service`, um einen Ausreisser den
Dienst statt die Maschine treffen zu lassen. Die Messung widerlegt das: Bei
einem geladenen 24-GB-Modell meldet die cgroup

```
MemoryCurrent=3982524416      # 3.71 GiB
anon: 0.77 GiB, file: 2.87 GiB
```

Der Unified-Memory des Modells taucht im cgroup-Accounting nicht auf, weil ihn
der NVIDIA-Treiber ausserhalb alloziert. Ein Limit würde entweder nie greifen
oder den Dienst aus einem unbeteiligten Grund treffen.

Der in der GB10-Community verbreitete Rat, die Inferenz in einen Container mit
`--memory` zu sperren, zielt auf SGLang und vLLM. Dort alloziert der Python-
Prozess grosse Mengen Host-RAM (CUDA-Graph-Capture, Autotuner), die sehr wohl
gezählt werden. Auf Ollama ist er nicht übertragbar.

## Die Ursache, gemessen am 24.08. um 14:27

Der dritte Freeze fiel in den ersten Boot mit laufender Telemetrie. Die
Aufzeichnung reicht bis fünf Sekunden vor den Stillstand:

| Zeit | Temperatur | Leistung | Auslastung | Drosselung |
|---|---|---|---|---|
| 14:19:53 | 76 °C | 43.67 W | 96 % | keine |
| 14:23:42 | 86 °C | 76.99 W | 96 % | **0x20** |
| 14:25:03 | 85 °C | 81.18 W | 96 % | **0x20** |
| 14:27:05 | 88 °C | 76.70 W | 96 % | **0x20** |
| 14:27:26 | 87 °C | 70.21 W | 96 % | **0x20** |

`0x20` ist `nvmlClocksThrottleReasonSwThermalSlowdown`: Die GPU drosselte sich
wegen Übertemperatur, ab 14:23 zunehmend, zuletzt fast durchgehend. Der
Speicher war zu keinem Zeitpunkt knapp — im letzten Messpunkt standen 62 GB
zur Verfügung.

### Warum die Maschine so heiss wurde

`qwen3.8:27b-q8_0` ist ein **dicht** quantisiertes Modell (`qwen35`,
27.3 Mrd. Parameter, Q8_0, 29 GB). Für jeden erzeugten Token müssen die
vollständigen Gewichte durch die Speicherschnittstelle. Der GB10 liefert rund
273 GB/s, woraus sich etwa 9 Token/s ergeben — gemessen wurden **7.65 t/s**.
Die Schnittstelle lief also während der gesamten Generierung am Anschlag, und
das ist der eigentliche Wärmeerzeuger.

Verschärfend kommt hinzu, dass dieser Tag **keinen MTP-Head** mitbringt:

```
cmd="/usr/local/lib/ollama/llama-server --model sha256-2bb22714... -c 65536 ..."
srv prompt_save: total state size = 2462.895 MiB (draft: 0.000 MiB)
```

Kein `--spec-type draft-mtp`, kein Draft-Speicher. Ollamas offizieller
`27b`-Tag dagegen ist Q4_K_M (18 GB) und aktiviert spekulatives Dekodieren mit
`draft_num_predict 4` von selbst — auf derselben Hardware gemessene 26.5 t/s.

| | `27b-q8_0` | `27b` |
|---|---|---|
| Quantisierung | Q8_0, 29 GB | Q4_K_M, 18 GB |
| Spekulatives Dekodieren | aus | an, bis 4 Token je Durchlauf |
| Durchsatz | 7.65 t/s (gemessen) | 26.5 t/s (GB10-Referenz) |
| Speicherverkehr je Token | 29 GB | rund 4.5 GB |

Der Effekt wirkt doppelt: höhere Leistungsaufnahme je Sekunde **und** die
dreieinhalbfache Laufzeit für dieselbe Arbeit. Je erledigter Aufgabe wandert
damit ein Vielfaches an Wärme in die Maschine.

### Die Reboot-Historie stützt das

Ollama wurde am **16.08.2026 um 19:06** von 0.24.0 auf 0.32.14 aktualisiert;
qwen3.8 wurde erst dadurch verfügbar. Davor lief dieselbe Maschine mit
demselben Kernel `6.17.0-1029-nvidia`:

| Boot | Laufzeit |
|---|---|
| 05.05. | 42 Tage |
| 30.06. | 27 Tage |
| 27.07. | 6 Tage |
| 06.08. | **10 Tage 23 h** |
| ab 17.08. | drei bis vier Freezes je Nutzungstag |

Kernel und Gerät scheiden damit als Ursache aus.

### Was auf dem GB10 nicht hilft

Power-Limit, Taktbegrenzung und Lüftersteuerung melden auf diesem Gerät
durchweg `[N/A]`:

```
Current Power Limit : N/A      --query-supported-clocks : [N/A]
Max Power Limit     : N/A      fan.speed                : [N/A]
```

`nvidia-smi -pl` und `-lgc` sind hier also keine Option, auch wenn sie in
GB10-Foren empfohlen werden. Ebenso liefert `--query-gpu=memory.used` `[N/A]`,
weil Unified Memory nicht getrennt ausgewiesen wird — Speicherwerte müssen aus
`/proc/meminfo` kommen.

## Abgeleitete Massnahmen

Keine davon beschränkt die Leistungsfähigkeit der Maschine:

1. **Hardware-Watchdog** (SBSA, `/dev/watchdog0`, 120 s) — war deaktiviert.
   Macht aus stundenlangem Stillstand einen Neustart nach rund drei Minuten.
2. **Telemetrie im 5-Sekunden-Takt** mit `sync` nach jeder Zeile, weil bei
   einem Hard-Lock der Page-Cache verloren geht und ungepufferte Zeilen genau
   die interessanten wären.
3. **`sar` im Minutentakt** statt alle zehn Minuten.
4. **Auswertungsskript**, das nach einem Neustart die Schlussminuten
   zusammenzieht.

5. **Temperaturwarnung ab 84 °C** ins Journal (`logger -t gx10-telemetrie`),
   gedämpft auf eine Meldung je Minute. Die Schwelle liegt bewusst unter den
   85 °C, ab denen die Drosselung einsetzte.

## Die Behebung an der Wurzel

Umgestellt auf `ollama/qwen3.8:27b` (Q4_K_M mit MTP-Head) an drei Stellen:

| Ort | Feld |
|---|---|
| `src/backend/app/config.py` | `triage_model` |
| `src/backend/app/services/llm_defaults.py` | `FALLBACK_LOCAL_MODEL` |
| Owner-Settings (DB) | `llm_default_model`, `llm_default_local_model` |

Steht `TP_TRIAGE_MODEL` in der `.env`, hat diese Variable Vorrang und muss
ebenfalls angepasst werden.

Das ist keine Beschränkung: dasselbe Modell mit denselben Fähigkeiten
(Vision, Werkzeuge, Thinking), bei einem Sechstel des Speicherverkehrs je Token
und dreieinhalbfacher Geschwindigkeit. Der Qualitätsabstand zwischen Q4_K_M und
Q8_0 liegt bei einem dichten 27-B-Modell im niedrigen einstelligen
Prozentbereich.

**Nicht auf `-q8_0` zurückwechseln.** Der Tag ist auf dieser Hardware kein
Qualitätsgewinn, sondern eine thermische Dauerlast.

An Kontextgrösse, Ausgabelänge und Thinking wurde weiterhin nichts geändert —
diese Hypothesen sind oben widerlegt.

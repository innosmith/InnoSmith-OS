#!/usr/bin/env bash
#
# Telemetrie-Sampler für die GX10.
#
# Schreibt periodisch eine CSV-Zeile mit GPU- und Speicherzustand und erzwingt
# danach das Zurückschreiben auf die Platte. Der sync ist der Kern der Sache:
# Bei einem Hard-Lock geht der Page-Cache verloren. Ungesyncte Zeilen wären
# genau jene Sekunden, die den Freeze erklären könnten.
#
# Auf GB10 liefert nvidia-smi für memory.used ein "[N/A]", weil Unified Memory
# nicht getrennt ausgewiesen wird. Die Speicherwerte stammen deshalb aus
# /proc/meminfo.

set -uo pipefail

LOG="${GX10_TELEMETRIE_LOG:-/var/log/gx10-telemetrie.log}"
INTERVALL="${GX10_TELEMETRIE_INTERVALL:-5}"
NVIDIA_TIMEOUT="${GX10_NVIDIA_TIMEOUT:-4}"

# Ab etwa 85 Grad drosselt die GPU (Flag 0x20, SW Thermal Slowdown). Die Warnung
# macht das im Journal sichtbar -- sie ist eine Beobachtung, keine Schutzmassnahme:
# Die Freezes vom August 2026 hatten keine thermische Ursache, die Maschine lief
# stundenlang bei 84 Grad und stürzte umgekehrt schon bei 78 Grad ab.
WARN_TEMP="${GX10_WARN_TEMP:-84}"
WARN_PAUSE="${GX10_WARN_PAUSE:-60}"

readonly NVIDIA_FIELDS='temperature.gpu,power.draw,utilization.gpu,clocks_throttle_reasons.active'
readonly HEADER='zeit,gpu_temp_c,gpu_power_w,gpu_util_pct,gpu_throttle,mem_available_kb,mem_free_kb,load1'

# Platzhalter für die vier GPU-Spalten, falls nvidia-smi nicht antwortet.
readonly GPU_AUSFALL='TIMEOUT,TIMEOUT,TIMEOUT,TIMEOUT'

if [[ ! -s "$LOG" ]]; then
    printf '%s\n' "$HEADER" >> "$LOG"
fi

letzte_warnung=0

while true; do
    zeit=$(date '+%Y-%m-%dT%H:%M:%S%z')

    # Hängt der Treiber, darf der Sampler nicht mit ihm blockieren. Eine Zeile
    # mit TIMEOUT ist in diesem Fall selbst der wertvollste Befund.
    if gpu=$(timeout "$NVIDIA_TIMEOUT" nvidia-smi \
                --query-gpu="$NVIDIA_FIELDS" \
                --format=csv,noheader,nounits 2>/dev/null); then
        gpu=$(printf '%s' "$gpu" | head -n 1 | tr -d '[:space:]')
        [[ -n "$gpu" ]] || gpu="$GPU_AUSFALL"
    else
        gpu="$GPU_AUSFALL"
    fi

    mem_available=$(awk '/^MemAvailable:/ {print $2}' /proc/meminfo)
    mem_free=$(awk '/^MemFree:/ {print $2}' /proc/meminfo)
    load1=$(cut -d ' ' -f 1 /proc/loadavg)

    printf '%s,%s,%s,%s,%s\n' \
        "$zeit" "$gpu" "$mem_available" "$mem_free" "$load1" >> "$LOG"

    # Nur die Daten dieser Datei zurückschreiben, nicht das ganze Dateisystem.
    sync -d "$LOG" 2>/dev/null || sync

    temp=${gpu%%,*}
    if [[ "$temp" =~ ^[0-9]+$ ]] && (( temp >= WARN_TEMP )); then
        jetzt=$(date +%s)
        if (( jetzt - letzte_warnung >= WARN_PAUSE )); then
            logger -t gx10-telemetrie -p daemon.warning \
                "GPU bei ${temp} C (Schwelle ${WARN_TEMP} C) -- ${gpu#*,}"
            letzte_warnung=$jetzt
        fi
    fi

    sleep "$INTERVALL"
done

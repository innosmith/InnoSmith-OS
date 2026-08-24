#!/usr/bin/env bash
#
# Fasst nach einem unerwarteten Neustart die letzten Minuten vor dem Abriss
# zusammen: Watchdog-Status, Telemetrie, sar-Kennzahlen und Ollama-Journal.
#
# Aufruf: gx10-freeze-report.sh [anzahl_telemetriezeilen]
# Vorgabe sind 72 Zeilen, bei 5 Sekunden Takt also die letzten sechs Minuten.

set -uo pipefail

LOG="${GX10_TELEMETRIE_LOG:-/var/log/gx10-telemetrie.log}"
ZEILEN="${1:-72}"

boot_zeit=$(uptime -s)
boot_iso=$(date -d "$boot_zeit" '+%Y-%m-%dT%H:%M:%S')

# Über die Epoche rechnen: "$boot_zeit - 30 minutes" würde GNU date als
# Zeitzonenangabe missdeuten und mit einem Parserfehler abbrechen.
boot_epoch=$(date -d "$boot_zeit" '+%s')

trenner() { printf '\n===== %s =====\n' "$1"; }

# Gibt die Eingabe aus oder einen Hinweis, wenn sie leer ist. In einer Pipe
# bestimmt sonst das letzte Glied den Rückgabewert, ein "|| echo" liefe leer.
oder_hinweis() {
    local inhalt
    inhalt=$(cat)
    if [[ -n "$inhalt" ]]; then
        printf '%s\n' "$inhalt"
    else
        printf '%s\n' "$1"
    fi
}

printf 'GX10 Freeze-Bericht\n'
printf 'Aktueller Boot: %s\n' "$boot_zeit"

trenner 'Watchdog'
if [[ -r /sys/class/watchdog/watchdog0/bootstatus ]]; then
    bootstatus=$(cat /sys/class/watchdog/watchdog0/bootstatus)
    state=$(cat /sys/class/watchdog/watchdog0/state 2>/dev/null || echo '?')
    timeout=$(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null || echo '?')
    printf 'state=%s timeout=%ss bootstatus=%s\n' "$state" "$timeout" "$bootstatus"
    if [[ "$bootstatus" != "0" ]]; then
        printf 'Hinweis: bootstatus ungleich 0 -- der letzte Neustart ging vermutlich vom Watchdog aus.\n'
    fi
else
    printf 'Kein Watchdog-Geraet lesbar.\n'
fi

trenner "Telemetrie: letzte $ZEILEN Zeilen vor dem Abriss"
if [[ -r "$LOG" ]]; then
    head -n 1 "$LOG"
    # Nur Zeilen, die zeitlich vor dem aktuellen Boot liegen -- das ist der
    # Zustand unmittelbar vor dem Freeze.
    awk -F, -v grenze="$boot_iso" \
        'NR > 1 && substr($1, 1, 19) < grenze' "$LOG" \
        | tail -n "$ZEILEN" \
        | oder_hinweis 'Keine Messwerte aus der Zeit vor diesem Boot.'
else
    printf 'Keine Telemetriedatei unter %s.\n' "$LOG"
fi

sa_datei="/var/log/sysstat/sa$(date -d "$boot_zeit" '+%d')"
start=$(date -d "@$((boot_epoch - 1800))" '+%H:%M:%S')
ende=$(date -d "@$boot_epoch" '+%H:%M:%S')

trenner "sar Speicher ($start bis $ende)"
LC_ALL=C sar -r -f "$sa_datei" -s "$start" -e "$ende" 2>/dev/null \
    | oder_hinweis "Keine sar-Daten in $sa_datei."

trenner "sar Last ($start bis $ende)"
LC_ALL=C sar -q -f "$sa_datei" -s "$start" -e "$ende" 2>/dev/null \
    | oder_hinweis "Keine sar-Daten in $sa_datei."

trenner 'Ollama: letzte 40 Zeilen des vorherigen Boots'
journalctl -b -1 -u ollama --no-pager -o short-iso 2>/dev/null | tail -n 40 \
    | oder_hinweis 'Kein vorheriger Boot im Journal.'

trenner 'Kernel: Auffaelligkeiten im vorherigen Boot'
# Bewusst eng gefasst: ein blosses "thermal" faengt die Registrierung des
# Governors beim Systemstart ein und uebertoent die echten Befunde.
journalctl -b -1 -k --no-pager -o short-iso 2>/dev/null \
    | grep -iE 'xid [0-9]|nvrm:|out of memory|oom-kill|hung task|soft lockup|hard lockup|critical temperature|thermal shutdown|throttl' \
    | tail -n 20 \
    | oder_hinweis 'Nichts gefunden.'

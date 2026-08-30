#!/usr/bin/env bash
#
# Fasst nach einem unerwarteten Neustart die letzten Minuten vor dem Abriss
# zusammen: Ollama-Konfiguration, Rettungskette, Telemetrie, sar-Kennzahlen
# und Journal.
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

# Alle Telemetriezeilen in zeitlicher Reihenfolge, rotierte Dateien inbegriffen.
# Ohne das endete der Bericht ausgerechnet bei einem Freeze kurz nach
# Mitternacht leer: logrotate hatte die entscheidenden Minuten nach .1 verschoben.
telemetrie_zeilen() {
    local datei
    # Höhere Nummer heisst älter, also absteigend sortiert zuerst ausgeben.
    while IFS= read -r datei; do
        zcat -- "$datei" 2>/dev/null
    done < <(ls -1 "$LOG".*.gz 2>/dev/null | sort -rV)
    [[ -r "$LOG.1" ]] && cat -- "$LOG.1"
    [[ -r "$LOG" ]] && cat -- "$LOG"
    return 0
}

printf 'GX10 Freeze-Bericht\n'
printf 'Aktueller Boot: %s\n' "$boot_zeit"

trenner 'Ollama: war Flash Attention aktiv?'
# Steht am Anfang, weil dieser eine Wert die Ursache der Freezes vom August 2026
# war. Siehe docs/gx10-freeze-befund.md.
runner=$(journalctl -b -1 -u ollama --no-pager -o cat 2>/dev/null \
    | grep -o -- '--flash-attn [a-z]*' | tail -n 1)
printf 'Runner im vorherigen Boot: %s\n' "${runner:-kein Runner-Start im Journal}"
printf 'Aktuell konfiguriert:      %s\n' \
    "$(systemctl show ollama -p Environment --value 2>/dev/null \
        | tr ' ' '\n' | grep OLLAMA_FLASH_ATTENTION || echo 'nicht gesetzt (Vorgabe: aus)')"

trenner 'Rettungskette'
if [[ -r /sys/class/watchdog/watchdog0/bootstatus ]]; then
    bootstatus=$(cat /sys/class/watchdog/watchdog0/bootstatus)
    state=$(cat /sys/class/watchdog/watchdog0/state 2>/dev/null || echo '?')
    timeout=$(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null || echo '?')
    printf 'Watchdog: state=%s timeout=%ss bootstatus=%s\n' "$state" "$timeout" "$bootstatus"
    if [[ "$bootstatus" != "0" ]]; then
        printf 'Hinweis: bootstatus ungleich 0 -- der letzte Neustart ging vermutlich vom Watchdog aus.\n'
    fi
else
    printf 'Kein Watchdog-Geraet lesbar.\n'
fi
printf 'Panic-Aktion: kernel.panic=%s kernel.panic_on_oops=%s\n' \
    "$(sysctl -n kernel.panic 2>/dev/null || echo '?')" \
    "$(sysctl -n kernel.panic_on_oops 2>/dev/null || echo '?')"
if [[ "$(sysctl -n kernel.panic 2>/dev/null || echo 0)" == "0" ]]; then
    printf 'WARNUNG: kernel.panic=0 -- ein Panic bliebe stehen statt neu zu starten.\n'
fi

trenner "Telemetrie: letzte $ZEILEN Zeilen vor dem Abriss"
# Kopfzeile aus der aktuellen Datei, danach nur echte Messzeilen: über mehrere
# rotierte Dateien hinweg taugt "NR > 1" nicht mehr zum Aussortieren.
head -n 1 "$LOG" 2>/dev/null || printf 'Keine Telemetriedatei unter %s.\n' "$LOG"
telemetrie_zeilen \
    | awk -F, -v grenze="$boot_iso" \
        '$1 ~ /^20[0-9][0-9]-/ && substr($1, 1, 19) < grenze' \
    | tail -n "$ZEILEN" \
    | oder_hinweis 'Keine Messwerte aus der Zeit vor diesem Boot.'

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
    | grep -iE 'xid [0-9]|nvrm:|out of memory|oom-kill|hung task|soft lockup|hard lockup|critical temperature|thermal shutdown|throttl|kernel panic|unhandled context fault|illegal memory access|smmu' \
    | tail -n 30 \
    | oder_hinweis 'Nichts gefunden. Bei einem Hard-Lock ist genau das der Normalfall -- der Kernel kam nicht mehr zum Schreiben.'

#!/usr/bin/env bash
#
# Richtet auf der GX10 die Massnahmen gegen die Freezes ein und die Messung,
# mit der ein erneuter Freeze aufzuklären wäre. Aufruf: sudo ./install.sh
#
# Die Reihenfolge folgt der Wirkung: zuerst die Ursache (Flash Attention),
# dann die Rettungskette (Panic -> Neustart, Watchdog), dann die Beobachtung.
#
# Das Skript ist idempotent -- ein zweiter Lauf richtet keinen Schaden an.
# Zur Rücknahme siehe README.md in diesem Verzeichnis.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    printf 'Dieses Skript braucht root-Rechte: sudo %s\n' "$0" >&2
    exit 1
fi

QUELLE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SICHERUNG="/var/backups/gx10-setup-$(date '+%Y%m%d-%H%M%S')"

schritt() { printf '\n>>> %s\n' "$1"; }

# Legt die Sicherung unter dem vollen Pfad ab. Sonst überschrieben sich
# override.conf und watchdog.conf gegenseitig -- beide heissen nur "*.conf".
sichern() {
    local datei="$1"
    [[ -f "$datei" ]] || return 0
    cp -a "$datei" "$SICHERUNG/$(printf '%s' "${datei#/}" | tr '/' '_')"
    printf '     gesichert: %s\n' "$datei"
}

mkdir -p "$SICHERUNG"

schritt "1/7  Vorhandene Konfiguration sichern nach $SICHERUNG"
sichern /etc/systemd/system/ollama.service.d/override.conf
sichern /etc/sysctl.d/60-gx10-panic.conf
sichern /etc/systemd/system.conf.d/watchdog.conf
sichern /etc/cron.d/sysstat

schritt '2/7  Ollama: Flash Attention abschalten'
# Der eigentliche Eingriff. Begründung in ollama-override.conf und in
# docs/gx10-freeze-befund.md.
install -d -m 0755 /etc/systemd/system/ollama.service.d
install -m 0644 "$QUELLE/ollama-override.conf" \
    /etc/systemd/system/ollama.service.d/override.conf
systemctl daemon-reload
if ! timeout 60 systemctl restart ollama; then
    printf 'WARNUNG: Ollama liess sich nicht neu starten.\n'
    printf '         Pruefen mit: systemctl status ollama\n'
fi

schritt '3/7  Kernel-Panic soll neu starten statt stehenzubleiben'
install -m 0644 "$QUELLE/panic-reboot.conf" /etc/sysctl.d/60-gx10-panic.conf
sysctl -p /etc/sysctl.d/60-gx10-panic.conf

schritt '4/7  Hardware-Watchdog konfigurieren'
install -d -m 0755 /etc/systemd/system.conf.d
install -m 0644 "$QUELLE/watchdog.conf" /etc/systemd/system.conf.d/watchdog.conf
# daemon-reexec startet den systemd-Manager neu und übernimmt dabei die
# Watchdog-Einstellung. Laufende Dienste bleiben unberührt.
systemctl daemon-reexec

schritt '5/7  Telemetrie-Sampler einrichten'
install -m 0755 "$QUELLE/gx10-telemetrie.sh"    /usr/local/bin/gx10-telemetrie.sh
install -m 0755 "$QUELLE/gx10-freeze-report.sh" /usr/local/bin/gx10-freeze-report.sh
install -m 0644 "$QUELLE/gx10-telemetrie.service" /etc/systemd/system/gx10-telemetrie.service
install -m 0644 "$QUELLE/gx10-telemetrie.logrotate" /etc/logrotate.d/gx10-telemetrie
systemctl daemon-reload
systemctl enable gx10-telemetrie.service

# Einen aus einem früheren Lauf verbliebenen, wartenden Start-Job abräumen.
timeout 10 systemctl stop gx10-telemetrie.service >/dev/null 2>&1 || true

# Start mit Zeitschranke. Auf dieser Maschine blockiert plymouth-quit-wait
# den Boot dauerhaft, wodurch multi-user.target nie erreicht wird -- ein
# unbegrenztes "systemctl start" könnte darauf endlos warten.
if ! timeout 30 systemctl restart gx10-telemetrie.service; then
    printf 'WARNUNG: Start hat 30s ueberschritten oder ist fehlgeschlagen.\n'
    printf '         Pruefen mit: systemctl status gx10-telemetrie.service\n'
    printf '         Und mit:     systemctl list-jobs\n'
fi

schritt '6/7  sar auf Minutentakt verdichten'
# Gesammelt wird über sysstat-collect.timer, nicht über cron: In
# /etc/default/sysstat steht ENABLED="false", der cron-Aufruf von debian-sa1
# bleibt damit folgenlos. Die cron-Datei wird nur auf die Debian-Vorgabe
# zurückgesetzt, falls ein früherer Lauf sie verändert hat.
install -m 0644 "$QUELLE/sysstat.cron" /etc/cron.d/sysstat
install -d -m 0755 /etc/systemd/system/sysstat-collect.timer.d
install -m 0644 "$QUELLE/sysstat-collect-minutentakt.conf" \
    /etc/systemd/system/sysstat-collect.timer.d/minutentakt.conf
systemctl daemon-reload
if ! timeout 20 systemctl restart sysstat-collect.timer; then
    printf 'WARNUNG: sysstat-collect.timer liess sich nicht neu starten.\n'
fi

schritt '7/7  Ergebnis pruefen'
printf '\n--- Ollama ---\n'
printf 'Dienst: %s\n' "$(systemctl is-active ollama)"
if systemctl show ollama -p Environment --value | grep -q 'OLLAMA_FLASH_ATTENTION=0'; then
    printf 'OLLAMA_FLASH_ATTENTION=0 ist aktiv.\n'
else
    printf 'WARNUNG: Flash Attention ist NICHT abgeschaltet. Das ist der Kern der Massnahme.\n'
    systemctl show ollama -p Environment --value | tr ' ' '\n' | grep OLLAMA || true
fi

printf '\n--- Kernel-Panic ---\n'
printf 'kernel.panic=%s  kernel.panic_on_oops=%s\n' \
    "$(sysctl -n kernel.panic)" "$(sysctl -n kernel.panic_on_oops)"

printf '\n--- Watchdog ---\n'
printf 'systemd RuntimeWatchdog: %s\n' \
    "$(systemctl show -p RuntimeWatchdogUSec --value)"
printf 'Geraet state=%s timeout=%ss\n' \
    "$(cat /sys/class/watchdog/watchdog0/state 2>/dev/null || echo '?')" \
    "$(cat /sys/class/watchdog/watchdog0/timeout 2>/dev/null || echo '?')"

printf '\n--- Telemetrie ---\n'
systemctl is-active gx10-telemetrie.service
sleep 6
if [[ -s /var/log/gx10-telemetrie.log ]]; then
    tail -n 3 /var/log/gx10-telemetrie.log
else
    printf 'WARNUNG: noch keine Messwerte geschrieben.\n'
fi

printf '\n--- sar ---\n'
printf 'Timer-Takt: %s\n' \
    "$(systemctl show sysstat-collect.timer -p TimersCalendar --value)"
timeout 10 systemctl list-timers --all --no-pager 2>/dev/null \
    | grep sysstat-collect \
    || printf 'WARNUNG: sysstat-collect.timer nicht gelistet.\n'

printf '\nFertig. Sicherung der Vorgaengerdateien: %s\n' "$SICHERUNG"
printf 'Letzter Nachweis, erst nach der naechsten Modellanfrage moeglich:\n'
printf '  journalctl -u ollama -o cat | grep -o -- "--flash-attn [a-z]*" | tail -n 1\n'
printf 'Dort muss "--flash-attn off" stehen. Fehlt der Schalter ganz, ist ein\n'
printf 'alter Runner noch geladen -- dann "ollama stop <modell>" und neu anfragen.\n'

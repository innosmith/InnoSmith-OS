#!/usr/bin/env bash
#
# Installiert Watchdog, Telemetrie-Sampler und verdichtete sar-Messung auf der
# GX10. Aufruf: sudo ./install.sh
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

mkdir -p "$SICHERUNG"

schritt "1/5  Vorhandene Konfiguration sichern nach $SICHERUNG"
for datei in /etc/cron.d/sysstat /etc/systemd/system.conf.d/watchdog.conf; do
    if [[ -f "$datei" ]]; then
        cp -a "$datei" "$SICHERUNG/"
        printf '     gesichert: %s\n' "$datei"
    fi
done

schritt '2/5  Hardware-Watchdog konfigurieren'
install -d -m 0755 /etc/systemd/system.conf.d
install -m 0644 "$QUELLE/watchdog.conf" /etc/systemd/system.conf.d/watchdog.conf
# daemon-reexec startet den systemd-Manager neu und übernimmt dabei die
# Watchdog-Einstellung. Laufende Dienste bleiben unberührt.
systemctl daemon-reexec

schritt '3/5  Telemetrie-Sampler einrichten'
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

schritt '4/5  sar auf Minutentakt verdichten'
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

schritt '5/5  Ergebnis pruefen'
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
printf 'Der Watchdog muss state=active und ein Timeout nahe 120s zeigen.\n'
printf 'Weicht das Timeout ab, hat der Treiber den Wert gekappt -- siehe README.md.\n'

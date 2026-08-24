# GX10: Selbstheilung und Messung

Host-Konfiguration für die GX10 (ASUS Ascent, GB10). Zweck ist **nicht**, die
Maschine zu beschränken, sondern sie einen Freeze überleben zu lassen und beim
nächsten Mal auswertbare Spuren zu hinterlassen.

Der Hintergrund und die verworfenen Hypothesen stehen in
[docs/gx10-freeze-befund.md](../../docs/gx10-freeze-befund.md).

## Installation

```bash
cd ops/gx10
sudo ./install.sh
```

Das Skript ist idempotent und sichert die ersetzten Dateien nach
`/var/backups/gx10-setup-<zeitstempel>/`.

## Was installiert wird

| Datei | Ziel | Zweck |
|---|---|---|
| `watchdog.conf` | `/etc/systemd/system.conf.d/` | Hardware-Watchdog, 120 s |
| `gx10-telemetrie.sh` | `/usr/local/bin/` | Sampler, 5 s Takt |
| `gx10-telemetrie.service` | `/etc/systemd/system/` | Dienst für den Sampler |
| `gx10-telemetrie.logrotate` | `/etc/logrotate.d/gx10-telemetrie` | 14 Tage Aufbewahrung |
| `gx10-freeze-report.sh` | `/usr/local/bin/` | Auswertung nach einem Freeze |
| `sysstat-collect-minutentakt.conf` | `/etc/systemd/system/sysstat-collect.timer.d/` | sar im Minutentakt statt alle 10 min |
| `sysstat.cron` | `/etc/cron.d/sysstat` | Debian-Vorgabe, nur zur Wiederherstellung |

## Besonderheit dieser Maschine: blockierter Boot

`systemctl is-system-running` meldet dauerhaft `starting`, nicht `running`.
Ursache ist `plymouth-quit-wait.service`, das seit dem Systemstart auf einen
Splash-Screen wartet, den es headless nie gibt. Dahinter stauen sich rund neun
Jobs, darunter `multi-user.target`, `getty.target` und `x11vnc.service`.

Praktische Folgen:

- Units dürfen **nicht** `After=multi-user.target` mit `WantedBy=multi-user.target`
  kombinieren. Sie würden nie starten, und `systemctl enable --now` wartet
  endlos mit. Der Telemetrie-Dienst nutzt deshalb `After=local-fs.target`.
- `systemctl list-jobs` zeigt dauerhaft wartende Jobs. Das ist hier normal und
  kein Hinweis auf einen Fehler der Installation.

Das ist ein eigenständiges Thema und wurde bewusst nicht mitverändert.

## Besonderheit: sar sammelt über systemd, nicht über cron

`/etc/default/sysstat` steht auf `ENABLED="false"`. Der Eintrag in
`/etc/cron.d/sysstat` ruft zwar `debian-sa1` auf, dieses bricht wegen des
Schalters aber folgenlos ab. Die Messpunkte stammen ausschliesslich vom Timer
`sysstat-collect.timer`, der `/usr/lib/sysstat/sa1` direkt startet.

Wer den Takt über die cron-Datei ändert, ändert damit nichts. Die Verdichtung
läuft deshalb über ein Timer-Drop-in. Zu beachten ist dort, dass `OnCalendar`
additiv wirkt: Ohne vorangestelltes leeres `OnCalendar=` bliebe die Vorgabe
`*:00/10` zusätzlich aktiv.

Prüfen lässt sich der Takt mit:

```bash
systemctl show sysstat-collect.timer -p TimersCalendar --value
systemctl list-timers --all | grep sysstat
```

## Verifikation nach der Installation

Der Watchdog muss `active` melden:

```bash
cat /sys/class/watchdog/watchdog0/state    # erwartet: active
cat /sys/class/watchdog/watchdog0/timeout  # erwartet: 120
systemctl show -p RuntimeWatchdogUSec      # erwartet: 2min
```

Meldet `timeout` einen kleineren Wert, hat der Treiber 120 s gekappt. Das ist
kein Fehler — der gekappte Wert gilt dann, und `RuntimeWatchdogSec` in
`watchdog.conf` sollte darauf angepasst werden, damit systemd und Hardware
übereinstimmen.

Der Sampler muss laufend schreiben:

```bash
systemctl status gx10-telemetrie.service
tail -f /var/log/gx10-telemetrie.log
```

## Auswertung nach einem Freeze

```bash
gx10-freeze-report.sh        # letzte 6 Minuten
gx10-freeze-report.sh 240    # letzte 20 Minuten
```

Der Bericht zeigt Watchdog-Status, die Telemetrie bis zum Abriss, `sar`-Werte
derselben Spanne, die letzten Ollama-Zeilen und auffällige Kernel-Meldungen.

Worauf zu achten ist:

- **`bootstatus` ungleich 0** — der Neustart ging vom Watchdog aus, die
  Selbstheilung hat also funktioniert.
- **`TIMEOUT` in den GPU-Spalten** — `nvidia-smi` antwortete nicht mehr. Das
  wäre der erste harte Hinweis auf einen Treiber- oder GPU-Hang.
- **`gpu_throttle` ungleich `0x0...0`** — die GPU drosselt. Zusammen mit
  `gpu_power_w` prüft das die Hypothese, dass Dauerlast an der Leistungsgrenze
  die Ursache ist.
- **`mem_available_kb` fällt in den Schlusssekunden** — nur dann wäre der
  Speicher doch beteiligt. Die bisherigen Daten sprechen dagegen.

## Rücknahme

```bash
sudo systemctl disable --now gx10-telemetrie.service
sudo rm /etc/systemd/system/gx10-telemetrie.service \
        /etc/systemd/system.conf.d/watchdog.conf \
        /etc/logrotate.d/gx10-telemetrie \
        /usr/local/bin/gx10-telemetrie.sh \
        /usr/local/bin/gx10-freeze-report.sh
sudo rm -rf /etc/systemd/system/sysstat-collect.timer.d
sudo systemctl daemon-reload
sudo systemctl restart sysstat-collect.timer
sudo systemctl daemon-reexec
```

`daemon-reexec` schaltet den Watchdog wieder ab. Da `nowayout` auf `0` steht,
ist das gefahrlos möglich.

## Platzbedarf

Der Minutentakt vergrössert die `sar`-Dateien etwa um den Faktor zehn, auf
grob 12 MB pro Tag bei sieben Tagen Aufbewahrung. Die Telemetriedatei wächst
mit rund 1.5 MB pro Tag. Bei 1.5 TB freiem Platz ist beides unerheblich.

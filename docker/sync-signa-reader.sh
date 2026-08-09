#!/usr/bin/env bash
# Das Lesepaket von Signa in den Build-Kontext des Frontends kopieren.
#
# Warum kopieren und nicht verlinken: Der Docker-Build hat als Kontext ausschliesslich
# src/frontend. Ein Verweis auf ein Nachbarverzeichnis (file:../../..) laesst sich lokal
# aufloesen, im Image aber nicht - der Build braeche erst dort, also spaet.
#
# Es ist dasselbe Muster wie bei contentConverter und AI9 im Backend (docker/build.sh),
# nur fuer ein npm-Paket statt fuer ein Python-Paket.
#
# Aufruf:
#   ./docker/sync-signa-reader.sh
#   SIGNA_PATH=/anderer/pfad ./docker/sync-signa-reader.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR/.."
ZIEL="$PROJECT_ROOT/src/frontend/vendor/signa-reader"

SIGNA_SRC="${SIGNA_PATH:-$HOME/dev/github/InnoSmithSigna}"
QUELLE="$SIGNA_SRC/frontend/reader"

if [ ! -d "$QUELLE" ]; then
    echo "FEHLER: Signa-Lesepaket nicht gefunden unter $QUELLE"
    echo "Setze SIGNA_PATH auf das Wurzelverzeichnis von InnoSmithSigna."
    exit 1
fi

rm -rf "$ZIEL"
mkdir -p "$(dirname "$ZIEL")"
cp -r "$QUELLE" "$ZIEL"
rm -rf "$ZIEL/node_modules" "$ZIEL/.git"

# Der Stand wird festgehalten, damit man im Zweifel sieht, welche Fassung im Image
# steckt. Eine Kopie ohne Herkunft ist spaeter nicht mehr zuzuordnen.
{
    echo "Quelle:  $QUELLE"
    echo "Kopiert: $(date -Iseconds)"
    if git -C "$SIGNA_SRC" rev-parse --short HEAD >/dev/null 2>&1; then
        echo "Stand:   $(git -C "$SIGNA_SRC" rev-parse --short HEAD)"
    fi
} >"$ZIEL/HERKUNFT.txt"

echo "  Signa-Lesepaket: $QUELLE -> src/frontend/vendor/signa-reader"

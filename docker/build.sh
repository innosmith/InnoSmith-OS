#!/usr/bin/env bash
# Build-Script: Bereitet den Docker-Build-Kontext vor und baut Images.
# Kopiert private Packages in vendor/, damit sie ohne Git-Credentials
# im Docker-Image installiert werden koennen.
#
# Aufruf:
#   ./docker/build.sh          # Nur vendor vorbereiten
#   ./docker/build.sh --all    # vendor + alle Images bauen

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$SCRIPT_DIR/.."
VENDOR_DIR="$PROJECT_ROOT/src/backend/vendor"

echo "==> Vendor-Verzeichnis vorbereiten..."
rm -rf "$VENDOR_DIR"
mkdir -p "$VENDOR_DIR"

CONTENTCONVERTER_SRC="${CONTENTCONVERTER_PATH:-$HOME/dev/github/contentConverter}"

if [ ! -d "$CONTENTCONVERTER_SRC" ]; then
    echo "FEHLER: contentConverter nicht gefunden unter $CONTENTCONVERTER_SRC"
    echo "Setze CONTENTCONVERTER_PATH auf den korrekten Pfad."
    exit 1
fi

echo "  contentConverter: $CONTENTCONVERTER_SRC"
cp -r "$CONTENTCONVERTER_SRC" "$VENDOR_DIR/contentconverter"
rm -rf "$VENDOR_DIR/contentconverter/.git"

# AI9-Core (privates Schwester-Repo, InnoSmith-IP). Wird wie contentConverter
# vendored, damit das Docker-Image ohne Git-Credentials/Registry gebaut werden
# kann. Dev nutzt stattdessen den Editable-Install (siehe requirements.txt).
AI9_SRC="${AI9_PATH:-$HOME/dev/github/AI9}"

if [ ! -d "$AI9_SRC" ]; then
    echo "FEHLER: AI9-Core nicht gefunden unter $AI9_SRC"
    echo "Setze AI9_PATH auf den korrekten Pfad."
    exit 1
fi

echo "  AI9-Core: $AI9_SRC"
cp -r "$AI9_SRC" "$VENDOR_DIR/ai9"
rm -rf "$VENDOR_DIR/ai9/.git" \
       "$VENDOR_DIR/ai9/.venv" \
       "$VENDOR_DIR/ai9/.pytest_cache" \
       "$VENDOR_DIR/ai9/.ruff_cache"

echo "==> Vendor-Verzeichnis bereit."

# Signa-Lesepaket ins Frontend vendoren. Gleicher Grund wie oben: Der Build-Kontext des
# Frontend-Images ist src/frontend, ein Verweis nach aussen laesst sich dort nicht
# aufloesen.
echo ""
echo "==> Signa-Lesepaket vendoren..."
"$SCRIPT_DIR/sync-signa-reader.sh"

# Sandbox-Image bauen (falls Dockerfile vorhanden)
if [ -f "$SCRIPT_DIR/sandbox/Dockerfile" ]; then
    echo ""
    echo "==> Sandbox-Image bauen..."
    docker build -t taskpilot-sandbox:latest "$SCRIPT_DIR/sandbox/"
fi

# Sandbox-Executor-Image bauen (Sidecar mit docker.sock-Zugriff)
if [ -f "$SCRIPT_DIR/Dockerfile.sandbox-executor" ]; then
    echo ""
    echo "==> Sandbox-Executor-Image bauen..."
    docker build -t taskpilot-sandbox-executor:latest \
        -f "$SCRIPT_DIR/Dockerfile.sandbox-executor" "$PROJECT_ROOT"
fi

if [ "${1:-}" = "--all" ]; then
    echo ""
    echo "==> Integration-Images bauen..."
    docker compose -f "$SCRIPT_DIR/docker-compose.yml" \
        -f "$SCRIPT_DIR/docker-compose.integration.yml" build

    echo ""
    echo "==> Produktion-Images bauen..."
    docker compose -f "$SCRIPT_DIR/docker-compose.prod.yml" build

    echo ""
    echo "==> Alle Images gebaut."
else
    echo ""
    echo "Naechste Schritte:"
    echo "  make int    # Integration starten"
    echo "  make prod   # Produktion starten"
    echo "  make build  # Alle Images bauen (ohne Start)"
fi

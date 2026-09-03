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
AI9_REF="${AI9_REF:-v0.5.2}"

if [ ! -d "$AI9_SRC" ]; then
    echo "FEHLER: AI9-Core nicht gefunden unter $AI9_SRC"
    echo "Setze AI9_PATH auf den korrekten Pfad."
    exit 1
fi

# Bis zum 25.08.2026 wurde hier ungeprueft kopiert, was gerade dalag. Das
# Vendor-Verzeichnis stand dadurch auf 0.4.4, waehrend das Backend gegen 0.5.0
# entwickelt wurde -- ein Abbild aus diesem Stand waere beim ersten Import
# gescheitert, und zwar erst im Container. Dieselbe Schranke wie im GSW-Cockpit:
# Ohne Schild kein Abbild.
if ! git -C "$AI9_SRC" rev-parse --verify --quiet "$AI9_REF" >/dev/null; then
    echo "FEHLER: Schild $AI9_REF existiert im AI9-Repository nicht." >&2
    echo "Vorhandene Schilder: $(git -C "$AI9_SRC" tag -l | tr '\n' ' ')" >&2
    exit 1
fi

AI9_HEAD="$(git -C "$AI9_SRC" rev-parse HEAD)"
AI9_TAGGED="$(git -C "$AI9_SRC" rev-parse "${AI9_REF}^{commit}")"

if [ "$AI9_HEAD" != "$AI9_TAGGED" ]; then
    echo "FEHLER: Die Arbeitskopie von AI9 steht nicht auf $AI9_REF." >&2
    echo "  HEAD:    $AI9_HEAD" >&2
    echo "  $AI9_REF: $AI9_TAGGED" >&2
    echo "Wechsle dorthin oder setze AI9_REF auf die gewuenschte Fassung." >&2
    exit 1
fi

if [ -n "$(git -C "$AI9_SRC" status --porcelain)" ]; then
    echo "FEHLER: Die Arbeitskopie von AI9 hat nicht eingecheckte Aenderungen." >&2
    echo "Auf dem Schild stuende $AI9_REF, kopiert wuerde etwas anderes." >&2
    exit 1
fi

echo "  AI9-Core: $AI9_SRC ($AI9_REF)"
cp -r "$AI9_SRC" "$VENDOR_DIR/ai9"
rm -rf "$VENDOR_DIR/ai9/.git" \
       "$VENDOR_DIR/ai9/.venv" \
       "$VENDOR_DIR/ai9/.pytest_cache" \
       "$VENDOR_DIR/ai9/.ruff_cache"

echo "$AI9_REF ($AI9_TAGGED)" > "$VENDOR_DIR/ai9/VENDORED_REF"

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

#!/usr/bin/env bash
# build.sh — Build AutoNotes.app (standalone macOS application)
# Usage: ./build.sh [--clean]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Homebrew Python — the interpreter the app and all deps are installed on
PYTHON="/opt/homebrew/bin/python3"

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
    echo "Installing PyInstaller…"
    "$PYTHON" -m pip install --break-system-packages pyinstaller
fi

if [[ "${1:-}" == "--clean" ]]; then
    echo "Cleaning previous build artefacts…"
    rm -rf build dist __pycache__
fi

echo "Building AutoNotes.app — this will take several minutes…"
"$PYTHON" -m PyInstaller \
    --noconfirm \
    --log-level WARN \
    AutoNotes.spec

APP="$SCRIPT_DIR/dist/AutoNotes.app"
if [[ -d "$APP" ]]; then
    # Ad-hoc sign so macOS Gatekeeper allows it to open
    codesign --force --deep --sign - "$APP" 2>/dev/null || true
    echo ""
    echo "✓ Build complete: $APP"
    echo "  Size: $(du -sh "$APP" | cut -f1)"

    bash "$SCRIPT_DIR/make_dmg.sh"

    echo ""
    echo "  To open:       open \"$APP\""
    echo "  To distribute: share dist/AutoNotes.dmg"
    echo "  To install:    ditto \"$APP\" /Applications/AutoNotes.app   (NOT cp -r — it breaks symlinks)"
else
    echo "Build failed — no .app produced."
    exit 1
fi

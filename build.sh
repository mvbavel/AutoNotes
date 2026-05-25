#!/usr/bin/env bash
# build.sh — Build AutoNotes.app (standalone macOS application)
# Usage: ./build.sh [--clean]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

PYINSTALLER="$HOME/Library/Python/3.9/bin/pyinstaller"
PYTHON="/usr/bin/python3"

if [[ ! -x "$PYINSTALLER" ]]; then
    echo "Installing PyInstaller into Python 3.9…"
    "$PYTHON" -m pip install pyinstaller --user
fi

if [[ "${1:-}" == "--clean" ]]; then
    echo "Cleaning previous build artefacts…"
    rm -rf build dist __pycache__
fi

echo "Building AutoNotes.app — this will take several minutes…"
"$PYINSTALLER" \
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
    echo ""
    echo "  To open:  open \"$APP\""
    echo "  To copy to Applications:  cp -r \"$APP\" /Applications/"
else
    echo "Build failed — no .app produced."
    exit 1
fi

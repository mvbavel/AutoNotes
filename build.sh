#!/usr/bin/env bash
# build.sh — Build AutoNotes.app (standalone macOS application)
# Usage: ./build.sh [--clean]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Homebrew Python — the interpreter the app and all deps are installed on.
# Versioned on purpose: the unversioned python3 link follows Homebrew's
# default Python (now 3.15), which has none of the build dependencies.
PYTHON="/opt/homebrew/bin/python3.14"

if ! "$PYTHON" -m PyInstaller --version >/dev/null 2>&1; then
    echo "Installing PyInstaller…"
    "$PYTHON" -m pip install --break-system-packages pyinstaller
fi

# Refuse to bundle dependencies that don't meet requirements.txt. The build
# packs whatever this shared interpreter has, and nothing else checked it: a
# DMG shipped with the yt-dlp that YouTube 403s, and with an anthropic version
# the requirements excluded. pip's dry run lists anything it would change.
echo "Checking build dependencies against requirements.txt…"
"$PYTHON" scripts/check_deps.py
"$PYTHON" -c 'from pipeline.ytdlp_health import staleness_warning as w; m = w(); print(f"Warning: {m}") if m else None'

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

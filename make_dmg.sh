#!/usr/bin/env bash
# make_dmg.sh — Wrap dist/AutoNotes.app in a distributable DMG
# Usage: ./make_dmg.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP="dist/AutoNotes.app"
DMG_OUT="dist/AutoNotes.dmg"

if [[ ! -d "$APP" ]]; then
    echo "Error: $APP not found — run ./build.sh first."
    exit 1
fi

rm -f "$DMG_OUT"

# Stage app + Applications shortcut in a temp folder so hdiutil auto-sizes
STAGING=$(mktemp -d)
trap 'rm -rf "$STAGING"' EXIT

# ditto preserves the app's internal symlinks (cp -r follows them, which
# flattens the Qt framework structure and makes the app crash at launch)
ditto "$APP" "$STAGING/AutoNotes.app"
ln -s /Applications "$STAGING/Applications"

echo "Creating DMG (this takes a minute for large apps)…"
hdiutil create \
    -volname "AutoNotes" \
    -srcdir "$STAGING" \
    -ov \
    -format UDZO \
    -imagekey zlib-level=6 \
    -quiet \
    "$DMG_OUT"

echo "✓ DMG ready: $DMG_OUT"
echo "  Size: $(du -sh "$DMG_OUT" | cut -f1)"

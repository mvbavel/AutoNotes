#!/usr/bin/env bash
# make_dmg.sh — Wrap dist/AutoNotes.app in a distributable DMG
# Usage: ./make_dmg.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

APP="dist/AutoNotes.app"
DMG_OUT="dist/AutoNotes.dmg"
RW_IMAGE="dist/.AutoNotes-rw.dmg"
VOLNAME="AutoNotes"

if [[ ! -d "$APP" ]]; then
    echo "Error: $APP not found — run ./build.sh first."
    exit 1
fi

rm -f "$DMG_OUT" "$RW_IMAGE"

# Why not the one-liner `hdiutil create -srcdir`: it mounts a temp volume,
# copies into it, then has to unmount before compressing. When something grabs
# the new volume — Spotlight, or an endpoint/AV scanner, both common on managed
# Macs — that unmount fails. It retries three times politely, has no force
# option, and dies with the badly misleading "create failed - Resource busy"
# *after* the copy has already succeeded. Driving the steps ourselves lets us
# force the detach, which is the one thing that reliably works.

DEV=""
MNT=""

cleanup() {
    # Never leave a mounted image behind: the next run would collide with it
    # and fail for a genuinely different reason.
    if [[ -n "$DEV" ]] && hdiutil info | grep -q "^${DEV}"; then
        hdiutil detach "$DEV" -force >/dev/null 2>&1 || true
    fi
    rm -f "$RW_IMAGE"
}
trap cleanup EXIT

# HFS+ needs headroom beyond the payload; 10% + 150 MB covers catalog overhead
# comfortably without inflating the intermediate image.
app_mb=$(du -sm "$APP" | cut -f1)
size_mb=$(( app_mb + app_mb / 10 + 150 ))

echo "Creating DMG (this takes a minute for large apps)…"
hdiutil create -size "${size_mb}m" -fs HFS+ -volname "$VOLNAME" -ov -quiet "$RW_IMAGE"

# Parse both device and mount point from the attach table: a stale volume of the
# same name would make macOS mount us at "AutoNotes 1", so neither can be assumed.
attach_out=$(hdiutil attach "$RW_IMAGE" -nobrowse -noautoopen | grep '^/dev/' | grep '/Volumes/')
DEV=$(echo "$attach_out" | tail -1 | awk '{print $1}')
MNT=$(echo "$attach_out" | tail -1 | sed -E 's|^.*(/Volumes/.*)$|\1|')

# Discourage the indexing that causes the unmount contention in the first place
touch "$MNT/.metadata_never_index" 2>/dev/null || true
mdutil -i off "$MNT" >/dev/null 2>&1 || true

# ditto preserves the app's internal symlinks (cp -r follows them, which
# flattens the Qt framework structure and makes the app crash at launch)
ditto "$APP" "$MNT/AutoNotes.app"
ln -s /Applications "$MNT/Applications"
sync

detached=0
for attempt in 1 2 3 4 5; do
    if hdiutil detach "$DEV" -quiet 2>/dev/null; then
        detached=1
        break
    fi
    sleep 3   # give whatever is scanning the volume a chance to let go
done
if [[ "$detached" -eq 0 ]]; then
    echo "  Volume busy after 5 attempts — forcing detach"
    hdiutil detach "$DEV" -force >/dev/null
fi
DEV=""   # detached; stop cleanup() trying again

hdiutil convert "$RW_IMAGE" -format UDZO -imagekey zlib-level=6 -quiet -o "$DMG_OUT"
rm -f "$RW_IMAGE"

# A DMG that fails checksum is worse than no DMG — it fails at the user's end
hdiutil verify "$DMG_OUT" >/dev/null 2>&1 || {
    echo "Error: $DMG_OUT failed checksum verification."
    exit 1
}

echo "✓ DMG ready: $DMG_OUT"
echo "  Size: $(du -sh "$DMG_OUT" | cut -f1)"

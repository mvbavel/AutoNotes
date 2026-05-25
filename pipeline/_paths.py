import os
import sys

_SEARCH_DIRS = [
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/bin",
]


def _find_binary(name: str) -> str:
    # When frozen, binaries land in the same dir as the executable
    if getattr(sys, "frozen", False):
        candidate = os.path.join(sys._MEIPASS, name)
        if os.path.isfile(candidate):
            return candidate
    for d in _SEARCH_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return name


FFMPEG = _find_binary("ffmpeg")
FFPROBE = _find_binary("ffprobe")

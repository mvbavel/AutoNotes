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


def ytdlp_command() -> list[str]:
    """Command prefix for invoking yt-dlp.

    In the frozen app, re-exec our own executable with --yt-dlp so the
    bundled yt_dlp package runs (a copied Homebrew shim would depend on a
    Python environment the target machine doesn't have). In development,
    use the system yt-dlp.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--yt-dlp"]
    return [_find_binary("yt-dlp")]

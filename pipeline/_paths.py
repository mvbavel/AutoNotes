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


_MAIN_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


# Fetch DASH/HLS fragments in parallel: Teams recordings arrive as ~1,350
# fragments for an hour, and one at a time took ~20 minutes. Kept modest so
# SharePoint doesn't start answering 429s. No effect on single-file formats.
YTDLP_FRAGMENT_ARGS = ["--concurrent-fragments", "4"]


def ytdlp_command() -> list[str]:
    """Command prefix for invoking yt-dlp.

    Both modes re-exec through our own --yt-dlp dispatch so the child inherits
    the TLS trust main.py installs. A bare system binary cannot: on a
    TLS-inspecting network the corporate root is keychain-only and not
    RFC-compliant, so Python 3.13+ rejects it under VERIFY_X509_STRICT and every
    download fails — and truststore's macOS-native verification only applies
    in-process. Running through main.py also keeps dev on the same yt_dlp the
    build bundles, instead of whatever version the system binary happens to be.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--yt-dlp"]
    if os.path.isfile(_MAIN_PY):
        return [sys.executable, _MAIN_PY, "--yt-dlp"]
    return [_find_binary("yt-dlp")]

import glob
import json
import os
import re
import subprocess

from pipeline._paths import FFMPEG, ytdlp_command
from pipeline._util import safe_filename
from pipeline.vtt_parser import parse_srt

YTDLP_CMD = ytdlp_command()

_METADATA_TIMEOUT = 60  # seconds for the yt-dlp metadata fetch

# No client pinning or UA spoofing: yt-dlp's maintained defaults pick working
# player clients (a pinned ios/android/web list left only the 360p legacy
# format once YouTube gated those clients behind PO tokens/SABR), and with
# curl_cffi installed it impersonates a browser TLS fingerprint automatically.
_BASE_ARGS = [
    "--no-playlist",
    "--retries", "5",
    "--fragment-retries", "5",
]


def download_youtube(
    url: str, output_dir: str, progress_cb=None, log_cb=None, cancel_check=None
) -> tuple[str, str, str, list]:
    """Download a YouTube video and return (file_path, title, description, chapters).

    Also attempts to download subtitles (manual then auto-generated) as SRT files
    alongside the video. Callers can look for *.srt files in output_dir afterward.
    """
    info = _run_json(YTDLP_CMD + _BASE_ARGS + ["--dump-single-json", url])
    title = info.get("title", "video")
    description = info.get("description", "") or ""
    chapters = info.get("chapters") or []
    safe_title = safe_filename(title)
    out_template = os.path.join(output_dir, f"{safe_title}.%(ext)s")

    ffmpeg_dir = os.path.dirname(FFMPEG)
    dl_args = [
        *YTDLP_CMD,
        *_BASE_ARGS,
        # Highest resolution regardless of container: 1440p/4K on YouTube is
        # VP9/AV1, which an mp4-first format string silently caps at 1080p.
        # Merge prefers mp4, falls back to mkv for codecs mp4 can't carry —
        # ffmpeg reads either downstream.
        "--format", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4/mkv",
        "--ffmpeg-location", ffmpeg_dir,
        # Fetch subtitles (manual preferred, auto-generated as fallback)
        "--write-sub",
        "--write-auto-sub",
        "--sub-langs", "en.*",
        "--convert-subs", "srt",
        "--newline",
        "--progress",
        "-o", out_template,
        url,
    ]
    proc = subprocess.Popen(
        dl_args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        for line in proc.stdout:
            if cancel_check:
                cancel_check()
            line = line.rstrip()
            if not line:
                continue
            m = re.search(r"(\d+(?:\.\d+)?)%", line)
            if m:
                pct = min(int(float(m.group(1))), 100)
                if progress_cb:
                    progress_cb(pct)
                if log_cb:
                    log_cb(f"Downloading: {m.group(1)}%")
            elif log_cb and not line.startswith("[debug]"):
                log_cb(line)
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp exited with code {proc.returncode}")

    out_path = _find_output(output_dir, safe_title)
    if out_path is None:
        raise RuntimeError("Download finished but no video file was produced")

    return out_path, title, description, chapters


def _find_output(output_dir: str, safe_title: str) -> str | None:
    """Locate the downloaded video (container depends on the merged codecs)."""
    for ext in ("mp4", "mkv", "webm", "mov"):
        candidate = os.path.join(output_dir, f"{safe_title}.{ext}")
        if os.path.exists(candidate):
            return candidate
    return None


def find_transcript(output_dir: str) -> list[dict] | None:
    """Look for a downloaded SRT subtitle file and parse it into segments.

    Returns [{start, end, text}] or None if no subtitle file was found.
    """
    srt_files = glob.glob(os.path.join(output_dir, "*.srt"))
    if not srt_files:
        return None
    segments = parse_srt(srt_files[0])
    return segments if segments else None


def _run_json(cmd: list[str]) -> dict:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=_METADATA_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"yt-dlp metadata fetch timed out after {_METADATA_TIMEOUT}s")
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp metadata fetch failed")
    # Find the JSON line (last non-empty line)
    for line in reversed(result.stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise RuntimeError("No JSON output from yt-dlp")

import json
import os
import re
import subprocess

from pipeline._paths import FFMPEG, _find_binary

YTDLP = _find_binary("yt-dlp")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_BASE_ARGS = [
    "--user-agent", _UA,
    "--extractor-args", "youtube:player_client=ios,android,web",
    "--no-playlist",
    "--retries", "5",
    "--fragment-retries", "5",
]


def download_youtube(url: str, output_dir: str, progress_cb=None, log_cb=None) -> tuple[str, str]:
    """Download a YouTube video and return (file_path, title)."""
    # Fetch title/metadata without downloading
    info = _run_json([YTDLP] + _BASE_ARGS + ["--dump-single-json", url])
    title = info.get("title", "video")
    safe_title = _safe_filename(title)
    out_template = os.path.join(output_dir, f"{safe_title}.%(ext)s")
    out_path = os.path.join(output_dir, f"{safe_title}.mp4")

    ffmpeg_dir = os.path.dirname(FFMPEG)
    dl_args = [
        YTDLP,
        *_BASE_ARGS,
        "--format", (
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]"
            "/bestvideo+bestaudio"
            "/best[ext=mp4]"
            "/best"
        ),
        "--merge-output-format", "mp4",
        "--ffmpeg-location", ffmpeg_dir,
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
    for line in proc.stdout:
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
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp exited with code {proc.returncode}")

    return out_path, title


def _run_json(cmd: list[str]) -> dict:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp metadata fetch failed")
    # Find the JSON line (last non-empty line)
    for line in reversed(result.stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise RuntimeError("No JSON output from yt-dlp")


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:80]

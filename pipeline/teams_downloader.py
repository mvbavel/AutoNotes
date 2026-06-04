"""Download Teams/SharePoint recordings via yt-dlp with browser cookies."""
import glob
import os
import re
import subprocess

from pipeline._paths import FFMPEG, _find_binary
from pipeline.vtt_parser import parse_vtt

YTDLP = _find_binary("yt-dlp")

_TEAMS_PATTERNS = [
    r"teams\.microsoft\.com",
    r"sharepoint\.com",
    r"stream\.microsoft\.com",
    r"microsoftstream\.com",
]

_BROWSERS = ["chrome", "edge"]   # preference order per user request


def is_teams_url(url: str) -> bool:
    return any(re.search(p, url, re.I) for p in _TEAMS_PATTERNS)


def download_teams_recording(
    url: str,
    output_dir: str,
    progress_cb=None,
    log_cb=None,
) -> dict:
    """Download a Teams/SharePoint recording and return a context dict.

    Returns:
        {
            video_path: str | None,
            title: str,
            description: str,
            duration: float,
            transcript_segments: list[dict] | None,  # from VTT subtitles
        }
    """
    result = {
        "video_path": None,
        "title": "Teams Recording",
        "description": "",
        "duration": 0.0,
        "transcript_segments": None,
    }

    info = _fetch_info(url, log_cb)
    if info:
        result["title"] = info.get("title") or "Teams Recording"
        result["description"] = info.get("description") or ""
        result["duration"] = float(info.get("duration") or 0)

    safe_title = _safe_filename(result["title"])
    out_template = os.path.join(output_dir, f"{safe_title}.%(ext)s")
    out_path = os.path.join(output_dir, f"{safe_title}.mp4")

    ffmpeg_dir = os.path.dirname(FFMPEG)

    success = False
    for browser in _BROWSERS:
        if log_cb:
            log_cb(f"Attempting download with {browser} cookies…")
        ok = _run_download(url, out_template, ffmpeg_dir, browser, log_cb)
        if ok:
            success = True
            break

    if not success:
        raise RuntimeError(
            "Could not download the recording. Make sure you are logged into "
            "Microsoft/Teams in Chrome or Edge and the URL is accessible."
        )

    # yt-dlp may produce a .mkv or other container — find what landed
    for ext in ("mp4", "mkv", "webm", "mov"):
        candidate = os.path.join(output_dir, f"{safe_title}.{ext}")
        if os.path.exists(candidate):
            result["video_path"] = candidate
            break

    if progress_cb:
        progress_cb(80)

    # Parse any downloaded VTT/SRT subtitle file
    vtt_files = glob.glob(os.path.join(output_dir, "*.vtt"))
    srt_files = glob.glob(os.path.join(output_dir, "*.srt"))

    if vtt_files:
        if log_cb:
            log_cb("Parsing VTT transcript with speaker names…")
        try:
            result["transcript_segments"] = parse_vtt(vtt_files[0])
            if log_cb:
                log_cb(f"Transcript: {len(result['transcript_segments'])} segments")
        except Exception as e:
            if log_cb:
                log_cb(f"VTT parse failed: {e}")
    elif srt_files:
        from pipeline.downloader import _parse_srt
        if log_cb:
            log_cb("Parsing SRT transcript…")
        result["transcript_segments"] = _parse_srt(srt_files[0])

    if progress_cb:
        progress_cb(100)

    return result


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fetch_info(url: str, log_cb=None) -> dict | None:
    for browser in _BROWSERS:
        try:
            cmd = [
                YTDLP,
                "--cookies-from-browser", browser,
                "--dump-single-json",
                "--no-playlist",
                url,
            ]
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                import json
                return json.loads(out.stdout)
        except Exception:
            pass
    return None


def _run_download(url: str, out_template: str, ffmpeg_dir: str, browser: str, log_cb=None) -> bool:
    cmd = [
        YTDLP,
        "--cookies-from-browser", browser,
        "--no-playlist",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--ffmpeg-location", ffmpeg_dir,
        "--write-sub",
        "--write-auto-sub",
        "--sub-langs", "en.*",
        "--convert-subs", "srt",
        "--write-sub",          # also try VTT natively
        "--sub-format", "vtt/srt/best",
        "--newline",
        "--progress",
        "-o", out_template,
        url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if log_cb and proc.stdout:
            for line in proc.stdout.splitlines()[-10:]:
                if line.strip():
                    log_cb(line.strip())
        return proc.returncode == 0
    except Exception:
        return False


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:80].strip()

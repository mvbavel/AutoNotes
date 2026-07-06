"""Download Teams/SharePoint recordings via yt-dlp with browser cookies."""
import glob
import json
import os
import re
import subprocess

from pipeline._paths import FFMPEG, _find_binary
from pipeline._util import safe_filename
from pipeline.vtt_parser import parse_srt, parse_vtt

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
    cancel_check=None,
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

    safe_title = safe_filename(result["title"])
    out_template = os.path.join(output_dir, f"{safe_title}.%(ext)s")

    ffmpeg_dir = os.path.dirname(FFMPEG)

    success = False
    for browser in _BROWSERS:
        if log_cb:
            log_cb(f"Attempting download with {browser} cookies…")
        ok = _run_download(url, out_template, ffmpeg_dir, browser,
                           progress_cb=progress_cb, log_cb=log_cb,
                           cancel_check=cancel_check)
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

    # Parse any downloaded VTT/SRT subtitle file (VTT preferred: keeps speaker tags)
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
        if log_cb:
            log_cb("Parsing SRT transcript…")
        result["transcript_segments"] = parse_srt(srt_files[0])

    if progress_cb:
        progress_cb(100)

    return result


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fetch_info(url: str, log_cb=None) -> dict | None:
    for browser in _BROWSERS:
        cmd = [
            YTDLP,
            "--cookies-from-browser", browser,
            "--dump-single-json",
            "--no-playlist",
            url,
        ]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if out.returncode == 0:
                return json.loads(out.stdout)
            if log_cb:
                err = (out.stderr or "").strip().splitlines()
                log_cb(f"Metadata fetch with {browser} cookies failed"
                       + (f": {err[-1]}" if err else ""))
        except Exception as e:
            if log_cb:
                log_cb(f"Metadata fetch with {browser} cookies failed: {e}")
    return None


def _run_download(url: str, out_template: str, ffmpeg_dir: str, browser: str,
                  progress_cb=None, log_cb=None, cancel_check=None) -> bool:
    cmd = [
        YTDLP,
        "--cookies-from-browser", browser,
        "--no-playlist",
        "--format", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "--merge-output-format", "mp4",
        "--ffmpeg-location", ffmpeg_dir,
        # Keep subtitles in VTT where possible — Teams VTT carries speaker tags
        # that parse_vtt() needs; converting to SRT would destroy them
        "--write-sub",
        "--write-auto-sub",
        "--sub-langs", "en.*",
        "--sub-format", "vtt/srt/best",
        "--newline",
        "--progress",
        "-o", out_template,
        url,
    ]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
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
                pct = min(int(float(m.group(1)) * 0.8), 80)  # download = 0-80% of stage
                if progress_cb:
                    progress_cb(pct)
            elif log_cb and not line.startswith("[debug]"):
                log_cb(line)
        proc.wait()
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    return proc.returncode == 0

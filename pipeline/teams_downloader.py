"""Download Teams/SharePoint recordings via yt-dlp with browser cookies."""
import glob
import json
import os
from urllib.parse import urlparse

from pipeline._paths import FFMPEG, YTDLP_FRAGMENT_ARGS, ytdlp_command
from pipeline._util import (
    PipelineCancelled, find_video, run_capture, run_streaming, safe_filename,
    ytdlp_template,
)
from pipeline.download_progress import DownloadProgress
from pipeline.sharepoint_transcript import fetch_transcript
from pipeline.vtt_parser import parse_srt, parse_vtt

YTDLP_CMD = ytdlp_command()

# Kept under its old name for tests written against it
_DownloadProgress = DownloadProgress

_TEAMS_DOMAINS = (
    "teams.microsoft.com",
    "sharepoint.com",
    "stream.microsoft.com",
    "microsoftstream.com",
)

_METADATA_TIMEOUT = 60

# Edge first: SharePoint's FedAuth/rtFa are persistent cookies there ("keep me
# signed in"), but session-only in Chrome. --cookies-from-browser reads the
# on-disk cookie DB, so Chrome's in-memory value is never available and its
# stale on-disk copy always redirects to login.microsoftonline.com.
_BROWSERS = ["edge", "chrome"]


def is_teams_url(url: str) -> bool:
    """True when the URL's *host* is a Microsoft recording host.

    Matched on the hostname, not the whole string: a Teams URL is downloaded
    with the browser's cookies, so "youtube.com/watch?v=x&ref=sharepoint.com"
    or "evil.example/sharepoint.com/x" must not qualify.
    """
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return any(host == d or host.endswith("." + d) for d in _TEAMS_DOMAINS)


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

    info = _fetch_info(url, log_cb, cancel_check)
    if info:
        result["title"] = info.get("title") or "Teams Recording"
        result["description"] = info.get("description") or ""
        result["duration"] = float(info.get("duration") or 0)

    safe_title = safe_filename(result["title"])
    out_template = ytdlp_template(output_dir, safe_title)

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
        # Being signed in is not sufficient — the cookie jar needs a live
        # session for *this* site collection, which visiting the page mints.
        raise RuntimeError(
            "Could not download the recording.\n\n"
            "Open the recording in Microsoft Edge and let it start playing, "
            "then run AutoNotes again. Signing in is not enough on its own — "
            "SharePoint issues the session cookie only once you have opened "
            "that specific recording.\n\n"
            "Recordings on someone else's OneDrive also need to have been "
            "shared with you."
        )

    # yt-dlp may produce a .mkv or other container — find what landed
    result["video_path"] = find_video(output_dir, safe_title)
    if result["video_path"] is None:
        raise RuntimeError("The download finished but no video file was produced.")

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

    # yt-dlp's SharePoint extractor exposes no subtitles at all, so the VTT
    # branches above never fire for stream.aspx URLs — ask the Stream API
    # directly for the Teams transcript instead.
    if not result["transcript_segments"]:
        if log_cb:
            log_cb("Checking for a Teams transcript…")
        result["transcript_segments"] = fetch_transcript(
            url, browsers=_BROWSERS, log_cb=log_cb
        )

    if progress_cb:
        progress_cb(100)

    return result


# ── Internal helpers ───────────────────────────────────────────────────────────

def _fetch_info(url: str, log_cb=None, cancel_check=None) -> dict | None:
    for browser in _BROWSERS:
        cmd = [
            *YTDLP_CMD,
            "--cookies-from-browser", browser,
            "--dump-single-json",
            "--no-playlist",
            url,
        ]
        try:
            out = run_capture(cmd, timeout=_METADATA_TIMEOUT, cancel_check=cancel_check)
            if out.returncode == 0:
                return json.loads(out.stdout)
            if log_cb:
                err = (out.stderr or "").strip().splitlines()
                log_cb(f"Metadata fetch with {browser} cookies failed"
                       + (f": {err[-1]}" if err else ""))
        except PipelineCancelled:
            raise
        except Exception as e:
            if log_cb:
                log_cb(f"Metadata fetch with {browser} cookies failed: {e}")
    return None


def _run_download(url: str, out_template: str, ffmpeg_dir: str, browser: str,
                  progress_cb=None, log_cb=None, cancel_check=None) -> bool:
    cmd = [
        *YTDLP_CMD,
        "--cookies-from-browser", browser,
        "--no-playlist",
        *YTDLP_FRAGMENT_ARGS,
        "--format", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4/mkv",
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
    reporter = DownloadProgress(log_cb=log_cb, progress_cb=progress_cb)
    return run_streaming(cmd, on_line=reporter.feed, cancel_check=cancel_check) == 0

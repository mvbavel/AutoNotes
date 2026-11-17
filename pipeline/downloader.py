import glob
import json
import os
import subprocess
from collections import deque

from pipeline import kaltura
from pipeline._paths import FFMPEG, YTDLP_FRAGMENT_ARGS, ytdlp_command
from pipeline._util import (
    PipelineCancelled, find_video, run_capture, run_streaming, safe_filename,
    ytdlp_template,
)
from pipeline.download_progress import DownloadProgress
from pipeline.vtt_parser import parse_srt
from pipeline.ytdlp_health import diagnose

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
    url, info = _resolve_metadata(url, log_cb, cancel_check)
    title = info.get("title", "video")
    description = info.get("description", "") or ""
    chapters = info.get("chapters") or []
    safe_title = safe_filename(title)
    out_template = ytdlp_template(output_dir, safe_title)

    ffmpeg_dir = os.path.dirname(FFMPEG)
    dl_args = [
        *YTDLP_CMD,
        *_BASE_ARGS,
        *YTDLP_FRAGMENT_ARGS,
        # Highest resolution regardless of container: 1440p/4K on YouTube is
        # VP9/AV1, which an mp4-first format string silently caps at 1080p.
        # Merge prefers mp4, falls back to mkv for codecs mp4 can't carry —
        # ffmpeg reads either downstream.
        "--format", "bestvideo+bestaudio/best",
        "--merge-output-format", "mp4/mkv",
        "--ffmpeg-location", ffmpeg_dir,
        "--newline",
        "--progress",
        "-o", out_template,
        url,
    ]
    # Kept so a failure can be explained: yt-dlp reports an aged-out extractor
    # as a transport or format error, and the exit code alone says nothing.
    tail = deque(maxlen=40)
    # The video is most of this stage; the subtitle fetch after it is quick
    reporter = DownloadProgress(log_cb=log_cb, progress_cb=progress_cb, ceiling=95)

    def on_line(line: str) -> None:
        if line.strip():
            tail.append(line.rstrip())
        reporter.feed(line)

    returncode = run_streaming(dl_args, on_line=on_line, cancel_check=cancel_check)
    if returncode != 0:
        hint = diagnose(tail)
        raise RuntimeError(
            f"yt-dlp exited with code {returncode}. " + (hint or _last_error(tail))
        )

    out_path = _find_output(output_dir, safe_title)
    if out_path is None:
        raise RuntimeError("Download finished but no video file was produced")

    _download_subtitles(url, out_template, ffmpeg_dir, _pick_sub_lang(info), log_cb, cancel_check)

    return out_path, title, description, chapters


def _resolve_metadata(url: str, log_cb=None, cancel_check=None) -> tuple[str, dict]:
    """Return (url to download, metadata) for the first candidate yt-dlp accepts."""
    *alternatives, original = kaltura.candidate_urls(url)
    for candidate in alternatives:
        try:
            info = _run_json(YTDLP_CMD + _BASE_ARGS + ["--dump-single-json", candidate],
                             cancel_check)
        except RuntimeError:
            continue
        if log_cb:
            log_cb(f"Resolved Kaltura player page to {candidate}")
        return candidate, info
    # Last so a total failure reports the user's URL, not a synthesised one.
    return original, _run_json(
        YTDLP_CMD + _BASE_ARGS + ["--dump-single-json", original], cancel_check)


def _last_error(tail) -> str:
    """The most useful line of yt-dlp's output for an undiagnosed failure."""
    lines = [line for line in tail if line.strip()]
    for line in reversed(lines):
        if line.startswith("ERROR"):
            return line
    return lines[-1] if lines else "Check the link opens in your browser, then try again."


def _pick_sub_lang(info: dict) -> str | None:
    """Choose the single English track to fetch, manual captions preferred.

    A glob like "en.*" also matches YouTube's auto-*translated* tracks
    (en-es-…, en-pt-…, en-de-…), so yt-dlp would request six or seven files
    back-to-back and trip a 429 — for a pipeline that only ever reads one.
    """
    for source in (info.get("subtitles"), info.get("automatic_captions")):
        if not source:
            continue
        for preferred in ("en", "en-orig"):
            if preferred in source:
                return preferred
        for lang in source:
            if lang.startswith("en-"):
                return lang
    return None


def _download_subtitles(
    url: str, out_template: str, ffmpeg_dir: str, sub_lang: str | None,
    log_cb=None, cancel_check=None,
) -> None:
    """Best-effort subtitle fetch, kept separate from the video download.

    YouTube rate-limits (429) or otherwise fails the subtitle endpoint often
    enough that yt-dlp would abort the whole run over it; a missing SRT just
    means the pipeline transcribes with Whisper instead, so failures here are
    logged and swallowed rather than raised.
    """
    if sub_lang is None:
        if log_cb:
            log_cb("No English subtitles listed; will transcribe with Whisper instead.")
        return

    sub_args = [
        *YTDLP_CMD,
        *_BASE_ARGS,
        "--skip-download",
        "--write-sub",
        "--write-auto-sub",
        "--sub-langs", sub_lang,
        "--convert-subs", "srt",
        # --convert-subs shells out to ffmpeg, which is not assumed on PATH.
        "--ffmpeg-location", ffmpeg_dir,
        "-o", out_template,
        url,
    ]
    try:
        returncode = run_streaming(sub_args, cancel_check=cancel_check)
        if returncode != 0 and log_cb:
            log_cb("Subtitle download failed; will transcribe with Whisper instead.")
    except PipelineCancelled:
        raise
    except Exception:
        if log_cb:
            log_cb("Subtitle download failed; will transcribe with Whisper instead.")


def _find_output(output_dir: str, safe_title: str) -> str | None:
    """Locate the downloaded video (container depends on the merged codecs)."""
    return find_video(output_dir, safe_title)


def find_transcript(output_dir: str) -> list[dict] | None:
    """Look for a downloaded SRT subtitle file and parse it into segments.

    Returns [{start, end, text}] or None if no subtitle file was found.
    """
    srt_files = glob.glob(os.path.join(output_dir, "*.srt"))
    if not srt_files:
        return None
    segments = parse_srt(sorted(srt_files)[0])
    return segments if segments else None


def _run_json(cmd: list[str], cancel_check=None) -> dict:
    try:
        result = run_capture(cmd, timeout=_METADATA_TIMEOUT, cancel_check=cancel_check)
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"yt-dlp metadata fetch timed out after {_METADATA_TIMEOUT}s") from None
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "yt-dlp metadata fetch failed")
    # Find the JSON line (last non-empty line)
    for line in reversed(result.stdout.splitlines()):
        if line.strip().startswith("{"):
            return json.loads(line)
    raise RuntimeError("No JSON output from yt-dlp")

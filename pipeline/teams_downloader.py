"""Download Teams/SharePoint recordings via yt-dlp with browser cookies."""
import glob
import json
import os
import re
import subprocess

from pipeline._paths import FFMPEG, ytdlp_command
from pipeline._util import safe_filename
from pipeline.sharepoint_transcript import fetch_transcript
from pipeline.vtt_parser import parse_srt, parse_vtt

YTDLP_CMD = ytdlp_command()

_TEAMS_PATTERNS = [
    r"teams\.microsoft\.com",
    r"sharepoint\.com",
    r"stream\.microsoft\.com",
    r"microsoftstream\.com",
]

# Edge first: SharePoint's FedAuth/rtFa are persistent cookies there ("keep me
# signed in"), but session-only in Chrome. --cookies-from-browser reads the
# on-disk cookie DB, so Chrome's in-memory value is never available and its
# stale on-disk copy always redirects to login.microsoftonline.com.
_BROWSERS = ["edge", "chrome"]


def is_teams_url(url: str) -> bool:
    return any(re.search(p, url, re.I) for p in _TEAMS_PATTERNS)


class _DownloadProgress:
    """Turn yt-dlp's --newline output into throttled, readable log lines.

    Teams recordings arrive as hundreds of DASH fragments over several minutes,
    so the raw stream is far too chatty to log verbatim and yt-dlp's own "N%" is
    untrustworthy: for fragmented downloads it is a share of an estimated total
    that keeps growing, so it reads 0.2% -> 0.1% -> 0.0% while the estimate
    climbs from 347KiB to 330MiB. "(frag N/462)" is monotonic with a known
    denominator, so prefer it and fall back to "%" only for progressive
    (single-file) downloads.
    """

    _LOG_EVERY_PCT = 5       # one log line per 5% of a stream
    _STAGE_CEILING = 80      # download is 0-80% of the enclosing stage

    _FRAG = re.compile(r"\(frag (\d+)/(\d+)\)")
    _PCT = re.compile(r"(\d+(?:\.\d+)?)%")
    _SPEED = re.compile(r"\bat\s+([\d.]+\s*[KMG]?i?B/s)")
    _ETA = re.compile(r"\bETA\s+(\S+)")
    _DEST = re.compile(r"^\[download\] Destination:\s*(.+)$")

    def __init__(self, log_cb=None, progress_cb=None):
        self._log_cb = log_cb
        self._progress_cb = progress_cb
        self._label = ""
        self._last_bucket = -1
        # yt-dlp restarts the fragment counter for each stream (video, then
        # audio), so clamp to a high-water mark or the bar would rewind.
        self._high_water = 0

    def feed(self, line: str) -> None:
        line = line.rstrip()
        if not line or line.startswith("[debug]"):
            return

        dest = self._DEST.match(line)
        if dest:
            self._start_stream(dest.group(1))
            self._log(line)
            return

        pct = self._PCT.search(line)
        if not pct:
            self._log(line)
            return

        frag = self._FRAG.search(line)
        if frag:
            cur, total = int(frag.group(1)), int(frag.group(2))
            frac = cur / total if total else 0.0
        else:
            frac = float(pct.group(1)) / 100.0

        frac = max(0.0, min(frac, 1.0))
        self._report(frac)

        bucket = int(frac * 100) // self._LOG_EVERY_PCT
        if bucket > self._last_bucket:
            self._last_bucket = bucket
            self._log(self._describe(frac, frag, line))

    # ── internals ─────────────────────────────────────────────────────────────

    def _start_stream(self, dest: str) -> None:
        name = os.path.basename(dest).lower()
        # yt-dlp names DASH streams ...audcopy / ...vcopy
        self._label = "audio" if ("aud" in name or "audio" in name) else "video"
        self._last_bucket = -1

    def _report(self, frac: float) -> None:
        pct = min(int(frac * self._STAGE_CEILING), self._STAGE_CEILING)
        self._high_water = max(self._high_water, pct)
        if self._progress_cb:
            self._progress_cb(self._high_water)

    def _describe(self, frac: float, frag, line: str) -> str:
        what = f"Downloading {self._label}" if self._label else "Downloading"
        parts = [f"{what}: {int(frac * 100)}%"]
        if frag:
            parts.append(f"(frag {frag.group(1)}/{frag.group(2)})")
        speed = self._SPEED.search(line)
        if speed:
            parts.append(f"at {speed.group(1).replace(' ', '')}")
        eta = self._ETA.search(line)
        if eta and eta.group(1) != "Unknown":
            parts.append(f"ETA {eta.group(1)}")
        return " ".join(parts)

    def _log(self, msg: str) -> None:
        if self._log_cb:
            self._log_cb(msg)


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

def _fetch_info(url: str, log_cb=None) -> dict | None:
    for browser in _BROWSERS:
        cmd = [
            *YTDLP_CMD,
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
        *YTDLP_CMD,
        "--cookies-from-browser", browser,
        "--no-playlist",
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
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    reporter = _DownloadProgress(log_cb=log_cb, progress_cb=progress_cb)
    try:
        for line in proc.stdout:
            if cancel_check:
                cancel_check()
            reporter.feed(line)
        proc.wait()
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    return proc.returncode == 0

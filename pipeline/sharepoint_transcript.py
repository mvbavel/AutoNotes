"""Fetch the Teams transcript attached to a SharePoint-hosted recording.

yt-dlp's SharePoint extractor returns only formats and basic metadata — it
never populates `subtitles`, so `--write-sub` is a no-op for these URLs and the
transcript has to be fetched separately.

The Stream web player reads it from the same Vroom API that serves the video,
authenticated by the very cookies yt-dlp already uses for the download:

    1. stream.aspx embeds a `g_fileInfo` JSON blob carrying `.spItemUrl`
       (site path + drive + item) and a `hasTranscripts` flag.
    2. items/{id}?$expand=media/transcripts lists the transcripts, each with a
       pre-signed `temporaryDownloadUrl`.
    3. That URL with `format=json` returns timed entries that already carry
       speaker names — so a recording with a transcript needs neither Whisper
       nor diarization.

Unlike the Graph path in graph_client.py this needs no Azure app registration,
client ID or join URL. Every failure degrades to None so the caller just
transcribes normally.
"""
import json
import re
import urllib.parse

from pipeline.vtt_parser import _merge_consecutive, _ts_to_secs

_TIMEOUT = 60

# Site collections appear as /teams/X (Teams-created sites), /sites/X or
# /personal/X (OneDrive). Omitting `teams` silently breaks every recording made
# from a Teams channel meeting, which is the common case.
_SP_ITEM_RE = re.compile(
    r"^(?P<site>/(?:personal|sites|teams)/[^/]+)"
    r"/_api/v[0-9.]+/drives/(?P<drive>[^/]+)/items/(?P<item>[^/?]+)"
)

_G_FILE_INFO_RE = re.compile(r"g_fileInfo\s*=\s*(\{.*?\});", re.S)

# SharePoint serves a different page to non-browser agents
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0")


def fetch_transcript(url: str, browsers=("edge", "chrome"), log_cb=None) -> list[dict] | None:
    """Return transcript segments for a SharePoint recording, or None.

    Segments match the vtt_parser shape: {start, end, speaker, text}.
    """
    for browser in browsers:
        segments = _try_browser(url, browser, log_cb)
        if segments:
            return segments
    return None


def _try_browser(url: str, browser: str, log_cb=None) -> list[dict] | None:
    try:
        import requests
        from yt_dlp.cookies import extract_cookies_from_browser
    except ImportError as e:
        _log(log_cb, f"Transcript lookup unavailable: {e}")
        return None

    try:
        jar = extract_cookies_from_browser(browser, logger=_QuietLogger())
    except Exception as e:
        _log(log_cb, f"Could not read {browser} cookies for transcript lookup: {e}")
        return None

    session = requests.Session()
    session.headers["User-Agent"] = _UA
    for cookie in jar:
        if "sharepoint.com" in (cookie.domain or ""):
            session.cookies.set_cookie(cookie)

    try:
        return _fetch_with_session(session, url, log_cb)
    except Exception as e:
        _log(log_cb, f"Transcript lookup with {browser} cookies failed: {e}")
        return None


def _fetch_with_session(session, url: str, log_cb=None) -> list[dict] | None:
    resp = session.get(url, timeout=_TIMEOUT)
    if urllib.parse.urlparse(resp.url).hostname == "login.microsoftonline.com":
        _log(log_cb, "Transcript lookup redirected to sign-in — skipping")
        return None
    resp.raise_for_status()

    info = _extract_g_file_info(resp.text)
    if info is None:
        _log(log_cb, "No player metadata on the page — skipping transcript lookup")
        return None

    # Authoritative up-front answer; avoids a pointless round-trip and lets us
    # say "none exists" rather than "lookup failed".
    if info.get("hasTranscripts") is False:
        _log(log_cb, "Recording has no transcript")
        return None

    parts = _parse_sp_item_url(info.get(".spItemUrl") or "")
    if parts is None:
        _log(log_cb, "Could not locate the recording's drive item — skipping transcript")
        return None
    site_path, drive_id, item_id = parts

    origin = f"https://{urllib.parse.urlparse(url).hostname}"
    base = f"{origin}{site_path}/_api/v2.1/drives/{drive_id}/items/{item_id}"
    headers = {"Accept": "application/json"}

    data = _get_json(session, base + "?select=media%2Ftranscripts&%24expand=media%2Ftranscripts",
                     headers)
    if data is None:
        # Older tenants only expose the flat collection
        data = _get_json(session, base + "/media/transcripts", headers)
    if data is None:
        _log(log_cb, "Transcript metadata request failed")
        return None

    transcript = _pick_transcript(data)
    if not transcript or not transcript.get("temporaryDownloadUrl"):
        _log(log_cb, "No transcript attached to this recording")
        return None

    download_url = transcript["temporaryDownloadUrl"]
    download_url += ("&" if "?" in download_url else "?") + "format=json"
    payload = _get_json(session, download_url, {"Accept": "application/json"})
    if payload is None:
        _log(log_cb, "Transcript download failed")
        return None

    segments = _entries_to_segments(payload.get("entries") or [])
    if not segments:
        _log(log_cb, "Transcript was empty")
        return None

    lang = transcript.get("languageTag") or "unknown"
    _log(log_cb, f"Teams transcript found ({len(segments)} segments, {lang}) — "
                 "skipping transcription and diarization")
    return segments


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get_json(session, url: str, headers: dict):
    """GET returning parsed JSON, or None on any HTTP/parse failure."""
    try:
        resp = session.get(url, headers=headers, timeout=_TIMEOUT)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def _extract_g_file_info(html: str) -> dict | None:
    match = _G_FILE_INFO_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1))
    except (ValueError, TypeError):
        return None


def _parse_sp_item_url(sp_item_url: str) -> tuple[str, str, str] | None:
    """Split `.spItemUrl` into (site_path, drive_id, item_id)."""
    if not sp_item_url:
        return None
    match = _SP_ITEM_RE.match(urllib.parse.urlparse(sp_item_url).path)
    if not match:
        return None
    return match.group("site"), match.group("drive"), match.group("item")


def _pick_transcript(data: dict) -> dict | None:
    """Pull the preferred transcript out of any of the three response shapes."""
    if not isinstance(data, dict):
        return None

    transcripts = (data.get("media") or {}).get("transcripts")
    if isinstance(transcripts, list) and transcripts:
        return _preferred(transcripts)

    value = data.get("value")
    if isinstance(value, list) and value:
        return _preferred(value)

    if data.get("temporaryDownloadUrl"):
        return data
    return None


def _preferred(transcripts: list) -> dict | None:
    usable = [t for t in transcripts if isinstance(t, dict)]
    if not usable:
        return None
    return next((t for t in usable if t.get("isDefault")), usable[0])


def _entries_to_segments(entries: list) -> list[dict]:
    """Convert Stream transcript entries to vtt_parser-shaped segments."""
    segments = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        try:
            start = _ts_to_secs(entry["startOffset"])
            end = _ts_to_secs(entry["endOffset"])
        except (KeyError, ValueError, IndexError, TypeError):
            continue
        segments.append({
            "start": start,
            "end": end,
            "speaker": (entry.get("speakerDisplayName") or "Speaker").strip() or "Speaker",
            "text": text,
        })
    segments.sort(key=lambda s: s["start"])
    return _merge_consecutive(segments)


def _log(log_cb, message: str) -> None:
    if log_cb:
        log_cb(message)


class _QuietLogger:
    """yt-dlp cookie extraction is chatty; route it away from the UI log."""

    def debug(self, msg): pass

    def info(self, msg): pass

    def warning(self, msg, once=False): pass

    def error(self, msg): pass

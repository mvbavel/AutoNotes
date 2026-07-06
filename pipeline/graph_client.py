"""Microsoft Graph API client with MSAL-based interactive auth.

First run opens a browser window for the user to sign in.
Tokens are cached in ~/.autonotes_graph_tokens.json and refreshed automatically.
"""
import json
import os
import re

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/common"
SCOPES = [
    "User.Read",
    "Calendars.Read",
    "OnlineMeetings.Read",
    "OnlineMeetingTranscript.Read.All",
]
_CACHE_PATH = os.path.expanduser("~/.autonotes_graph_tokens.json")


# ── Authentication ─────────────────────────────────────────────────────────────

def get_token(client_id: str, log_cb=None) -> str:
    """Return a valid access token, prompting the user to sign in if needed."""
    import msal

    cache = msal.SerializableTokenCache()
    if os.path.exists(_CACHE_PATH):
        os.chmod(_CACHE_PATH, 0o600)  # tighten perms for caches written by older versions
        with open(_CACHE_PATH) as f:
            cache.deserialize(f.read())

    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)

    accounts = app.get_accounts()
    result = None
    if accounts:
        result = app.acquire_token_silent(SCOPES, account=accounts[0])

    if not result:
        if log_cb:
            log_cb("Opening browser for Microsoft sign-in…")
        result = app.acquire_token_interactive(scopes=SCOPES)

    if "access_token" not in result:
        raise RuntimeError(f"Graph auth failed: {result.get('error_description', result)}")

    # Persist updated cache (owner-only: it holds refresh tokens)
    if cache.has_state_changed:
        fd = os.open(_CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(cache.serialize())

    return result["access_token"]


# ── Graph API helpers ──────────────────────────────────────────────────────────

def _get(token: str, path: str, params: dict | None = None):
    import requests
    r = requests.get(
        f"{GRAPH}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def _get_raw(token: str, path: str) -> bytes:
    import requests
    r = requests.get(
        f"{GRAPH}{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/vtt"},
        timeout=60,
    )
    r.raise_for_status()
    return r.content


# ── Meeting lookup ─────────────────────────────────────────────────────────────

def find_meeting(token: str, join_url: str, log_cb=None) -> dict | None:
    """Look up a Teams online meeting by its join URL."""
    try:
        data = _get(token, "/me/onlineMeetings", {
            "$filter": f"joinWebUrl eq '{join_url}'"
        })
        meetings = data.get("value", [])
        return meetings[0] if meetings else None
    except Exception as e:
        if log_cb:
            log_cb(f"Meeting lookup failed: {e}")
        return None


def get_attendees_from_calendar(token: str, join_url: str, log_cb=None) -> list[str]:
    """Find meeting in calendar and return attendee display names."""
    try:
        # Search recent calendar events for one whose online meeting URL matches
        data = _get(token, "/me/events", {
            "$filter": "isOnlineMeeting eq true",
            "$select": "subject,attendees,onlineMeeting",
            "$top": "50",
            "$orderby": "start/dateTime desc",
        })
        for event in data.get("value", []):
            om = event.get("onlineMeeting") or {}
            if om.get("joinUrl", "").rstrip("/") == join_url.rstrip("/"):
                return [
                    a["emailAddress"]["name"]
                    for a in event.get("attendees", [])
                    if a.get("emailAddress", {}).get("name")
                ]
    except Exception as e:
        if log_cb:
            log_cb(f"Calendar attendee lookup failed: {e}")
    return []


def get_transcript_vtt(token: str, meeting_id: str, log_cb=None) -> str | None:
    """Return the VTT content of the first transcript for a meeting, or None."""
    try:
        data = _get(token, f"/me/onlineMeetings/{meeting_id}/transcripts")
        transcripts = data.get("value", [])
        if not transcripts:
            return None
        tid = transcripts[0]["id"]
        raw = _get_raw(token, f"/me/onlineMeetings/{meeting_id}/transcripts/{tid}/content")
        return raw.decode("utf-8", errors="replace")
    except Exception as e:
        if log_cb:
            log_cb(f"Transcript fetch failed: {e}")
        return None


def get_ai_notes(token: str, meeting_id: str, log_cb=None) -> str | None:
    """Return Teams AI-generated meeting recap text, or None if unavailable."""
    try:
        data = _get(token, f"/me/onlineMeetings/{meeting_id}/meetingInfo")
        recap = data.get("recap") or data.get("meetingNotes") or ""
        return recap.strip() or None
    except Exception as e:
        if log_cb:
            log_cb(f"AI notes fetch failed: {e}")
        return None


# ── High-level fetch ───────────────────────────────────────────────────────────

def fetch_meeting_context(client_id: str, join_url: str, log_cb=None) -> dict:
    """Fetch all available meeting metadata from Graph API.

    Returns a dict with keys: title, attendees, transcript_vtt, ai_notes.
    Any unavailable field is None / [].
    """
    ctx: dict = {"title": None, "attendees": [], "transcript_vtt": None, "ai_notes": None}

    try:
        token = get_token(client_id, log_cb=log_cb)
    except Exception as e:
        if log_cb:
            log_cb(f"Graph auth failed — skipping Teams metadata: {e}")
        return ctx

    if log_cb:
        log_cb("Fetching Teams meeting metadata from Microsoft Graph…")

    meeting = find_meeting(token, join_url, log_cb=log_cb)
    if meeting:
        ctx["title"] = meeting.get("subject")
        meeting_id = meeting.get("id")
        if log_cb:
            log_cb(f"Meeting found: {ctx['title']}")

        if log_cb:
            log_cb("Fetching meeting transcript…")
        ctx["transcript_vtt"] = get_transcript_vtt(token, meeting_id, log_cb=log_cb)
        if ctx["transcript_vtt"]:
            if log_cb:
                log_cb("Transcript retrieved from Graph API")

        if log_cb:
            log_cb("Fetching AI notes…")
        ctx["ai_notes"] = get_ai_notes(token, meeting_id, log_cb=log_cb)
        if ctx["ai_notes"]:
            if log_cb:
                log_cb("Teams AI notes retrieved")
    else:
        if log_cb:
            log_cb("Meeting not found via join URL — fetching attendees from calendar only")

    ctx["attendees"] = get_attendees_from_calendar(token, join_url, log_cb=log_cb)
    if ctx["attendees"] and log_cb:
        log_cb(f"Attendees: {', '.join(ctx['attendees'])}")

    return ctx

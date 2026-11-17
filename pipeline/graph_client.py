"""Microsoft Graph API client with MSAL-based interactive auth.

First run opens a browser window for the user to sign in. The token cache
(which holds refresh tokens) is kept in the macOS Keychain and refreshed
automatically.
"""
import os

GRAPH = "https://graph.microsoft.com/v1.0"
AUTHORITY = "https://login.microsoftonline.com/common"
# Transcripts are not fetched here — sharepoint_transcript.py gets them from the
# recording itself with no app registration, so OnlineMeetingTranscript.Read.All
# (which needs tenant-admin consent) is deliberately not requested.
SCOPES = [
    "User.Read",
    "Calendars.Read",
    "OnlineMeetings.Read",
]
# The Teams AI recap is a separate, optional permission: it needs a Microsoft
# 365 Copilot licence and many tenants require admin consent for it. Requested
# on its own after sign-in, so refusing it never costs the attendee list.
RECAP_SCOPES = ["OnlineMeetingAiInsight.Read.All"]
# Where versions before 1.7 kept the cache. Still read (then migrated), and
# still written when the Keychain is unavailable.
_CACHE_PATH = os.path.expanduser("~/.autonotes_graph_tokens.json")
_KEYRING_SERVICE = "AutoNotes"
_KEYRING_NAME = "graph_token_cache"
# Present once the recap permission has been refused, so later runs don't ask
# again; deleted with the rest of the sign-in by clear_token_cache().
_RECAP_DECLINED = os.path.expanduser(
    "~/Library/Application Support/AutoNotes/graph_recap_declined")

# How long to wait for the user to finish signing in. Without a limit, closing
# the browser tab left the pipeline blocked forever with Cancel unable to help.
_SIGN_IN_TIMEOUT = 300


# ── Token cache storage ────────────────────────────────────────────────────────

def _keyring():
    try:
        import keyring
        return keyring
    except ImportError:
        return None


def _load_cache_text() -> str:
    """Serialized MSAL cache: Keychain first, then the legacy 0600 file."""
    kr = _keyring()
    if kr is not None:
        try:
            text = kr.get_password(_KEYRING_SERVICE, _KEYRING_NAME)
            if text:
                return text
        except Exception:
            pass
    try:
        os.chmod(_CACHE_PATH, 0o600)  # tighten perms for caches written by older versions
        with open(_CACHE_PATH, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _save_cache_text(text: str) -> None:
    """Store the cache in the Keychain; fall back to an owner-only file."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(_KEYRING_SERVICE, _KEYRING_NAME, text)
            try:
                os.remove(_CACHE_PATH)   # refresh tokens no longer belong on disk
            except OSError:
                pass
            return
        except Exception:
            pass
    fd = os.open(_CACHE_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def clear_token_cache() -> None:
    """Forget the Microsoft sign-in (Keychain entry and any legacy file)."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.delete_password(_KEYRING_SERVICE, _KEYRING_NAME)
        except Exception:
            pass
    for path in (_CACHE_PATH, _RECAP_DECLINED):
        try:
            os.remove(path)
        except OSError:
            pass


# ── Authentication ─────────────────────────────────────────────────────────────

def _open_app(client_id: str):
    import msal

    cache = msal.SerializableTokenCache()
    stored = _load_cache_text()
    if stored:
        try:
            cache.deserialize(stored)
        except ValueError:
            pass   # corrupt cache: sign in again rather than fail
    app = msal.PublicClientApplication(client_id, authority=AUTHORITY, token_cache=cache)
    return app, cache


def _acquire(client_id: str, scopes: list[str], interactive: bool, log_cb=None) -> dict | None:
    """Token result for `scopes`: silently if possible, else by browser sign-in."""
    app, cache = _open_app(client_id)
    accounts = app.get_accounts()
    result = app.acquire_token_silent(scopes, account=accounts[0]) if accounts else None
    if not result and interactive:
        if log_cb:
            log_cb("Opening browser for Microsoft sign-in "
                   f"(waiting up to {_SIGN_IN_TIMEOUT // 60} minutes)…")
        result = app.acquire_token_interactive(scopes=scopes, timeout=_SIGN_IN_TIMEOUT)
    if cache.has_state_changed:
        _save_cache_text(cache.serialize())
    return result


def get_token(client_id: str, log_cb=None) -> str:
    """Return a valid access token, prompting the user to sign in if needed."""
    result = _acquire(client_id, SCOPES, interactive=True, log_cb=log_cb) or {}
    if "access_token" not in result:
        raise RuntimeError(f"Graph auth failed: {result.get('error_description', result)}")
    return result["access_token"]


def get_recap_token(client_id: str, log_cb=None) -> str | None:
    """Token for the Teams AI recap, or None if it isn't available.

    Asks for consent once; a refusal (no Copilot licence, or the tenant
    requires admin approval) is remembered so later runs don't prompt again.
    """
    declined = os.path.exists(_RECAP_DECLINED)
    result = _acquire(client_id, RECAP_SCOPES, interactive=not declined, log_cb=log_cb) or {}
    if "access_token" in result:
        return result["access_token"]
    if not declined:
        os.makedirs(os.path.dirname(_RECAP_DECLINED), exist_ok=True)
        with open(_RECAP_DECLINED, "w", encoding="utf-8") as f:
            f.write(str(result.get("error_description") or result.get("error") or ""))
        if log_cb:
            log_cb("The Teams AI recap needs a Microsoft 365 Copilot licence and the "
                   "OnlineMeetingAiInsight.Read.All permission, which weren't granted. "
                   "Continuing without it (Help > Delete Saved Data asks again).")
    return None


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


# ── Meeting lookup ─────────────────────────────────────────────────────────────

def _odata_quote(value: str) -> str:
    """Escape a string for use inside an OData single-quoted literal."""
    return value.replace("'", "''")


def find_meeting(token: str, join_url: str, log_cb=None) -> dict | None:
    """Look up a Teams online meeting by its join URL."""
    try:
        data = _get(token, "/me/onlineMeetings", {
            "$filter": f"joinWebUrl eq '{_odata_quote(join_url)}'"
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


def get_ai_notes(token: str, user_id: str, meeting_id: str, log_cb=None) -> str | None:
    """Teams AI recap (meeting notes and action items) as text, or None.

    From Microsoft 365 Copilot's meeting insights API: list the meeting's
    insights, then read the newest. Each holds `meetingNotes` (title, text,
    subpoints) and `actionItems` (text, owner). Insights only exist after the
    meeting has ended, can take hours to appear, and are never produced for
    channel meetings — so "none yet" is normal, not an error.
    """
    base = f"/copilot/users/{user_id}/onlineMeetings/{meeting_id}/aiInsights"
    try:
        listing = _get(token, base).get("value") or []
        if not listing:
            if log_cb:
                log_cb("No Teams AI recap for this meeting (yet)")
            return None
        newest = max(listing, key=lambda i: i.get("createdDateTime") or "")
        insight = _get(token, f"{base}/{newest['id']}")
    except Exception as e:
        if log_cb:
            log_cb(f"Teams AI recap fetch failed: {e}")
        return None
    return _format_insight(insight) or None


def _format_insight(insight: dict) -> str:
    """Render meetingNotes and actionItems as plain text with bullets."""
    lines = []
    for note in insight.get("meetingNotes") or []:
        heading = (note.get("title") or "").strip()
        body = (note.get("text") or "").strip()
        if heading or body:
            lines.append(f"{heading}: {body}" if heading and body else heading or body)
        for sub in note.get("subpoints") or []:
            text = (sub.get("text") or sub.get("title") or "").strip()
            if text:
                lines.append(f"  \u2022 {text}")
    actions = [a for a in insight.get("actionItems") or [] if (a.get("text") or "").strip()]
    if actions:
        lines.append("")
        lines.append("Action items:")
        for item in actions:
            owner = (item.get("ownerDisplayName") or "").strip()
            lines.append(f"  \u2022 {item['text'].strip()}" + (f" ({owner})" if owner else ""))
    return "\n".join(lines).strip()


# ── High-level fetch ───────────────────────────────────────────────────────────

def fetch_meeting_context(client_id: str, join_url: str, log_cb=None) -> dict:
    """Fetch all available meeting metadata from Graph API.

    Returns a dict with keys: title, attendees, ai_notes.
    Any unavailable field is None / [].
    """
    ctx: dict = {"title": None, "attendees": [], "ai_notes": None}

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

        ctx["ai_notes"] = _fetch_recap(client_id, token, meeting_id, log_cb)
        if ctx["ai_notes"] and log_cb:
            log_cb("Teams AI notes retrieved")
    else:
        if log_cb:
            log_cb("Meeting not found via join URL — fetching attendees from calendar only")

    ctx["attendees"] = get_attendees_from_calendar(token, join_url, log_cb=log_cb)
    if ctx["attendees"] and log_cb:
        log_cb(f"Attendees: {', '.join(ctx['attendees'])}")

    return ctx


def _fetch_recap(client_id: str, token: str, meeting_id: str | None,
                 log_cb=None) -> str | None:
    """`token` is the main sign-in token (User.Read, for /me); the recap itself
    is read with its own separately consented token."""
    if not meeting_id:
        return None
    recap_token = get_recap_token(client_id, log_cb=log_cb)
    if recap_token is None:
        return None
    try:
        # The /copilot/users/{id}/... path takes the Entra object ID, not "me"
        user_id = _get(token, "/me").get("id")
    except Exception as e:
        if log_cb:
            log_cb(f"Teams AI recap skipped: couldn't read your user ID ({e})")
        return None
    if log_cb:
        log_cb("Fetching the Teams AI recap…")
    return get_ai_notes(recap_token, user_id, meeting_id, log_cb=log_cb)

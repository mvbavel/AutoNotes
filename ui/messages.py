"""User-facing wording for pipeline failures and timings."""
import re

# (pattern searched in the raw "ExcType: message" string, explanation).
# Order matters: the first match wins, so specific causes precede broad ones.
_KNOWN = [
    (r"credit balance",
     "Your Anthropic account has run out of credit. Add credit in the Anthropic "
     "Console, then try again."),
    (r"authentication_error|invalid x-api-key|AuthenticationError",
     "Claude didn't accept your API key. Check the key under Settings, "
     "then try again."),
    (r"OverloadedError|RateLimitError|overloaded|\b529\b|\b429\b",
     "Claude is busy right now. Try again in a few minutes. Your transcript is "
     "saved, so tick Reuse last transcript to skip transcribing again."),
    (r"CERTIFICATE_VERIFY_FAILED",
     "Couldn't verify a secure connection. If you're on a corporate network or "
     "VPN, check it's connected, then try again."),
    (r"APIConnectionError|Connection error|ConnectionError|timed out",
     "Couldn't reach the server. Check your internet connection or VPN, then "
     "try again."),
    (r"No such file or directory: '[^']*ffmpeg",
     "ffmpeg isn't installed. Install it with brew install ffmpeg, then try again."),
    (r"Audio extraction failed",
     "Couldn't read the audio track from this video. Check the file plays, "
     "then try again."),
    (r"Graph auth failed",
     "Microsoft sign-in for meeting details failed. Clear MS client ID to "
     "continue without attendees and the Teams recap."),
]

_YTDLP_EXIT = re.compile(r"^yt-dlp exited with code \d+\.?\s*")


def explain_error(raw: str, activity: str) -> str:
    """Turn a worker error ("ExcType: message") into what happened and what to do.

    RuntimeErrors raised by the pipeline already carry written-for-users
    messages, so they pass through without the class name; anything else is a
    bug or an unrecognised library error, which names the stage and defers to
    the log rather than surfacing a raw exception string. ``activity`` is
    what was under way, e.g. "downloading the recording".
    """
    for pattern, explanation in _KNOWN:
        if re.search(pattern, raw):
            return explanation

    exc_type, _, message = raw.partition(": ")
    if exc_type == "RuntimeError" and message:
        if _YTDLP_EXIT.match(message):
            hint = _YTDLP_EXIT.sub("", message)
            return ("The download failed. " + hint if hint else
                    "The download failed. Check the link opens in your browser, "
                    "then try again.")
        return message

    return f"Something went wrong while {activity}. The log has the details."


def format_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m {s % 60:02d}s"
    return f"{s // 3600}h {(s % 3600) // 60:02d}m"

"""Resolve Kaltura wrapper pages to URLs yt-dlp's Kaltura extractor accepts.

Some portals (e.g. sapvideo.cfapps.*.hana.ondemand.com/?entry_id=…) serve a
bare page that loads the Kaltura player from JavaScript. yt-dlp's generic
extractor sees no embed there and fails with "Unsupported URL", yet the same
entry downloads fine as kaltura:<partner>:<entry>. The page can name more than
one partner — the player script's partner is rejected ("Invalid entry id")
while the thumbnail's partner owns the content — so callers try each.
"""
import re
from urllib.parse import parse_qs, urlparse

_ENTRY_ID = re.compile(r"^\d_[a-z0-9]{8}$")
_PARTNER = re.compile(r"kaltura\.com/p/(\d+)/(\w+)")
_TIMEOUT = 15


def entry_id(url: str) -> str | None:
    values = parse_qs(urlparse(url).query).get("entry_id") or []
    return values[0] if values and _ENTRY_ID.match(values[0]) else None


def partner_ids(html: str) -> list[str]:
    """Partner IDs referenced in the page, content (thumbnail) partners first."""
    content, other = [], []
    for pid, kind in _PARTNER.findall(html):
        bucket = content if kind == "thumbnail" else other
        if pid not in content and pid not in other:
            bucket.append(pid)
    return content + other


def candidate_urls(url: str) -> list[str]:
    """URLs to try in order; always ends with the original URL."""
    eid = entry_id(url)
    if eid is None:
        return [url]
    try:
        import requests
        resp = requests.get(url, timeout=_TIMEOUT)
        html = resp.text if resp.status_code == 200 else ""
    except Exception:
        # Resolution is best-effort; the original URL still gets its normal try.
        html = ""
    return [f"kaltura:{pid}:{eid}" for pid in partner_ids(html)] + [url]

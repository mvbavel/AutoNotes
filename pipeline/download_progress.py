"""Readable, throttled progress reporting for yt-dlp downloads."""
import os
import re


class DownloadProgress:
    """Turn yt-dlp's --newline output into throttled, readable log lines.

    Used for every download, YouTube included. Recordings arrive as hundreds of
    DASH fragments over several minutes, so the raw stream is far too chatty to
    log verbatim and yt-dlp's own "N%" is untrustworthy: for fragmented downloads it is a share of an estimated total
    that keeps growing, so it reads 0.2% -> 0.1% -> 0.0% while the estimate
    climbs from 347KiB to 330MiB. "(frag N/462)" is monotonic with a known
    denominator, so prefer it and fall back to "%" only for progressive
    (single-file) downloads.
    """

    _LOG_EVERY_PCT = 5       # one log line per 5% of a stream
    _STAGE_CEILING = 80      # default: download is 0-80% of the enclosing stage

    _FRAG = re.compile(r"\(frag (\d+)/(\d+)\)")
    _PCT = re.compile(r"(\d+(?:\.\d+)?)%")
    _SPEED = re.compile(r"\bat\s+([\d.]+\s*[KMG]?i?B/s)")
    _ETA = re.compile(r"\bETA\s+(\S+)")
    _DEST = re.compile(r"^\[download\] Destination:\s*(.+)$")

    def __init__(self, log_cb=None, progress_cb=None, ceiling: int | None = None):
        self._log_cb = log_cb
        self._progress_cb = progress_cb
        self._ceiling = self._STAGE_CEILING if ceiling is None else ceiling
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
        pct = min(int(frac * self._ceiling), self._ceiling)
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

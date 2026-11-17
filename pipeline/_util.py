"""Small helpers shared across pipeline modules."""
import contextlib
import os
import queue
import re
import subprocess
import threading
import time

# Characters XML 1.0 forbids. python-docx (lxml) raises ValueError on any of
# them, and they do turn up: YouTube descriptions carry stray \x0b / \x1f, and
# a model can echo them back from a transcript.
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")

_VIDEO_EXTS = ("mp4", "mkv", "webm", "mov")


class PipelineCancelled(Exception):
    """Raised when the user cancels processing; unwinds the pipeline cleanly."""


def safe_filename(name: str) -> str:
    cleaned = re.sub(r'[\\/*?:"<>|\x00-\x1f]', "_", name or "")[:80].strip()
    # A leading dot hides the file in Finder; an empty stem yields ".docx"
    cleaned = cleaned.lstrip(".").strip()
    return cleaned or "recording"


def clean_text(value) -> str:
    """Text safe to hand to python-docx: a str with XML-illegal characters removed."""
    if value is None:
        return ""
    return _XML_ILLEGAL.sub("", str(value))


def find_video(output_dir: str, stem: str) -> str | None:
    """Locate a downloaded video (the container depends on the merged codecs)."""
    for ext in _VIDEO_EXTS:
        candidate = os.path.join(output_dir, f"{stem}.{ext}")
        if os.path.exists(candidate):
            return candidate
    return None


def ytdlp_template(output_dir: str, stem: str) -> str:
    """yt-dlp -o template writing '<stem>.<ext>' into output_dir.

    A literal % in the title would otherwise be read as a template field.
    """
    return os.path.join(output_dir, stem.replace("%", "%%") + ".%(ext)s")


def private_dir(path: str) -> str:
    """Create a directory only its owner can read.

    Used for anything holding meeting content (transcripts, screenshots, raw
    model output), which should not be readable by other accounts on the Mac.
    """
    os.makedirs(path, mode=0o700, exist_ok=True)
    try:
        os.chmod(path, 0o700)   # tighten directories created by older versions
    except OSError:
        pass
    return path


def write_private(path: str, text: str) -> None:
    """Write a text file readable only by its owner (0600)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)   # O_CREAT's mode is ignored for existing files
    except OSError:
        pass


# ── Child processes ────────────────────────────────────────────────────────────
#
# Every long-running child (yt-dlp, ffmpeg) goes through here so that one place
# owns the three things each call site used to get subtly different: killing
# the child on the way out, noticing a cancel while the child is silent, and
# letting the app kill whatever is still running when it quits.

_active_lock = threading.Lock()
_active: set = set()

_POLL_SECONDS = 0.25


def kill_active_processes() -> None:
    """Kill every child process the pipeline still has running."""
    with _active_lock:
        procs = list(_active)
    for proc in procs:
        _kill(proc)


def _kill(proc) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    except OSError:
        pass


def _track(proc):
    with _active_lock:
        _active.add(proc)
    return proc


def _untrack(proc) -> None:
    with _active_lock:
        _active.discard(proc)


def _pump(stream, sink: queue.Queue) -> None:
    try:
        for line in stream:
            sink.put(line)
    except (OSError, ValueError):
        pass
    finally:
        sink.put(None)


def run_streaming(cmd: list[str], on_line=None, cancel_check=None) -> int:
    """Run a child process, feeding its merged stdout/stderr to on_line.

    Returns the exit code. cancel_check is polled even while the child prints
    nothing — iterating proc.stdout directly only notices a cancel when the
    next line arrives, so a stalled download could not be cancelled at all.
    """
    proc = _track(subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True))
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_pump, args=(proc.stdout, lines), daemon=True).start()
    try:
        while True:
            try:
                line = lines.get(timeout=_POLL_SECONDS)
            except queue.Empty:
                if cancel_check:
                    cancel_check()
                continue
            if line is None:
                break
            if cancel_check:
                cancel_check()
            if on_line:
                on_line(line)
        proc.wait()
        # A cancel kills the child, which ends its output; report that as the
        # cancel it was, not as the child failing
        if cancel_check:
            cancel_check()
    finally:
        _kill(proc)
        _untrack(proc)
        close = getattr(proc.stdout, "close", None)
        if close is not None:
            with contextlib.suppress(OSError):
                close()
    return proc.returncode


def run_capture(cmd: list[str], timeout: float | None = None,
                cancel_check=None) -> subprocess.CompletedProcess:
    """subprocess.run(capture_output=True, text=True) that can be cancelled.

    Raises subprocess.TimeoutExpired after `timeout` seconds, like run().
    """
    proc = _track(subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=_POLL_SECONDS)
                if cancel_check:
                    cancel_check()
                return subprocess.CompletedProcess(cmd, proc.returncode, out, err)
            except subprocess.TimeoutExpired:
                if cancel_check:
                    cancel_check()
                if deadline is not None and time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(cmd, timeout) from None
    finally:
        _kill(proc)
        _untrack(proc)

import os
import subprocess
import time

from pipeline._paths import FFMPEG

# Rough realtime factors: transcription time ≈ audio duration × RTF, measured
# on Apple-Silicon CPU (int8, beam 5, word timestamps). Only used for the
# upfront estimate — the per-decile progress lines report a live ETA that
# self-corrects to the actual machine speed.
_RTF = {
    "tiny": 0.08,
    "base": 0.12,
    "small": 0.4,
    "medium": 1.1,
    "large-v3": 2.6,
}


def extract_audio(video_path: str, temp_dir: str, progress_cb=None, cancel_check=None) -> str:
    """Extract audio from video to a WAV file."""
    audio_path = os.path.join(temp_dir, "audio.wav")
    cmd = [
        FFMPEG, "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-nostats", "-loglevel", "error",
        audio_path
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        while proc.poll() is None:
            if cancel_check:
                cancel_check()
            time.sleep(0.2)
    except Exception:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise
    if proc.returncode != 0:
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise RuntimeError(f"Audio extraction failed: {stderr}")
    if progress_cb:
        progress_cb(100)
    return audio_path


def transcribe(audio_path: str, model_size: str = "medium",
               progress_cb=None, cancel_check=None, log_cb=None) -> list[dict]:
    """
    Transcribe audio using faster-whisper.
    Returns list of {start, end, text} dicts.
    """
    from faster_whisper import WhisperModel

    if progress_cb:
        progress_cb(5)

    cached = _model_is_cached(model_size)
    if not cached and log_cb:
        log_cb(f"Downloading Whisper model '{model_size}' (first use of this size)… "
               "this is a one-time download and can take a while")

    try:
        model = WhisperModel(model_size, device="cpu", compute_type="int8")
    except Exception as e:
        if not cached:
            raise RuntimeError(
                f"Whisper model '{model_size}' download failed — check your internet "
                "connection and retry (the download resumes where it left off)"
            ) from e
        raise

    if progress_cb:
        progress_cb(20)

    segments_iter, info = model.transcribe(
        audio_path,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
    )

    duration = info.duration if info.duration else 1
    if log_cb:
        rtf = _RTF.get(model_size)
        estimate = (f", rough estimate ~{_fmt_secs(duration * rtf)} on CPU "
                    "(live ETA follows)" if rtf else "")
        log_cb(f"Audio duration {_fmt_secs(duration)}{estimate}")

    segments = []
    start_time = time.monotonic()
    next_decile = 10
    for seg in segments_iter:
        if cancel_check:
            cancel_check()
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })
        if progress_cb:
            pct = 20 + int((seg.end / duration) * 75)
            progress_cb(min(pct, 95))

        audio_pct = seg.end / duration * 100
        if log_cb and audio_pct >= next_decile and seg.end > 0:
            elapsed = time.monotonic() - start_time
            remaining = (duration - seg.end) * (elapsed / seg.end)
            log_cb(f"Transcribing: {min(int(audio_pct), 100)}% — "
                   f"{_fmt_secs(elapsed)} elapsed, ~{_fmt_secs(remaining)} remaining")
            while next_decile <= audio_pct:
                next_decile += 10

    if progress_cb:
        progress_cb(100)

    return segments


def _fmt_secs(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


def _model_is_cached(model_size: str) -> bool:
    """True if the Whisper model is fully present in the local HF cache.

    local_files_only returns a snapshot dir even when the download was
    interrupted, so also require the CTranslate2 weights file to exist —
    an aborted download leaves only the small config/tokenizer files.
    """
    try:
        from faster_whisper.utils import download_model
        path = download_model(model_size, local_files_only=True)
        return os.path.isfile(os.path.join(path, "model.bin"))
    except Exception:
        return False

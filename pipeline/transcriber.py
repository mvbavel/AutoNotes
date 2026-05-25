import os
import subprocess

from pipeline._paths import FFMPEG


def extract_audio(video_path: str, temp_dir: str, progress_cb=None) -> str:
    """Extract audio from video to a WAV file."""
    audio_path = os.path.join(temp_dir, "audio.wav")
    cmd = [
        FFMPEG, "-y", "-i", video_path,
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        audio_path
    ]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"Audio extraction failed: {result.stderr.decode()}")
    if progress_cb:
        progress_cb(100)
    return audio_path


def transcribe(audio_path: str, model_size: str = "medium", progress_cb=None) -> list[dict]:
    """
    Transcribe audio using faster-whisper.
    Returns list of {start, end, text} dicts.
    """
    from faster_whisper import WhisperModel

    if progress_cb:
        progress_cb(5)

    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    if progress_cb:
        progress_cb(20)

    segments_iter, info = model.transcribe(
        audio_path,
        beam_size=5,
        word_timestamps=True,
        vad_filter=True,
    )

    segments = []
    duration = info.duration if info.duration else 1
    for seg in segments_iter:
        segments.append({
            "start": seg.start,
            "end": seg.end,
            "text": seg.text.strip(),
        })
        if progress_cb:
            pct = 20 + int((seg.end / duration) * 75)
            progress_cb(min(pct, 95))

    if progress_cb:
        progress_cb(100)

    return segments

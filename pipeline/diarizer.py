def diarize(audio_path: str, segments: list[dict], hf_token,
            progress_cb=None, log_cb=None) -> list[dict]:
    """
    Add speaker labels to transcript segments using pyannote.audio.
    Falls back to a single 'Speaker' label if token is missing or diarization fails.
    Returns list of {start, end, text, speaker} dicts.
    """
    if not hf_token:
        return [dict(s, speaker="Speaker") for s in segments]

    try:
        from pyannote.audio import Pipeline
        import torch

        if progress_cb:
            progress_cb(10)

        pipeline = Pipeline.from_pretrained(
            "pyannote/speaker-diarization-3.1",
            use_auth_token=hf_token,
        )
        pipeline.to(torch.device("cpu"))

        if progress_cb:
            progress_cb(30)

        diarization = pipeline(audio_path)

        if progress_cb:
            progress_cb(80)

        speaker_map = _build_speaker_map(diarization)
        result = []
        for seg in segments:
            mid = (seg["start"] + seg["end"]) / 2
            speaker = _find_speaker(speaker_map, mid)
            result.append(dict(seg, speaker=speaker))

        if progress_cb:
            progress_cb(100)

        return result

    except Exception as e:
        # Non-fatal: fall back to unlabeled, but tell the user why
        if log_cb:
            log_cb(f"Speaker diarization failed ({type(e).__name__}: {e}) — "
                   "continuing without speaker labels")
        return [dict(s, speaker="Speaker") for s in segments]


def _build_speaker_map(diarization) -> list[tuple]:
    """Convert pyannote diarization to list of (start, end, speaker)."""
    labels: dict[str, str] = {}   # per-run mapping so labels never leak across videos
    turns = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        if speaker not in labels:
            idx = len(labels)
            labels[speaker] = (_SPEAKER_NAMES[idx] if idx < len(_SPEAKER_NAMES)
                               else f"Speaker {idx + 1}")
        turns.append((turn.start, turn.end, labels[speaker]))
    return turns


def _find_speaker(speaker_map: list[tuple], t: float) -> str:
    for start, end, speaker in speaker_map:
        if start <= t <= end:
            return speaker
    return "Speaker"


_SPEAKER_NAMES = [
    "Speaker A", "Speaker B", "Speaker C", "Speaker D",
    "Speaker E", "Speaker F", "Speaker G", "Speaker H",
]

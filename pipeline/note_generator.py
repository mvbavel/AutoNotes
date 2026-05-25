import base64
import json
import os


MODEL = "claude-sonnet-4-6"
MAX_SCREENSHOTS = 20


def generate_notes(
    segments: list[dict],
    frames: list[dict],
    video_title: str,
    api_key: str,
    progress_cb=None,
) -> dict:
    """
    Send transcript + candidate screenshots to Claude and get structured notes.
    Returns a notes dict with chapters, key points, and screenshot references.
    """
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    transcript_text = _build_transcript(segments)
    top_frames = frames[:MAX_SCREENSHOTS]

    if progress_cb:
        progress_cb(10)

    content = _build_content(transcript_text, video_title, top_frames, progress_cb)

    if progress_cb:
        progress_cb(60)

    response = client.messages.create(
        model=MODEL,
        max_tokens=8192,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    if progress_cb:
        progress_cb(90)

    raw = response.content[0].text.strip()
    # Strip markdown code fences if present
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]

    try:
        notes = json.loads(raw)
    except json.JSONDecodeError:
        notes = _fallback_notes(video_title, segments)

    if progress_cb:
        progress_cb(100)

    return notes


def _build_transcript(segments: list[dict]) -> str:
    lines = []
    for seg in segments:
        ts = _format_ts(seg["start"])
        speaker = seg.get("speaker", "Speaker")
        lines.append(f"[{ts}] {speaker}: {seg['text']}")
    return "\n".join(lines)


def _format_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def _build_content(transcript: str, title: str, frames: list[dict], progress_cb=None) -> list:
    content = [
        {
            "type": "text",
            "text": (
                f"Video title: {title}\n\n"
                "TRANSCRIPT (timestamps and speakers):\n"
                f"{transcript}\n\n"
                "CANDIDATE SCREENSHOTS (evaluate each for usefulness):\n"
            ),
        }
    ]

    for i, frame in enumerate(frames):
        if progress_cb:
            progress_cb(10 + int((i / len(frames)) * 40))
        img_b64 = _encode_image(frame["path"])
        if img_b64 is None:
            continue
        ts = _format_ts(frame["timestamp"])
        content.append({
            "type": "text",
            "text": f"Screenshot {i + 1} (at {ts}):",
        })
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": img_b64},
        })

    content.append({"type": "text", "text": _USER_INSTRUCTIONS})
    return content


def _encode_image(path: str):
    try:
        with open(path, "rb") as f:
            return base64.standard_b64encode(f.read()).decode()
    except OSError:
        return None


def _fallback_notes(title: str, segments: list[dict]) -> dict:
    text = " ".join(s["text"] for s in segments)
    return {
        "title": title,
        "chapters": [
            {
                "title": "Full Content",
                "start_time": 0,
                "end_time": segments[-1]["end"] if segments else 0,
                "speakers": list({s.get("speaker", "Speaker") for s in segments}),
                "key_points": [{"text": text[:500], "screenshot_idx": None}],
            }
        ],
    }


_SYSTEM_PROMPT = """\
You are an expert note-taker producing professional, structured notes from video content.
You receive a full transcript with speaker labels and timestamps, plus candidate screenshots.
Your notes must be thorough, accurate, and formatted for easy reading.\
"""

_USER_INSTRUCTIONS = """

Based on the transcript and screenshots, produce structured notes in this exact JSON format:

{
  "title": "clear document title (not just the raw video title)",
  "chapters": [
    {
      "title": "concise chapter title",
      "start_time": 0.0,
      "end_time": 0.0,
      "speakers": ["Speaker A"],
      "key_points": [
        {
          "text": "Key insight with **important terms** bolded inline",
          "screenshot_idx": null
        }
      ]
    }
  ]
}

Rules:
- Identify 4-8 natural chapters based on topic transitions in the content
- Each chapter needs 4-8 key points as concise, informative bullets
- Wrap key concepts, jargon, names, and important terms in **double asterisks**
- screenshot_idx: use the screenshot number (1-based integer) if that screenshot clearly
  illustrates the key point (diagrams, code, slides with data). Use null otherwise.
- Each screenshot number should appear at most once across all key points
- Only reference screenshots showing slides, diagrams, code, or meaningful visuals —
  not talking heads or blurry frames
- Preserve accurate speaker attribution per chapter
- start_time and end_time are in seconds (floats)

Return ONLY the JSON — no preamble, no explanation, no markdown fences."""

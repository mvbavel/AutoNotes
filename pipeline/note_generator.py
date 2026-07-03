import base64
import json
import os
import time


MODEL = "claude-sonnet-4-6"
MAX_SCREENSHOTS = 30
# Estimated input tokens threshold above which we chunk (conservative for 30K TPM Tier 1)
_TOKEN_THRESHOLD = 20000
# chars / 4 ≈ tokens; 1000px image ≈ 750 tokens
_CHARS_PER_TOKEN = 4
_TOKENS_PER_IMAGE = 750
MAX_RETRIES = 3
_RETRY_BASE_WAIT = 60  # seconds; doubles each attempt


def generate_notes(
    segments: list[dict],
    frames: list[dict],
    video_title: str,
    api_key: str,
    progress_cb=None,
    description: str = "",
    yt_chapters: list[dict] | None = None,
    attendees: list[str] | None = None,
    ai_notes: str | None = None,
) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    transcript_text = _build_transcript(segments)
    top_frames = frames[:MAX_SCREENSHOTS]
    # Global 1-based index used in prompts and returned as screenshot_idx;
    # must match the docx writer's enumeration of the frames list.
    for i, frame in enumerate(top_frames):
        frame["idx"] = i + 1

    if progress_cb:
        progress_cb(10)

    estimated_tokens = len(transcript_text) // _CHARS_PER_TOKEN + len(top_frames) * _TOKENS_PER_IMAGE

    if estimated_tokens > _TOKEN_THRESHOLD:
        notes = _generate_chunked(
            client, segments, top_frames, video_title, progress_cb,
            description=description, yt_chapters=yt_chapters,
            attendees=attendees, ai_notes=ai_notes,
        )
    else:
        content = _build_content(
            transcript_text, video_title, top_frames, progress_cb,
            description=description, yt_chapters=yt_chapters,
            attendees=attendees, ai_notes=ai_notes,
        )
        if progress_cb:
            progress_cb(60)
        response = _call_with_retry(client, content)
        if progress_cb:
            progress_cb(90)
        notes = _parse_response(response, video_title, segments)

    if progress_cb:
        progress_cb(100)

    return notes


def _generate_chunked(
    client, segments, frames, video_title, progress_cb=None,
    description="", yt_chapters=None, attendees=None, ai_notes=None,
):
    """Split transcript and frames into two halves, call Claude once per half, merge."""
    if not segments:
        return _fallback_notes(video_title, segments)

    midpoint = segments[len(segments) // 2]["start"]

    chunk1_segs = [s for s in segments if s["start"] < midpoint]
    chunk2_segs = [s for s in segments if s["start"] >= midpoint]

    # Distribute frames between chunks (up to half of MAX_SCREENSHOTS each).
    # Frames keep their global "idx", so no index remapping is needed and
    # screenshot_idx always refers to the same enumeration the docx writer uses.
    per_chunk = MAX_SCREENSHOTS // 2
    chunk1_frames = [f for f in frames if f["timestamp"] < midpoint][:per_chunk]
    chunk2_frames = [f for f in frames if f["timestamp"] >= midpoint][:per_chunk]

    all_chapters = []
    title = video_title

    for i, (chunk_segs, chunk_frames) in enumerate([
        (chunk1_segs, chunk1_frames),
        (chunk2_segs, chunk2_frames),
    ]):
        if not chunk_segs:
            continue

        text = _build_transcript(chunk_segs)
        # Only include metadata in the first chunk to avoid redundancy
        content = _build_content(
            text, video_title, chunk_frames,
            description=description if i == 0 else "",
            yt_chapters=yt_chapters if i == 0 else None,
            attendees=attendees if i == 0 else None,
            ai_notes=ai_notes if i == 0 else None,
        )

        if i > 0:
            # Wait between chunks so we don't slam the TPM window
            time.sleep(30)

        if progress_cb:
            progress_cb(10 + i * 40)

        response = _call_with_retry(client, content)
        chunk_notes = _parse_response(response, video_title, chunk_segs)

        if i == 0:
            title = chunk_notes.get("title", video_title)

        all_chapters.extend(chunk_notes.get("chapters", []))

        if progress_cb:
            progress_cb(50 + i * 40)

    if not all_chapters:
        return _fallback_notes(video_title, segments)

    return {"title": title, "chapters": all_chapters}


def _call_with_retry(client, content):
    import anthropic

    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return client.messages.create(
                model=MODEL,
                max_tokens=8192,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.RateLimitError as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                wait = _RETRY_BASE_WAIT * (2 ** attempt)
                time.sleep(wait)

    raise last_exc


def _parse_response(response, video_title: str, segments: list[dict]) -> dict:
    raw = response.content[0].text.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return _fallback_notes(video_title, segments)


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


def _build_content(
    transcript: str,
    title: str,
    frames: list[dict],
    progress_cb=None,
    description: str = "",
    yt_chapters: list[dict] | None = None,
    attendees: list[str] | None = None,
    ai_notes: str | None = None,
) -> list:
    header = f"Video title: {title}\n"

    if attendees:
        header += f"\nATTENDEES: {', '.join(attendees)}\n"

    if description:
        trimmed = description[:800].rstrip()
        if len(description) > 800:
            trimmed += "…"
        header += f"\nDESCRIPTION:\n{trimmed}\n"

    if yt_chapters:
        chapter_lines = "\n".join(
            f"  {_format_ts(c['start_time'])} – {c.get('title', 'Chapter')}"
            for c in yt_chapters
        )
        header += f"\nYOUTUBE CHAPTERS (use these as natural section breaks):\n{chapter_lines}\n"

    if ai_notes:
        trimmed_notes = ai_notes[:1500].rstrip()
        if len(ai_notes) > 1500:
            trimmed_notes += "…"
        header += f"\nTEAMS AI NOTES (use as additional context, not as a replacement for your own analysis):\n{trimmed_notes}\n"

    header += "\nTRANSCRIPT (timestamps and speakers):\n" + transcript + "\n\nCANDIDATE SCREENSHOTS (evaluate each for usefulness):\n"

    content = [{"type": "text", "text": header}]

    for i, frame in enumerate(frames):
        if progress_cb:
            progress_cb(10 + int((i / len(frames)) * 40))
        img_b64 = _encode_image(frame.get("api_path", frame["path"]))
        if img_b64 is None:
            continue
        ts = _format_ts(frame["timestamp"])
        idx = frame.get("idx", i + 1)
        content.append({"type": "text", "text": f"Screenshot {idx} (at {ts}):"})
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
- If YouTube chapters are provided, use them as chapter boundaries (title and timing); otherwise identify 4-8 natural chapters based on topic transitions
- Each chapter needs 4-8 key points as concise, informative bullets
- Wrap key concepts, jargon, names, and important terms in **double asterisks**
- screenshot_idx: the screenshot number shown above each image (integer), or null.
  Screenshots are highly valuable to the reader — attach one to EVERY key point it
  illustrates or accompanies. Valuable screenshots include ANY screen content shown
  in the presentation: slides, diagrams, charts, code editors, terminals, browser
  windows, dashboards, application demos, and documents.
- Aim to reference MOST of the provided screenshots. The screenshots were pre-filtered
  to likely-useful frames, so err on the side of including them. Only skip a screenshot
  if it is a talking head, blank, blurry, or a near-duplicate of one already used.
- Each screenshot number may appear at most once across all key points; if needed,
  add a key point summarizing what a screenshot shows so it can be included
- Preserve accurate speaker attribution per chapter
- start_time and end_time are in seconds (floats)

Return ONLY the JSON — no preamble, no explanation, no markdown fences."""

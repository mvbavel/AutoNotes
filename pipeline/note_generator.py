import base64
import json
import os
import re
import time


MODEL = os.environ.get("AUTONOTES_MODEL", "claude-sonnet-5")
MAX_SCREENSHOTS = 30
# Estimated TRANSCRIPT tokens above which we split into two calls. Images are
# deliberately excluded from this estimate: the ~22.5K tokens of a full image
# set would otherwise trigger chunking on every real video, and chunking
# degrades results (frames split unevenly, metadata only in chunk 1).
_TOKEN_THRESHOLD = 20000
_CHARS_PER_TOKEN = 4  # chars / 4 ≈ tokens
MAX_RETRIES = 3
_RETRY_BASE_WAIT = 60  # seconds; doubles each attempt
# Smallest plausible content_box area (fraction of the frame); anything
# smaller is treated as a bad crop and the frame embeds uncropped
_MIN_BOX_AREA = 0.25

# Raw model responses land here for post-hoc debugging (same dir the worker
# uses for its log and frame copies)
_DEBUG_DIR = os.path.expanduser("~/Library/Logs/AutoNotes")


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
    cancel_check=None,
    log_cb=None,
) -> dict:
    import anthropic

    client = anthropic.Anthropic(api_key=api_key)

    transcript_text = _build_transcript(segments)
    top_frames = frames[:MAX_SCREENSHOTS]
    # Global 1-based index used in prompts and returned as screenshot_idx;
    # must match the docx writer's enumeration of the frames list.
    for i, frame in enumerate(top_frames):
        frame["idx"] = i + 1
    valid_idx = {frame["idx"] for frame in top_frames}

    if progress_cb:
        progress_cb(10)

    estimated_tokens = len(transcript_text) // _CHARS_PER_TOKEN

    if estimated_tokens > _TOKEN_THRESHOLD:
        notes = _generate_chunked(
            client, segments, top_frames, video_title, progress_cb,
            description=description, yt_chapters=yt_chapters,
            attendees=attendees, ai_notes=ai_notes,
            cancel_check=cancel_check, log_cb=log_cb, valid_idx=valid_idx,
        )
    else:
        content = _build_content(
            transcript_text, video_title, top_frames, progress_cb,
            description=description, yt_chapters=yt_chapters,
            attendees=attendees, ai_notes=ai_notes,
        )
        if progress_cb:
            progress_cb(60)
        response = _call_with_retry(client, content, cancel_check=cancel_check)
        if progress_cb:
            progress_cb(90)
        notes = _parse_response(response, video_title, segments,
                                valid_idx=valid_idx, log_cb=log_cb, tag="single")

    if progress_cb:
        progress_cb(100)

    return notes


def _generate_chunked(
    client, segments, frames, video_title, progress_cb=None,
    description="", yt_chapters=None, attendees=None, ai_notes=None,
    cancel_check=None, log_cb=None, valid_idx=None,
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
    all_boxes: dict = {}
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
            _interruptible_sleep(30, cancel_check)

        if progress_cb:
            progress_cb(10 + i * 40)

        response = _call_with_retry(client, content, cancel_check=cancel_check)
        chunk_notes = _parse_response(response, video_title, chunk_segs,
                                      valid_idx=valid_idx, log_cb=log_cb,
                                      tag=f"chunk{i + 1}")

        if i == 0:
            title = chunk_notes.get("title", video_title)

        all_chapters.extend(chunk_notes.get("chapters", []))
        all_boxes.update(chunk_notes.get("screenshot_boxes", {}))

        if progress_cb:
            progress_cb(50 + i * 40)

    if not all_chapters:
        return _fallback_notes(video_title, segments)

    return {"title": title, "chapters": all_chapters, "screenshot_boxes": all_boxes}


def _call_with_retry(client, content, cancel_check=None):
    import anthropic

    last_exc = None
    for attempt in range(MAX_RETRIES):
        if cancel_check:
            cancel_check()
        try:
            return client.messages.create(
                model=MODEL,
                # thinking output shares this budget on claude-sonnet-5, so
                # leave headroom beyond the ~2-4K tokens the JSON itself needs
                max_tokens=16000,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": content}],
            )
        except (anthropic.RateLimitError,
                anthropic.InternalServerError,
                anthropic.APIConnectionError) as exc:
            last_exc = exc
            if attempt < MAX_RETRIES - 1:
                wait = _RETRY_BASE_WAIT * (2 ** attempt)
                _interruptible_sleep(wait, cancel_check)

    raise last_exc


def _interruptible_sleep(seconds: float, cancel_check=None):
    """Sleep in 1s slices so a cancel request isn't stuck behind a long wait."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if cancel_check:
            cancel_check()
        time.sleep(min(1.0, deadline - time.monotonic()))


def _parse_response(response, video_title: str, segments: list[dict],
                    valid_idx: set | None = None, log_cb=None,
                    tag: str = "response") -> dict:
    # claude-sonnet-5 runs adaptive thinking by default, so the text answer
    # is not necessarily the first content block
    raw = next((b.text for b in response.content if b.type == "text"), None)
    if raw is None:
        if log_cb:
            log_cb(f"Claude response ({tag}) contained no text block — using fallback notes")
        return _fallback_notes(video_title, segments)
    raw = raw.strip()
    _dump_debug(raw, tag)
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1]
        raw = raw.rsplit("```", 1)[0]
    try:
        notes = json.loads(raw)
    except json.JSONDecodeError:
        if log_cb:
            log_cb(f"Claude response ({tag}) was not valid JSON — using fallback "
                   f"notes (raw saved to {_DEBUG_DIR})")
        return _fallback_notes(video_title, segments)
    notes = _normalize_screenshot_refs(notes, valid_idx, log_cb=log_cb, tag=tag)
    return _normalize_content_boxes(notes, valid_idx)


def _normalize_screenshot_refs(notes: dict, valid_idx: set | None,
                               log_cb=None, tag: str = "response") -> dict:
    """Coerce screenshot_idx values to int and drop ones that don't reference
    a provided screenshot, so bad references fail loudly instead of silently
    producing a document with missing images."""
    if valid_idx is None:
        return notes
    dropped = []
    for chapter in notes.get("chapters", []):
        for kp in chapter.get("key_points", []):
            if not isinstance(kp, dict) or kp.get("screenshot_idx") is None:
                continue
            idx = _coerce_idx(kp["screenshot_idx"])
            if idx not in valid_idx:
                dropped.append(kp["screenshot_idx"])
                idx = None
            kp["screenshot_idx"] = idx
    if dropped and log_cb:
        log_cb(f"Dropped {len(dropped)} invalid screenshot reference(s) in {tag}: "
               f"{dropped[:10]}{'…' if len(dropped) > 10 else ''}")
    return notes


def _normalize_content_boxes(notes: dict, valid_idx: set | None) -> dict:
    """Validate the per-screenshot "screenshots" map from Claude into
    notes["screenshot_boxes"]: {int idx: [x0, y0, x1, y1] fractions}.
    Invalid, implausible, or missing boxes are simply absent — the docx
    writer then embeds that frame uncropped, so a bad box can only ever
    make a crop looser, never destroy an image."""
    raw = notes.pop("screenshots", None)
    boxes = notes.setdefault("screenshot_boxes", {})
    if not isinstance(raw, dict):
        return notes
    for key, val in raw.items():
        idx = _coerce_idx(key)
        if idx is None or (valid_idx is not None and idx not in valid_idx):
            continue
        box = val.get("content_box") if isinstance(val, dict) else val
        box = _valid_box(box)
        if box:
            boxes[idx] = box
    return notes


def _valid_box(box):
    """Clamp and sanity-check a fractional [x0, y0, x1, y1] crop box."""
    if not isinstance(box, (list, tuple)) or len(box) != 4:
        return None
    try:
        x0, y0, x1, y1 = (min(1.0, max(0.0, float(v))) for v in box)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0 or (x1 - x0) * (y1 - y0) < _MIN_BOX_AREA:
        return None
    return [x0, y0, x1, y1]


def _coerce_idx(value):
    """Best-effort conversion of screenshot_idx to int — models occasionally
    return "5", "Screenshot 5", or 5.0 instead of a bare integer."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    m = re.search(r"\d+", str(value))
    return int(m.group()) if m else None


def _dump_debug(text: str, tag: str):
    """Save the raw model output so citation problems can be diagnosed post-hoc."""
    try:
        os.makedirs(_DEBUG_DIR, exist_ok=True)
        path = os.path.join(_DEBUG_DIR, f"last_claude_{tag}.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError:
        pass


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
  ],
  "screenshots": {
    "1": {"content_box": [0.0, 0.05, 0.87, 0.94]}
  }
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
- For EVERY screenshot you reference, add an entry under "screenshots" whose
  content_box is [left, top, right, bottom] as fractions (0.0–1.0) of the image,
  tightly enclosing ONLY the meaningful display content — the shared slide, or the
  page/app area of a shared window. EXCLUDE webcam/participant video tiles, meeting
  captions and name labels, browser tab bars / address bars / bookmark bars, OS menu
  bars, docks/taskbars, and black letterbox margins. If the entire image is content,
  use [0.0, 0.0, 1.0, 1.0]
- If a screenshot shows only meeting participants (webcam gallery) or contains no
  meaningful screen content, do not reference it in any key point at all
- Preserve accurate speaker attribution per chapter
- start_time and end_time are in seconds (floats)

Return ONLY the JSON — no preamble, no explanation, no markdown fences."""

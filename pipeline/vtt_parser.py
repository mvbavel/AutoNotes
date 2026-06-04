"""Parse WebVTT transcript files (including Teams-format with speaker tags)."""
import re


def parse_vtt(path: str) -> list[dict]:
    """Return [{start, end, speaker, text}] from a VTT file.

    Handles both plain VTT and Teams-flavoured VTT where speakers are encoded as:
      <v Speaker Name>text</v>   or   <v 0>text</v>  (index into NOTE speaker-list)
    """
    with open(path, encoding="utf-8") as f:
        content = f.read()

    speaker_map = _extract_speaker_map(content)
    segments = []

    for block in re.split(r"\n{2,}", content.strip()):
        lines = block.strip().splitlines()
        ts_line = next((l for l in lines if "-->" in l), None)
        if ts_line is None:
            continue

        m = re.match(
            r"(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})",
            ts_line,
        )
        if not m:
            continue

        start = _vtt_ts(m.group(1))
        end = _vtt_ts(m.group(2))

        # Join remaining lines after the timestamp as the cue text
        cue_lines = [l for l in lines if l != ts_line and not l.strip().isdigit()]
        raw_text = " ".join(cue_lines).strip()

        speaker, text = _extract_speaker(raw_text, speaker_map)
        text = re.sub(r"<[^>]+>", "", text)   # strip remaining HTML tags
        text = re.sub(r"\s+", " ", text).strip()

        if text:
            segments.append({"start": start, "end": end, "speaker": speaker, "text": text})

    return _merge_consecutive(segments)


def _extract_speaker_map(content: str) -> dict:
    """Parse Teams NOTE speaker-list block: {"speakersRaw":[{"id":0,"name":"..."}]}"""
    import json
    m = re.search(r'NOTE speaker-list\s*(\{.*?\})', content, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
        return {str(s["id"]): s["name"] for s in data.get("speakersRaw", [])}
    except Exception:
        return {}


def _extract_speaker(text: str, speaker_map: dict) -> tuple[str, str]:
    """Pull speaker from <v Name> or <v 0> tag, return (speaker, clean_text)."""
    m = re.match(r"<v ([^>]+)>(.*)", text, re.S)
    if not m:
        return "Speaker", text
    raw_id = m.group(1).strip()
    body = m.group(2).replace("</v>", "").strip()
    name = speaker_map.get(raw_id, raw_id)
    return name, body


def _merge_consecutive(segments: list[dict]) -> list[dict]:
    """Merge back-to-back cues from the same speaker into one segment."""
    merged = []
    for seg in segments:
        if merged and merged[-1]["speaker"] == seg["speaker"] and seg["start"] - merged[-1]["end"] < 1.5:
            merged[-1]["end"] = seg["end"]
            merged[-1]["text"] += " " + seg["text"]
        else:
            merged.append(dict(seg))
    return merged


def _vtt_ts(ts: str) -> float:
    ts = ts.replace(",", ".")
    parts = ts.split(":")
    h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
    return h * 3600 + m * 60 + s

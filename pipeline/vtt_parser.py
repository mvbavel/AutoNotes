"""Parse subtitle transcripts: WebVTT (including Teams-format with speaker tags) and SRT."""
import json
import re

# Hours are optional in WebVTT ("01:02.000" is a valid cue time); SRT always has them.
_TS = r"(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3}"
_CUE_TIMES = re.compile(rf"({_TS})\s*-->\s*({_TS})")

# A caption file is treated as "rolling" (see _collapse_rolling) once this share
# of its cues is repeated at the start of the cue that follows.
_ROLLING_SHARE = 0.25

# Longest span a merged segment may cover. Each segment carries one timestamp,
# so unbounded merging turned a presenter's monologue into a single 9-minute
# block — too coarse to line screenshots up with speech or time chapters.
MAX_MERGED_SECONDS = 45


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

        m = _CUE_TIMES.match(ts_line.strip())
        if not m:
            continue

        start = ts_to_secs(m.group(1))
        end = ts_to_secs(m.group(2))

        # Join remaining lines after the timestamp as the cue text
        cue_lines = [l for l in lines if l != ts_line and not l.strip().isdigit()]
        raw_text = " ".join(cue_lines).strip()

        speaker, text = _extract_speaker(raw_text, speaker_map)
        text = re.sub(r"<[^>]+>", "", text)   # strip remaining HTML tags
        text = re.sub(r"\s+", " ", text).strip()

        if text:
            segments.append({"start": start, "end": end, "speaker": speaker, "text": text})

    return merge_consecutive(segments)


def parse_srt(path: str) -> list[dict]:
    """Parse an SRT file into [{start, end, text}] segments."""
    with open(path, encoding="utf-8") as f:
        content = f.read()

    segments = []
    for block in re.split(r"\n\n+", content.strip()):
        lines = block.strip().splitlines()
        if len(lines) < 3:
            continue
        ts_match = _CUE_TIMES.match(lines[1].strip())
        if not ts_match:
            continue
        start = ts_to_secs(ts_match.group(1))
        end = ts_to_secs(ts_match.group(2))
        text = " ".join(lines[2:])
        text = re.sub(r"<[^>]+>", "", text)  # strip any inline HTML tags
        text = re.sub(r"\s+", " ", text).strip()
        if text:
            segments.append({"start": start, "end": end, "text": text})

    return _collapse_rolling(segments)


def _collapse_rolling(segments: list[dict]) -> list[dict]:
    """Undo YouTube's rolling auto-captions, which repeat every line three times.

    Auto-generated captions scroll: each cue shows the previous line plus a new
    one, with a 10 ms cue in between that repeats the line again. Converted to
    SRT that reads

        23.51  "intelligence. It's about having access"
        23.52  "intelligence. It's about having access to the best intelligence"
        26.23  "to the best intelligence"

    so a 108-minute talk parsed to 54,000 words — 502 a minute — tripling what
    is sent to Claude and pushing ordinary videos into chunked mode. Each cue is
    reduced to the words it adds to the cue before it.

    Only applied when the file actually rolls: hand-written captions can
    legitimately start a cue with the words the last one ended on.
    """
    if len(segments) < 4:
        return segments
    words = [s["text"].split() for s in segments]
    repeats = sum(1 for a, b in zip(words, words[1:], strict=False) if a and b[:len(a)] == a)
    if repeats / (len(segments) - 1) < _ROLLING_SHARE:
        return segments

    collapsed, previous = [], []
    for seg, current in zip(segments, words, strict=True):
        overlap = next(
            (k for k in range(min(len(previous), len(current)), 0, -1)
             if previous[-k:] == current[:k]),
            0,
        )
        fresh = current[overlap:]
        if fresh:
            collapsed.append(dict(seg, text=" ".join(fresh)))
        previous = current
    return collapsed


def _extract_speaker_map(content: str) -> dict:
    """Parse Teams NOTE speaker-list block: {"speakersRaw":[{"id":0,"name":"..."}]}"""
    idx = content.find("NOTE speaker-list")
    if idx == -1:
        return {}
    brace = content.find("{", idx)
    if brace == -1:
        return {}
    try:
        # raw_decode handles the nested braces a regex can't balance
        data, _ = json.JSONDecoder().raw_decode(content[brace:])
        return {str(s["id"]): s["name"] for s in data.get("speakersRaw", [])}
    except (ValueError, KeyError, TypeError):
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


def merge_consecutive(segments: list[dict]) -> list[dict]:
    """Merge back-to-back cues from the same speaker into one segment."""
    merged = []
    for seg in segments:
        if (merged and merged[-1]["speaker"] == seg["speaker"]
                and seg["start"] - merged[-1]["end"] < 1.5
                and seg["end"] - merged[-1]["start"] <= MAX_MERGED_SECONDS):
            merged[-1]["end"] = seg["end"]
            merged[-1]["text"] += " " + seg["text"]
        else:
            merged.append(dict(seg))
    return merged


def ts_to_secs(ts: str) -> float:
    """'HH:MM:SS.mmm', 'MM:SS.mmm' (comma or dot) -> seconds."""
    parts = ts.replace(",", ".").split(":")
    seconds = float(parts[-1])
    minutes = int(parts[-2])
    hours = int(parts[-3]) if len(parts) > 2 else 0
    return hours * 3600 + minutes * 60 + seconds


# Kept for callers and tests written against the private names
_merge_consecutive = merge_consecutive
_ts_to_secs = ts_to_secs

"""YouTube's rolling auto-captions, and caption timestamp formats.

Regression: a 108-minute talk's auto-captions parsed to 54,000 words — 502 a
minute against ~165 actually spoken — because every line appears in three
consecutive cues. That tripled what was sent to Claude and pushed ordinary
videos into chunked mode.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.vtt_parser import parse_srt, parse_vtt, ts_to_secs

# The shape `yt-dlp --convert-subs srt` produces from YouTube auto-captions
ROLLING_SRT = """1
00:00:23,510 --> 00:00:23,520
intelligence. It's about having access

2
00:00:23,520 --> 00:00:26,230
intelligence. It's about having access to the best intelligence in the world

3
00:00:26,230 --> 00:00:26,240
to the best intelligence in the world

4
00:00:26,240 --> 00:00:27,910
to the best intelligence in the world and choosing the right one for

5
00:00:27,910 --> 00:00:27,920
and choosing the right one for

6
00:00:27,920 --> 00:00:31,030
and choosing the right one for whatever you are trying to do
"""

# Hand-made captions: cue 3 legitimately repeats the end of cue 2
NORMAL_SRT = """1
00:00:01,000 --> 00:00:03,000
Welcome everyone.

2
00:00:03,000 --> 00:00:05,000
Today we look at the plan.

3
00:00:05,000 --> 00:00:07,000
The plan has three parts.

4
00:00:07,000 --> 00:00:09,000
First, the budget.

5
00:00:09,000 --> 00:00:11,000
Second, hiring.
"""


def _write(text: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class TestRollingCaptions(unittest.TestCase):
    def test_each_word_appears_once(self):
        path = _write(ROLLING_SRT, ".srt")
        try:
            text = " ".join(s["text"] for s in parse_srt(path))
        finally:
            os.unlink(path)
        self.assertEqual(
            text,
            "intelligence. It's about having access to the best intelligence in the "
            "world and choosing the right one for whatever you are trying to do")

    def test_timestamps_follow_the_new_words(self):
        path = _write(ROLLING_SRT, ".srt")
        try:
            segments = parse_srt(path)
        finally:
            os.unlink(path)
        self.assertEqual(segments[0]["start"], 23.51)
        self.assertTrue(all(a["start"] <= b["start"] for a, b in zip(segments, segments[1:])))

    def test_ordinary_captions_are_untouched(self):
        path = _write(NORMAL_SRT, ".srt")
        try:
            segments = parse_srt(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(segments), 5)
        self.assertEqual(segments[2]["text"], "The plan has three parts.")


class TestTimestamps(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(ts_to_secs("01:02:03.500"), 3723.5)
        self.assertEqual(ts_to_secs("01:02:03,500"), 3723.5)
        self.assertEqual(ts_to_secs("02:03.250"), 123.25)

    def test_vtt_cue_without_hours(self):
        # "mm:ss.ttt" is valid WebVTT; such cues used to be dropped silently
        path = _write("WEBVTT\n\n00:01.000 --> 00:04.000\nHello there\n\n"
                      "00:00:10.000 --> 00:00:11.000\nSecond\n", ".vtt")
        try:
            texts = [s["text"] for s in parse_vtt(path)]
        finally:
            os.unlink(path)
        self.assertEqual(texts, ["Hello there", "Second"])


if __name__ == "__main__":
    unittest.main()

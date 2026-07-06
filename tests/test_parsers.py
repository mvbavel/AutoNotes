"""Tests for the pure-function transcript parsers and helpers.

Run with:  python3 -m unittest discover tests
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline._util import safe_filename
from pipeline.vtt_parser import parse_srt, parse_vtt, _extract_speaker_map
from pipeline.note_generator import _format_ts


def _write_temp(content: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


class TestVttParser(unittest.TestCase):
    def test_teams_indexed_speakers(self):
        """Regression: <v 0> indexed speakers must resolve via NOTE speaker-list."""
        vtt = (
            "WEBVTT\n"
            "\n"
            'NOTE speaker-list {"speakersRaw":[{"id":0,"name":"Alice Smith"},'
            '{"id":1,"name":"Bob Jones"}]}\n'
            "\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "<v 0>Hello everyone</v>\n"
            "\n"
            "00:00:04.000 --> 00:00:06.000\n"
            "<v 1>Hi Alice</v>\n"
        )
        path = _write_temp(vtt, ".vtt")
        try:
            segments = parse_vtt(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["speaker"], "Alice Smith")
        self.assertEqual(segments[1]["speaker"], "Bob Jones")
        self.assertEqual(segments[0]["text"], "Hello everyone")

    def test_named_speakers_without_note(self):
        vtt = (
            "WEBVTT\n"
            "\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "<v Carol Danvers>First point</v>\n"
        )
        path = _write_temp(vtt, ".vtt")
        try:
            segments = parse_vtt(path)
        finally:
            os.unlink(path)
        self.assertEqual(segments[0]["speaker"], "Carol Danvers")

    def test_consecutive_same_speaker_merged(self):
        vtt = (
            "WEBVTT\n"
            "\n"
            "00:00:01.000 --> 00:00:03.000\n"
            "<v A>Part one.</v>\n"
            "\n"
            "00:00:03.500 --> 00:00:05.000\n"
            "<v A>Part two.</v>\n"
        )
        path = _write_temp(vtt, ".vtt")
        try:
            segments = parse_vtt(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["text"], "Part one. Part two.")
        self.assertEqual(segments[0]["end"], 5.0)

    def test_speaker_map_with_trailing_content(self):
        content = 'NOTE speaker-list {"speakersRaw":[{"id":0,"name":"X"}]}\n\nmore'
        self.assertEqual(_extract_speaker_map(content), {"0": "X"})

    def test_speaker_map_absent(self):
        self.assertEqual(_extract_speaker_map("WEBVTT\n\n"), {})


class TestSrtParser(unittest.TestCase):
    def test_basic_srt(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:04,500\n"
            "Hello <i>world</i>\n"
            "\n"
            "2\n"
            "01:00:05,000 --> 01:00:07,000\n"
            "Second line\n"
        )
        path = _write_temp(srt, ".srt")
        try:
            segments = parse_srt(path)
        finally:
            os.unlink(path)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["text"], "Hello world")  # HTML stripped
        self.assertEqual(segments[0]["start"], 1.0)
        self.assertEqual(segments[0]["end"], 4.5)
        self.assertEqual(segments[1]["start"], 3605.0)


class TestHelpers(unittest.TestCase):
    def test_safe_filename_strips_bad_chars(self):
        self.assertEqual(safe_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_safe_filename_truncates(self):
        self.assertEqual(len(safe_filename("x" * 200)), 80)

    def test_format_ts(self):
        self.assertEqual(_format_ts(65), "1:05")
        self.assertEqual(_format_ts(3725), "1:02:05")
        self.assertEqual(_format_ts(0), "0:00")


if __name__ == "__main__":
    unittest.main()

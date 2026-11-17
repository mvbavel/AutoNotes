"""Claude's reply is model output, not a validated schema.

Regression: a chapter without a title, key points as bare strings, a null
speaker or a sentence before the JSON each crashed the run at its final stage —
after the download, the transcription and the paid API call — or silently
produced a 500-character transcript stub under a green "Notes saved".
"""
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

try:   # anthropic 1.x+ sits on httpx2 and no longer installs httpx
    import httpx2 as http
except ImportError:
    import httpx as http

from pipeline import note_generator as ng

SEGMENTS = [
    {"start": 0, "end": 5, "speaker": "Ana", "text": "Welcome to the review."},
    {"start": 5, "end": 9, "speaker": "Ana", "text": "First the budget."},
    {"start": 9, "end": 14, "speaker": "Ben", "text": "Budget looks fine."},
]


def _response(text, stop="end_turn"):
    return types.SimpleNamespace(stop_reason=stop,
                                 content=[types.SimpleNamespace(type="text", text=text)])


class _DebugDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = ng._DEBUG_DIR
        ng._DEBUG_DIR = self._tmp.name

    def tearDown(self):
        ng._DEBUG_DIR = self._orig
        self._tmp.cleanup()

    def parse(self, text, **kwargs):
        return ng._parse_response(_response(text, **kwargs), "Video", SEGMENTS,
                                  valid_idx={1, 2})


class TestShapeNormalisation(_DebugDirCase):
    def test_messy_but_usable_reply_is_kept(self):
        notes = self.parse('{"title": "", "chapters": ['
                           '{"key_points": ["plain string", {"text": "a\\u000bb"}],'
                           ' "speakers": [null, "Ana"]},'
                           '"not a chapter"]}')
        self.assertNotIn("degraded", notes)
        self.assertEqual(notes["title"], "Video")          # empty title -> video title
        chapter = notes["chapters"][0]
        self.assertEqual(chapter["title"], "Part 1")       # missing title gets one
        self.assertEqual(chapter["speakers"], ["Ana"])     # null dropped
        self.assertEqual([p["text"] for p in chapter["key_points"]], ["plain string", "ab"])

    def test_wrong_shapes_fall_back_to_the_transcript(self):
        for reply in ('[]', '{"chapters": "none"}', '{"chapters": ["intro"]}',
                      '{"chapters": []}', '{"chapters": [{"key_points": []}]}'):
            notes = self.parse(reply)
            self.assertIn("degraded", notes, reply)
            self.assertEqual(notes["chapters"][0]["title"], "Transcript", reply)

    def test_sentence_before_json(self):
        notes = self.parse('Here are the notes:\n{"title": "T", "chapters": '
                           '[{"title": "C", "key_points": [{"text": "x"}]}]}')
        self.assertNotIn("degraded", notes)
        self.assertEqual(notes["title"], "T")

    def test_code_fence_with_and_without_newline(self):
        body = '{"title": "T", "chapters": [{"title": "C", "key_points": ["x"]}]}'
        for reply in (f"```json\n{body}\n```", f"```{body}```"):
            self.assertEqual(self.parse(reply)["title"], "T", reply)

    def test_bad_times_are_dropped_not_fatal(self):
        notes = self.parse('{"chapters": [{"title": "C", "start_time": "soon",'
                           ' "end_time": -4, "key_points": ["x"]}]}')
        self.assertIsNone(notes["chapters"][0]["start_time"])
        self.assertIsNone(notes["chapters"][0]["end_time"])


class TestFallbackNotes(_DebugDirCase):
    def test_contains_the_whole_transcript_with_speakers(self):
        notes = ng._fallback_notes("Video", SEGMENTS, "Claude's reply was cut off")
        text = " ".join(p["text"] for p in notes["chapters"][0]["key_points"])
        for seg in SEGMENTS:
            self.assertIn(seg["text"], text)
        self.assertIn("Ana:", text)
        self.assertIn("Ben:", text)
        self.assertEqual(notes["degraded"], "Claude's reply was cut off")

    def test_truncated_reply_says_so(self):
        notes = self.parse('{"title": "T", "chapters": [', stop="max_tokens")
        self.assertIn("cut off", notes["degraded"])

    def test_refusal_says_so(self):
        response = types.SimpleNamespace(stop_reason="refusal", content=[])
        notes = ng._parse_response(response, "Video", SEGMENTS, valid_idx=set())
        self.assertIn("declined", notes["degraded"])


class TestTransportErrors(unittest.TestCase):
    def test_both_http_libraries_are_retried(self):
        errors = ng._transport_errors()
        self.assertIn(http.TransportError, errors)
        self.assertTrue(ng._is_retryable(http.ReadError("reset")))
        try:
            import httpx
        except ImportError:
            return
        self.assertIn(httpx.TransportError, errors)


def _status_error(cls, status):
    request = http.Request("GET", "https://api.anthropic.com/v1/models/x")
    return cls("e", response=http.Response(status, request=request), body=None)


class TestCheckApiKey(unittest.TestCase):
    def _client(self, side_effect=None):
        client = MagicMock()
        client.models.retrieve.side_effect = side_effect
        return patch.object(anthropic, "Anthropic", return_value=client)

    def test_valid_key(self):
        with self._client():
            self.assertIsNone(ng.check_api_key("sk-ant-x"))

    def test_rejected_key_raises(self):
        with self._client(_status_error(anthropic.AuthenticationError, 401)):
            with self.assertRaises(anthropic.AuthenticationError):
                ng.check_api_key("bad")

    def test_unknown_model_explains(self):
        with self._client(_status_error(anthropic.NotFoundError, 404)):
            with self.assertRaises(RuntimeError) as ctx:
                ng.check_api_key("sk-ant-x")
        self.assertIn(ng.MODEL, str(ctx.exception))

    def test_offline_does_not_block_the_run(self):
        request = http.Request("GET", "https://api.anthropic.com")
        with self._client(anthropic.APIConnectionError(request=request)):
            note = ng.check_api_key("sk-ant-x")
        self.assertIn("continuing", note)


if __name__ == "__main__":
    unittest.main()

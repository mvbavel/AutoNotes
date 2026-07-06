"""Regression test: claude-sonnet-5 responses start with a ThinkingBlock,
so _parse_response must find the text block by type, not position."""
import os
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import note_generator


def _block(btype, **kw):
    return types.SimpleNamespace(type=btype, **kw)


def _response(blocks):
    return types.SimpleNamespace(content=blocks)


VALID_JSON = ('{"title": "T", "chapters": [{"title": "C", "start_time": 0, '
              '"end_time": 1, "speakers": [], "key_points": '
              '[{"text": "k", "screenshot_idx": 1}]}]}')


class TestParseResponse(unittest.TestCase):
    def setUp(self):
        # Keep debug dumps out of the real log dir
        self._orig_debug_dir = note_generator._DEBUG_DIR
        self._tmp = tempfile.TemporaryDirectory()
        note_generator._DEBUG_DIR = self._tmp.name

    def tearDown(self):
        note_generator._DEBUG_DIR = self._orig_debug_dir
        self._tmp.cleanup()

    def test_thinking_block_first(self):
        resp = _response([
            _block("thinking", thinking="pondering…"),
            _block("text", text=VALID_JSON),
        ])
        notes = note_generator._parse_response(resp, "vid", [], valid_idx={1})
        self.assertEqual(notes["title"], "T")
        self.assertEqual(notes["chapters"][0]["key_points"][0]["screenshot_idx"], 1)

    def test_text_block_only(self):
        resp = _response([_block("text", text=VALID_JSON)])
        notes = note_generator._parse_response(resp, "vid", [], valid_idx={1})
        self.assertEqual(notes["title"], "T")

    def test_no_text_block_falls_back(self):
        resp = _response([_block("thinking", thinking="…")])
        segments = [{"start": 0, "end": 2, "text": "hello"}]
        logs = []
        notes = note_generator._parse_response(resp, "vid", segments,
                                               valid_idx={1}, log_cb=logs.append)
        self.assertEqual(notes["chapters"][0]["title"], "Full Content")
        self.assertTrue(any("no text block" in m for m in logs))

    def test_invalid_json_falls_back_and_dumps(self):
        resp = _response([_block("text", text="not json")])
        segments = [{"start": 0, "end": 2, "text": "hello"}]
        notes = note_generator._parse_response(resp, "vid", segments,
                                               valid_idx={1}, tag="unittest")
        self.assertEqual(notes["chapters"][0]["title"], "Full Content")
        dump = os.path.join(self._tmp.name, "last_claude_unittest.json")
        self.assertTrue(os.path.isfile(dump))


if __name__ == "__main__":
    unittest.main()

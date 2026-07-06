"""Tests for saved-transcript loading (the 'Reuse last transcript' option)."""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import worker


SEGMENTS = [{"start": 0.0, "end": 2.5, "text": "hello", "speaker": "Speaker"}]


class TestLoadSavedTranscript(unittest.TestCase):
    def setUp(self):
        self._orig = worker.DEBUG_DIR
        self._tmp = tempfile.TemporaryDirectory()
        worker.DEBUG_DIR = self._tmp.name
        self._path = os.path.join(self._tmp.name, "last_transcript.json")

    def tearDown(self):
        worker.DEBUG_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, data):
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_current_format(self):
        self._write({"video": "My Meeting", "saved": "2026-07-05T09:18:52",
                     "segments": SEGMENTS})
        segments, meta = worker.load_saved_transcript()
        self.assertEqual(segments, SEGMENTS)
        self.assertEqual(meta["video"], "My Meeting")
        self.assertNotIn("segments", meta)

    def test_legacy_bare_list(self):
        self._write(SEGMENTS)
        segments, meta = worker.load_saved_transcript()
        self.assertEqual(segments, SEGMENTS)
        self.assertEqual(meta, {})

    def test_missing_file(self):
        segments, meta = worker.load_saved_transcript()
        self.assertEqual((segments, meta), ([], {}))

    def test_corrupt_file(self):
        with open(self._path, "w") as f:
            f.write("{not json")
        segments, meta = worker.load_saved_transcript()
        self.assertEqual((segments, meta), ([], {}))

    def test_malformed_dict(self):
        self._write({"video": "x", "segments": "not-a-list"})
        segments, meta = worker.load_saved_transcript()
        self.assertEqual((segments, meta), ([], {}))


if __name__ == "__main__":
    unittest.main()

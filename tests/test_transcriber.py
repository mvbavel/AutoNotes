"""Tests for transcriber helpers (duration formatting, RTF table sanity)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.transcriber import _fmt_secs, _RTF


class TestFmtSecs(unittest.TestCase):
    def test_seconds(self):
        self.assertEqual(_fmt_secs(45), "45s")

    def test_minutes(self):
        self.assertEqual(_fmt_secs(125), "2m 05s")

    def test_hours(self):
        self.assertEqual(_fmt_secs(3725), "1h 02m")


class TestRtfTable(unittest.TestCase):
    def test_covers_all_ui_model_sizes(self):
        # Must match the choices offered in ui/main_window.py's model combo
        for size in ("tiny", "base", "small", "medium", "large-v3"):
            self.assertIn(size, _RTF)

    def test_ordering_smaller_is_faster(self):
        self.assertLess(_RTF["tiny"], _RTF["base"])
        self.assertLess(_RTF["base"], _RTF["small"])
        self.assertLess(_RTF["small"], _RTF["medium"])
        self.assertLess(_RTF["medium"], _RTF["large-v3"])


if __name__ == "__main__":
    unittest.main()

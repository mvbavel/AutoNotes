"""Tests for the middle-elision helper used in the reuse-transcript label."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ui.main_window import _elide_middle


class TestElideMiddle(unittest.TestCase):
    def test_long_string_elided(self):
        name = "AI-first vision deep dive-20260630_085952-Meeting Recording"
        out = _elide_middle(name)
        self.assertEqual(out, "AI-first vision… Recording")  # last 10 incl. space
        self.assertEqual(out[:15], name[:15])
        self.assertEqual(out[-10:], name[-10:])

    def test_short_string_untouched(self):
        self.assertEqual(_elide_middle("Team Standup"), "Team Standup")

    def test_boundary_not_elided(self):
        s = "x" * 26  # 15 + 10 + 1, eliding wouldn't shorten it
        self.assertEqual(_elide_middle(s), s)

    def test_just_over_boundary_elided(self):
        s = "x" * 27
        self.assertEqual(_elide_middle(s), "x" * 15 + "…" + "x" * 10)

    def test_custom_lengths(self):
        self.assertEqual(_elide_middle("abcdefghij", head=2, tail=2), "ab…ij")


if __name__ == "__main__":
    unittest.main()

"""Tests for frame selection diversity and screenshot reference normalization.

Regression coverage for the Teams-recording failure where 34/40 frames came
from one 8-minute demo window and Claude's screenshot references were
silently dropped by the docx writer.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.frame_extractor import (MAX_FRAMES, MIN_FRAMES, _frame_budget,
                                      _select_top)
from pipeline.note_generator import (MAX_SCREENSHOTS, _coerce_idx,
                                     _normalize_screenshot_refs)


def _group(ts, score):
    return {"timestamp": ts, "score": score, "path": f"f{ts}.jpg", "cropped": False}


class TestSelectTop(unittest.TestCase):
    def test_burst_cannot_crowd_out_rest_of_video(self):
        """A dense run of high scorers (demo window) must not evict slides."""
        burst = [_group(3000 + i * 5, 0.9) for i in range(30)]   # 145s window, 5s apart
        slides = [_group(i * 300, 0.5) for i in range(8)]        # spread over 40min
        top = _select_top(burst + slides, max_frames=10, min_gap=25)
        slide_ts = {g["timestamp"] for g in slides}
        picked_slides = sum(1 for g in top if g["timestamp"] in slide_ts)
        # Without the gap constraint the higher-scoring burst takes all 10
        # slots and picked_slides is 0. With it, the 145s burst can claim at
        # most one slot per 25s (6), leaving the rest for the slides.
        self.assertGreaterEqual(picked_slides, 4)
        burst_picks = len(top) - picked_slides
        self.assertLessEqual(burst_picks, 6)

    def test_fills_remaining_slots_when_gap_exhausts(self):
        """Short video: fewer gap-respecting candidates than slots → fill by score."""
        groups = [_group(i * 5, 0.5 + i * 0.01) for i in range(6)]  # all within 30s
        top = _select_top(groups, max_frames=4, min_gap=25)
        self.assertEqual(len(top), 4)

    def test_sorted_by_timestamp(self):
        groups = [_group(500, 0.9), _group(100, 0.8), _group(300, 0.7)]
        top = _select_top(groups, max_frames=3, min_gap=25)
        self.assertEqual([g["timestamp"] for g in top], [100, 300, 500])


class TestFrameBudget(unittest.TestCase):
    def test_short_video_gets_one_per_30s(self):
        self.assertEqual(_frame_budget(10 * 60), 20)   # 10 min -> 20 frames

    def test_half_hour_hits_ceiling_exactly(self):
        self.assertEqual(_frame_budget(30 * 60), 60)

    def test_long_video_capped_to_one_per_minute_or_less(self):
        self.assertEqual(_frame_budget(98 * 60), MAX_FRAMES)  # ~1 per 98s

    def test_tiny_video_floor(self):
        self.assertEqual(_frame_budget(60), MIN_FRAMES)

    def test_all_selected_frames_reach_claude(self):
        self.assertGreaterEqual(MAX_SCREENSHOTS, MAX_FRAMES)


class TestCoerceIdx(unittest.TestCase):
    def test_int_passthrough(self):
        self.assertEqual(_coerce_idx(7), 7)

    def test_numeric_string(self):
        self.assertEqual(_coerce_idx("12"), 12)

    def test_labelled_string(self):
        self.assertEqual(_coerce_idx("Screenshot 5"), 5)

    def test_integral_float(self):
        self.assertEqual(_coerce_idx(3.0), 3)

    def test_garbage(self):
        self.assertIsNone(_coerce_idx("none"))
        self.assertIsNone(_coerce_idx(None))
        self.assertIsNone(_coerce_idx(True))


class TestNormalizeRefs(unittest.TestCase):
    def _notes(self, idx_values):
        return {"title": "t", "chapters": [{
            "title": "c", "key_points": [
                {"text": "p", "screenshot_idx": v} for v in idx_values
            ],
        }]}

    def test_valid_kept_invalid_dropped_and_logged(self):
        notes = self._notes([1, "2", 99, "Screenshot 3", "n/a", None])
        logs = []
        _normalize_screenshot_refs(notes, valid_idx={1, 2, 3}, log_cb=logs.append)
        kps = notes["chapters"][0]["key_points"]
        self.assertEqual([kp["screenshot_idx"] for kp in kps],
                         [1, 2, None, 3, None, None])
        self.assertEqual(len(logs), 1)
        self.assertIn("2 invalid", logs[0])

    def test_no_valid_idx_is_noop(self):
        notes = self._notes(["junk"])
        _normalize_screenshot_refs(notes, valid_idx=None)
        self.assertEqual(notes["chapters"][0]["key_points"][0]["screenshot_idx"], "junk")


if __name__ == "__main__":
    unittest.main()

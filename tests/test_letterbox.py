"""Tests for letterbox band detection.

Modelled on a real 60-frame keynote run in which 44 frames were letterboxed by
a composited backdrop and 14 ran full-bleed. Both must be handled by the same
per-frame pass: cropping the letterboxed ones is the point, and not cropping the
full-bleed ones matters just as much, since a crop there removes the slide title.
"""
import os
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.letterbox import detect_bars, trim_letterbox

H, W = 1080, 1920


def _content(h, w, seed=0):
    """A region with strong horizontal detail, as real slide content has."""
    rng = np.random.default_rng(seed)
    return rng.integers(0, 256, (h, w, 3), dtype=np.uint8)


def _gradient_band(h, w, start=20, end=90):
    """A band that varies down the frame but not across it.

    This is the case a black-bar or flat-colour test misses: the branded
    backdrops in the source recording are gradients, not flat fills.
    """
    column = np.linspace(start, end, h, dtype=np.float32)
    return np.repeat(np.repeat(column[:, None], w, axis=1)[:, :, None], 3, axis=2).astype(np.uint8)


def _letterboxed(top, bottom, h=H, w=W):
    img = np.zeros((h, w, 3), dtype=np.uint8)
    if top:
        img[:top] = _gradient_band(top, w)
    img[top:h - bottom] = _content(h - top - bottom, w)
    if bottom:
        img[h - bottom:] = _gradient_band(bottom, w, 90, 20)
    return img


class TestDetectBars(unittest.TestCase):
    def test_finds_bands_on_both_edges(self):
        top, bottom = detect_bars(cv2, _letterboxed(160, 150))
        self.assertAlmostEqual(top, 160, delta=8)
        self.assertAlmostEqual(bottom, 150, delta=8)

    def test_full_bleed_frame_is_left_alone(self):
        """The regression that makes per-frame detection necessary.

        A video mixes letterboxed scenes with edge-to-edge slides; cropping one
        of the latter cuts the title off.
        """
        self.assertEqual(detect_bars(cv2, _content(H, W, seed=1)), (0, 0))

    def test_gradient_band_is_still_a_band(self):
        """Detection keys on horizontal detail, so a vertical gradient qualifies."""
        img = _letterboxed(160, 0)
        self.assertGreater(detect_bars(cv2, img)[0], 100)

    def test_asymmetric_bands(self):
        top, bottom = detect_bars(cv2, _letterboxed(200, 0))
        self.assertAlmostEqual(top, 200, delta=8)
        self.assertEqual(bottom, 0)

    def test_oversized_band_is_refused(self):
        """Losing a quarter of the frame is worse than keeping a stripe."""
        self.assertEqual(detect_bars(cv2, _letterboxed(500, 0))[0], 0)

    def test_hairline_band_is_ignored(self):
        self.assertEqual(detect_bars(cv2, _letterboxed(6, 6)), (0, 0))

    def test_uniform_frame_is_not_all_bar(self):
        """A fade to black reads as flat everywhere; trimming it to nothing is wrong."""
        self.assertEqual(detect_bars(cv2, np.zeros((H, W, 3), dtype=np.uint8)), (0, 0))

    def test_rejects_unusable_input(self):
        self.assertEqual(detect_bars(cv2, None), (0, 0))
        self.assertEqual(detect_bars(cv2, np.zeros((H, W), dtype=np.uint8)), (0, 0))
        self.assertEqual(detect_bars(cv2, np.zeros((10, W, 3), dtype=np.uint8)), (0, 0))


class TestTrim(unittest.TestCase):
    def test_trim_removes_exactly_the_bands(self):
        img = _letterboxed(160, 150)
        out = trim_letterbox(cv2, img)
        self.assertIsNotNone(out)
        self.assertAlmostEqual(out.shape[0], H - 310, delta=16)
        self.assertEqual(out.shape[1], W)

    def test_nothing_to_trim_returns_none(self):
        """None, not a copy — the caller skips a needless JPEG re-encode."""
        self.assertIsNone(trim_letterbox(cv2, _content(H, W, seed=2)))


if __name__ == "__main__":
    unittest.main()

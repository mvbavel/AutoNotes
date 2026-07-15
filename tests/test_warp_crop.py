"""Regression tests: near-upright quads must be cropped, not warped.

Warping flat screencast content to slightly-imprecise corners rotated
document screenshots by a few degrees (seen with 1920px extraction).
"""
import math
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.frame_extractor import _max_edge_tilt_deg, _order_corners, _warp_crop


def _rect_quad(x0, y0, x1, y1, tilt_deg=0.0):
    """Corner array for a rectangle, optionally rotated about its centre."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    t = math.radians(tilt_deg)
    pts = []
    for x, y in [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]:
        dx, dy = x - cx, y - cy
        pts.append([cx + dx * math.cos(t) - dy * math.sin(t),
                    cy + dx * math.sin(t) + dy * math.cos(t)])
    return np.array(pts, dtype=np.float32)


class TestEdgeTilt(unittest.TestCase):
    def test_perfect_rectangle_is_zero(self):
        q = _order_corners(_rect_quad(100, 100, 900, 600))
        self.assertAlmostEqual(_max_edge_tilt_deg(q), 0.0, places=5)

    def test_small_rotation_measured(self):
        q = _order_corners(_rect_quad(100, 100, 900, 600, tilt_deg=2.0))
        self.assertAlmostEqual(_max_edge_tilt_deg(q), 2.0, places=1)

    def test_skewed_trapezoid_is_large(self):
        # A filmed screen: right edge much shorter than left
        quad = _order_corners(np.array(
            [[100, 100], [800, 180], [800, 520], [100, 600]], dtype=np.float32))
        self.assertGreater(_max_edge_tilt_deg(quad), 5.0)


class TestWarpCrop(unittest.TestCase):
    def setUp(self):
        import cv2
        self.cv2 = cv2
        # Gradient image so pixel-exactness is detectable
        self.img = np.arange(1280 * 720 * 3, dtype=np.uint32).reshape(
            720, 1280, 3).astype(np.uint8)

    def test_upright_quad_plain_crops_pixel_exact(self):
        quad = _rect_quad(100.4, 50.6, 900.2, 500.8)   # sub-pixel corners
        out = _warp_crop(self.cv2, self.img, quad)
        # Exact sub-array of the original: no rotation, no interpolation
        np.testing.assert_array_equal(out, self.img[50:501, 100:901])

    def test_slightly_tilted_quad_still_plain_crops(self):
        quad = _rect_quad(100, 50, 900, 500, tilt_deg=1.5)  # detector jitter
        out = _warp_crop(self.cv2, self.img, quad)
        # Plain crop: rows of the output are contiguous rows of the source
        y0 = int(np.floor(quad[:, 1].min()))
        x0 = int(np.floor(quad[:, 0].min()))
        np.testing.assert_array_equal(out[0], self.img[y0, x0:x0 + out.shape[1]])

    def test_skewed_quad_is_warped(self):
        quad = np.array([[100, 100], [900, 160], [900, 560], [100, 620]],
                        dtype=np.float32)
        out = _warp_crop(self.cv2, self.img, quad)
        self.assertIsNotNone(out)
        # Warped output cannot be a contiguous sub-array of the source
        self.assertFalse(
            np.array_equal(out[0], self.img[100, 100:100 + out.shape[1]]))

    def test_too_small_rejected(self):
        self.assertIsNone(_warp_crop(self.cv2, self.img, _rect_quad(0, 0, 150, 100)))


if __name__ == "__main__":
    unittest.main()

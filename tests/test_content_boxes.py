"""Tests for per-screenshot content_box validation and cropped DOCX embeds."""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.note_generator import _normalize_content_boxes, _valid_box
from output.docx_writer import _add_screenshot, _image_stream


class TestValidBox(unittest.TestCase):
    def test_good_box(self):
        self.assertEqual(_valid_box([0.0, 0.05, 0.87, 0.94]),
                         [0.0, 0.05, 0.87, 0.94])

    def test_clamped(self):
        self.assertEqual(_valid_box([-0.1, 0.0, 1.4, 1.0]), [0.0, 0.0, 1.0, 1.0])

    def test_tiny_area_rejected(self):
        self.assertIsNone(_valid_box([0.4, 0.4, 0.6, 0.6]))  # 4% of frame

    def test_inverted_rejected(self):
        self.assertIsNone(_valid_box([0.9, 0.1, 0.1, 0.9]))

    def test_junk_rejected(self):
        self.assertIsNone(_valid_box(None))
        self.assertIsNone(_valid_box("full"))
        self.assertIsNone(_valid_box([0.1, 0.1, 0.9]))
        self.assertIsNone(_valid_box([0.0, 0.0, "all", 1.0]))


class TestNormalizeContentBoxes(unittest.TestCase):
    def test_string_keys_and_shapes(self):
        notes = {"chapters": [], "screenshots": {
            "3": {"content_box": [0.0, 0.0, 0.9, 0.9]},   # canonical shape
            "5": [0.0, 0.1, 1.0, 1.0],                    # bare list tolerated
            "7": {"content_box": None},                   # no content -> absent
            "99": {"content_box": [0.0, 0.0, 1.0, 1.0]},  # invalid idx -> dropped
            "8": {"content_box": [0.4, 0.4, 0.5, 0.5]},   # tiny -> dropped
        }}
        out = _normalize_content_boxes(notes, valid_idx={3, 5, 7, 8})
        self.assertEqual(set(out["screenshot_boxes"]), {3, 5})
        self.assertNotIn("screenshots", out)

    def test_absent_screenshots_map(self):
        out = _normalize_content_boxes({"chapters": []}, valid_idx={1})
        self.assertEqual(out["screenshot_boxes"], {})


class TestCroppedEmbed(unittest.TestCase):
    def setUp(self):
        from PIL import Image
        fd, self.path = tempfile.mkstemp(suffix=".jpg")
        os.close(fd)
        Image.new("RGB", (200, 100), (10, 120, 200)).save(self.path, "JPEG")

    def tearDown(self):
        os.unlink(self.path)

    def test_crop_dimensions(self):
        from PIL import Image
        buf = _image_stream(self.path, [0.0, 0.0, 0.5, 1.0])
        img = Image.open(buf)
        self.assertEqual(img.size, (100, 100))

    def test_embed_with_box(self):
        from docx import Document
        doc = Document()
        _add_screenshot(doc, self.path, box=[0.1, 0.1, 0.9, 0.9])
        self.assertEqual(len(doc.inline_shapes), 1)

    def test_embed_without_box(self):
        from docx import Document
        doc = Document()
        _add_screenshot(doc, self.path, box=None)
        self.assertEqual(len(doc.inline_shapes), 1)


if __name__ == "__main__":
    unittest.main()

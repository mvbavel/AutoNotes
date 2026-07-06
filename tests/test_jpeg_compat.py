"""Tests for JFIF-marker detection (ffmpeg JPEGs that python-docx rejects)."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.frame_extractor import _needs_jfif_rewrite


def _write(data: bytes) -> str:
    fd, path = tempfile.mkstemp(suffix=".jpg")
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    return path


class TestNeedsJfifRewrite(unittest.TestCase):
    def _check(self, data, expected):
        path = _write(data)
        try:
            self.assertIs(_needs_jfif_rewrite(path), expected)
        finally:
            os.unlink(path)

    def test_ffmpeg_comment_jpeg_needs_rewrite(self):
        # ffmpeg mjpeg output: SOI + COM segment carrying "Lavc..."
        self._check(b"\xff\xd8\xff\xfe\x00\x10Lavc62.11.100", True)

    def test_jfif_jpeg_ok(self):
        self._check(b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01", False)

    def test_exif_jpeg_ok(self):
        self._check(b"\xff\xd8\xff\xe1\x1c\x45Exif\x00\x00", False)

    def test_non_jpeg_untouched(self):
        self._check(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, False)

    def test_missing_file(self):
        self.assertFalse(_needs_jfif_rewrite("/nonexistent/frame.jpg"))


if __name__ == "__main__":
    unittest.main()

"""The document writer must produce a document from whatever it is handed.

Regression: an empty title, a control character from a YouTube description,
a None speaker or string key points each raised inside python-docx at the last
stage of a run, losing everything before it.
"""
import os
import sys
import tempfile
import unittest
from typing import ClassVar

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from docx import Document

from output.docx_writer import timestamp_url, write_docx


class _Tmp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def build(self, notes, frames=(), source=None):
        path = write_docx(notes, list(frames), self.dir, "t", source_info=source)
        return Document(path)


class TestMalformedNotes(_Tmp):
    CASES: ClassVar[dict] = {
        "empty chapter title": {"title": "T", "chapters": [{"title": "", "key_points": ["x"]}]},
        "missing chapter title": {"title": "T", "chapters": [{"key_points": ["x"]}]},
        "empty document title": {"title": "", "chapters": []},
        "None title": {"title": None, "chapters": None},
        "control characters": {"title": "T\x0b", "chapters": [
            {"title": "C\x1f", "key_points": [{"text": "a\x0bb"}]}]},
        "string key points": {"title": "T", "chapters": [{"title": "C", "key_points": ["s"]}]},
        "None speaker": {"title": "T", "chapters": [
            {"title": "C", "speakers": [None, "Ana"], "key_points": ["s"]}]},
        "garbage entries": {"title": "T", "chapters": ["x", 5, {"key_points": [None, 3]}]},
    }

    def test_every_case_produces_a_document(self):
        for name, notes in self.CASES.items():
            with self.subTest(name):
                doc = self.build(notes, source={"type": "YouTube", "url": "https://x",
                                                "summary": "line\x1fone"})
                self.assertTrue(doc.paragraphs)

    def test_degraded_notes_say_so_at_the_top(self):
        doc = self.build({"title": "T", "degraded": "Claude's reply was cut off",
                          "chapters": [{"title": "Transcript", "key_points": ["words"]}]})
        text = "\n".join(p.text for p in doc.paragraphs[:4])
        self.assertIn("contains the transcript instead of AI notes", text)


class TestChapterTimes(_Tmp):
    def test_youtube_chapter_links_to_its_moment(self):
        doc = self.build({"title": "T", "chapters": [
            {"title": "Budget", "start_time": 754, "end_time": 1085, "key_points": ["x"]}]},
            source={"type": "YouTube", "url": "https://www.youtube.com/watch?v=abc"})
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("12:34 – 18:05", text)
        targets = {r.target_ref for r in doc.part.rels.values() if "hyperlink" in r.reltype}
        self.assertIn("https://www.youtube.com/watch?v=abc&t=754s", targets)

    def test_timestamp_url_only_for_known_hosts(self):
        self.assertEqual(timestamp_url("https://youtu.be/abc", 61), "https://youtu.be/abc?t=61s")
        self.assertIsNone(timestamp_url("https://contoso.sharepoint.com/x", 61))
        self.assertIsNone(timestamp_url("/Users/me/talk.mp4", 61))


class TestDescription(_Tmp):
    def test_long_description_is_trimmed(self):
        summary = "\n".join(f"Sponsor link {i}" for i in range(400))
        doc = self.build({"title": "T", "chapters": []},
                         source={"type": "YouTube", "url": "", "summary": summary,
                                 "summary_label": "Description", "summary_is_description": True})
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Description:", text)
        self.assertLess(len(text), 2000)
        self.assertTrue(text.rstrip().endswith("…"))

    def test_recap_is_kept_whole(self):
        summary = "Decision. " * 300
        doc = self.build({"title": "T", "chapters": []},
                         source={"type": "Teams / SharePoint", "url": "", "summary": summary,
                                 "summary_label": "Teams recap"})
        text = "\n".join(p.text for p in doc.paragraphs)
        self.assertIn("Teams recap:", text)
        self.assertIn(summary.strip(), text)


class TestScreenshots(_Tmp):
    def test_alt_text_describes_the_screenshot(self):
        import cv2
        img_path = os.path.join(self.dir, "f.jpg")
        cv2.imwrite(img_path, np.full((300, 400, 3), 200, np.uint8))
        doc = self.build({"title": "T", "chapters": [{"title": "C", "key_points": [
            {"text": "The **roadmap** slide", "screenshot_idx": 1}]}]},
            frames=[{"path": img_path, "timestamp": 95}])
        doc_prs = doc.element.body.findall(".//{*}docPr")
        self.assertEqual(len(doc_prs), 1)
        self.assertEqual(doc_prs[0].get("descr"), "Screenshot at 1:35: The roadmap slide")


if __name__ == "__main__":
    unittest.main()

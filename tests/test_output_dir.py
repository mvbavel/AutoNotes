"""The finished document must never be lost to the output folder.

Regression: an unwritable ~/Desktop (macOS privacy denial surfaces as
FileExistsError from os.makedirs) only failed at the final stage, after the
download, transcription and Claude call had all run.
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from output.docx_writer import check_output_dir, write_docx
from pipeline import worker

NOTES = {"title": "T", "chapters": [{"title": "C", "key_points": [
    {"text": "point", "screenshot_idx": None}]}]}


class TestCheckOutputDir(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_writable_dir_passes(self):
        self.assertIsNone(check_output_dir(self._tmp.name))
        self.assertEqual(os.listdir(self._tmp.name), [])  # probe cleaned up

    def test_missing_dir_is_created(self):
        path = os.path.join(self._tmp.name, "new", "sub")
        self.assertIsNone(check_output_dir(path))
        self.assertTrue(os.path.isdir(path))

    def test_path_that_is_a_file_fails(self):
        path = os.path.join(self._tmp.name, "afile")
        open(path, "w").close()
        self.assertIn(path, check_output_dir(path))

    def test_read_only_dir_fails(self):
        path = os.path.join(self._tmp.name, "ro")
        os.mkdir(path, 0o500)
        try:
            self.assertIsNotNone(check_output_dir(path))
        finally:
            os.chmod(path, 0o700)


class TestNoOverwrite(unittest.TestCase):
    def test_existing_document_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = write_docx(dict(NOTES), [], tmp, "t")
            with open(first, "ab") as f:
                f.write(b"user edits")
            size = os.path.getsize(first)
            second = write_docx(dict(NOTES), [], tmp, "t")
            self.assertNotEqual(first, second)
            self.assertEqual(os.path.getsize(first), size)
            self.assertTrue(second.endswith("t_notes (2).docx"))


class TestWorkerOutputHandling(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = worker.DEBUG_DIR
        worker.DEBUG_DIR = os.path.join(self._tmp.name, "logs")

    def tearDown(self):
        worker.DEBUG_DIR = self._orig
        self._tmp.cleanup()

    def test_unwritable_output_fails_before_download(self):
        bad = os.path.join(self._tmp.name, "afile")
        open(bad, "w").close()
        w = worker.ProcessingWorker("https://youtu.be/x", {"output_dir": bad})
        with patch.object(worker, "download_youtube") as download:
            with self.assertRaises(RuntimeError):
                w._run_pipeline(self._tmp.name)
        download.assert_not_called()

    def test_save_failure_falls_back_to_log_dir(self):
        w = worker.ProcessingWorker("x", {})
        real = worker.write_docx
        calls = []

        def flaky(notes, frames, output_dir, *args, **kwargs):
            calls.append(output_dir)
            if len(calls) == 1:
                raise PermissionError(13, "Permission denied")
            return real(notes, frames, output_dir, *args, **kwargs)

        with patch.object(worker, "write_docx", side_effect=flaky):
            path = w._write_document(dict(NOTES), [], "/denied", "t", None)
        self.assertEqual(calls, ["/denied", worker.DEBUG_DIR])
        self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main()

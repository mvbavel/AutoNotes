"""Shared pipeline helpers: file names, XML-safe text, owner-only files, and
the child-process runner every yt-dlp/ffmpeg call goes through."""
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _util
from pipeline._util import (
    PipelineCancelled, clean_text, find_video, private_dir, run_capture,
    run_streaming, safe_filename, write_private, ytdlp_template,
)


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


class TestSafeFilename(unittest.TestCase):
    def test_strips_path_and_shell_characters(self):
        self.assertEqual(safe_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_never_empty(self):
        # An empty stem produced "_notes.docx" and an all-dots one a hidden file
        for name in ("", "   ", "...", None):
            self.assertEqual(safe_filename(name), "recording", repr(name))

    def test_no_leading_dot(self):
        self.assertEqual(safe_filename("..hidden"), "hidden")

    def test_control_characters_removed(self):
        self.assertEqual(safe_filename("a\nb\tc"), "a_b_c")

    def test_capped_length(self):
        self.assertEqual(len(safe_filename("x" * 200)), 80)


class TestYtdlpTemplate(unittest.TestCase):
    def test_percent_in_title_is_escaped(self):
        # yt-dlp would otherwise read "100% Proof" as a template field
        self.assertEqual(ytdlp_template("/tmp", "100% Proof"), "/tmp/100%% Proof.%(ext)s")


class TestCleanText(unittest.TestCase):
    def test_removes_xml_illegal_characters(self):
        self.assertEqual(clean_text("a\x0bb\x1fc\x00d"), "abcd")

    def test_keeps_tabs_newlines_and_unicode(self):
        self.assertEqual(clean_text("a\tb\nc é ✓"), "a\tb\nc é ✓")

    def test_none_and_non_strings(self):
        self.assertEqual(clean_text(None), "")
        self.assertEqual(clean_text(5), "5")


class TestFiles(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_find_video_any_container(self):
        open(os.path.join(self.root, "t.mkv"), "w").close()
        self.assertEqual(find_video(self.root, "t"), os.path.join(self.root, "t.mkv"))
        self.assertIsNone(find_video(self.root, "other"))

    def test_private_dir_is_owner_only_even_if_it_existed(self):
        path = os.path.join(self.root, "logs")
        os.makedirs(path, mode=0o755)
        os.chmod(path, 0o755)
        private_dir(path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o700)

    def test_write_private_is_owner_only_even_if_it_existed(self):
        path = os.path.join(self.root, "t.json")
        with open(path, "w") as f:
            f.write("old")
        os.chmod(path, 0o644)
        write_private(path, "new")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertEqual(f.read(), "new")


class TestRunStreaming(unittest.TestCase):
    def test_lines_and_exit_code(self):
        lines = []
        code = run_streaming(_py("print('a'); print('b'); raise SystemExit(3)"),
                             on_line=lines.append)
        self.assertEqual([line.strip() for line in lines], ["a", "b"])
        self.assertEqual(code, 3)

    def test_stderr_is_merged(self):
        lines = []
        run_streaming(_py("import sys; sys.stderr.write('oops\\n')"), on_line=lines.append)
        self.assertEqual([line.strip() for line in lines], ["oops"])

    def test_cancel_while_child_is_silent(self):
        """A stalled download printed nothing, so the old loop never checked
        for a cancel and Cancel did nothing until the child exited."""
        started = time.monotonic()
        deadline = started + 0.6

        def cancel_check():
            if time.monotonic() > deadline:
                raise PipelineCancelled()

        with self.assertRaises(PipelineCancelled):
            run_streaming(_py("import time; time.sleep(30)"), cancel_check=cancel_check)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(_util._active, set(), "child must be killed and forgotten")

    def test_killed_child_reports_the_cancel_not_a_failure(self):
        cancelled = {"flag": False}

        def cancel_check():
            if cancelled["flag"]:
                raise PipelineCancelled()

        def on_line(_line):
            cancelled["flag"] = True
            _util.kill_active_processes()   # what the UI does on Cancel

        with self.assertRaises(PipelineCancelled):
            run_streaming(_py("import time; print('x', flush=True); time.sleep(30)"),
                          on_line=on_line, cancel_check=cancel_check)


class TestRunCapture(unittest.TestCase):
    def test_captures_output(self):
        result = run_capture(_py("print('out'); import sys; sys.stderr.write('err')"))
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr), (0, "out", "err"))

    def test_timeout(self):
        with self.assertRaises(subprocess.TimeoutExpired):
            run_capture(_py("import time; time.sleep(30)"), timeout=0.5)
        self.assertEqual(_util._active, set())

    def test_cancel(self):
        deadline = time.monotonic() + 0.5

        def cancel_check():
            if time.monotonic() > deadline:
                raise PipelineCancelled()

        with self.assertRaises(PipelineCancelled):
            run_capture(_py("import time; time.sleep(30)"), timeout=60,
                        cancel_check=cancel_check)


if __name__ == "__main__":
    unittest.main()

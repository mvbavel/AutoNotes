"""Tests for pipeline.downloader's subtitle handling.

Two gaps this covers:

1. yt-dlp raises a hard DownloadError when subtitle fetch fails (e.g. YouTube
   429s the subtitle endpoint) unless --ignore-errors is set, which took the
   whole video download down with it even though a missing SRT is a recoverable
   case (the pipeline falls back to Whisper transcription).
2. The 429 was self-inflicted: "--sub-langs en.*" also matches YouTube's
   auto-translated tracks (en-es-…, en-pt-…, en-de-…), so a single run fetched
   six or seven subtitle files back-to-back and got throttled — and the abort
   landed before the SRT conversion, so the run lost the transcript entirely.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import downloader
from pipeline._util import PipelineCancelled


def fake_proc(returncode=0, lines=None):
    proc = MagicMock()
    proc.stdout = iter(lines or [])
    proc.returncode = returncode
    proc.poll.return_value = returncode
    return proc


class TestPickSubLang(unittest.TestCase):
    """Exactly one English track, never a translation of one."""

    def test_prefers_manual_over_automatic(self):
        info = {
            "subtitles": {"en-eEY6OEpapPo": [], "es-y1SAwy5xd4g": []},
            "automatic_captions": {"en": [], "en-orig": []},
        }
        self.assertEqual(downloader._pick_sub_lang(info), "en-eEY6OEpapPo")

    def test_plain_en_wins_within_a_source(self):
        info = {"subtitles": {}, "automatic_captions": {"en-orig": [], "en": [], "de": []}}
        self.assertEqual(downloader._pick_sub_lang(info), "en")

    def test_falls_back_to_automatic_captions(self):
        info = {"subtitles": {"es-419": []}, "automatic_captions": {"en": []}}
        self.assertEqual(downloader._pick_sub_lang(info), "en")

    def test_never_returns_a_non_english_track(self):
        info = {"subtitles": {"es-419": [], "pt-BR": []}, "automatic_captions": {"de": []}}
        self.assertIsNone(downloader._pick_sub_lang(info))

    def test_missing_keys_are_safe(self):
        self.assertIsNone(downloader._pick_sub_lang({}))
        self.assertIsNone(downloader._pick_sub_lang({"subtitles": None, "automatic_captions": None}))

    def test_requests_a_single_lang_not_a_glob(self):
        """The glob is what tripped the 429; the arg must name one track."""
        captured = {}

        def record(args, **kwargs):
            captured["args"] = args
            return fake_proc(returncode=0)

        with patch("subprocess.Popen", side_effect=record):
            downloader._download_subtitles("u", "/tmp/o.%(ext)s", "/opt/homebrew/bin", "en")

        langs = captured["args"][captured["args"].index("--sub-langs") + 1]
        self.assertEqual(langs, "en")
        self.assertNotIn("*", langs)

    def test_skips_the_fetch_entirely_when_no_english_exists(self):
        with patch("subprocess.Popen") as mock_popen:
            logs = []
            downloader._download_subtitles("u", "/tmp/o.%(ext)s", "/x", None, log_cb=logs.append)
        mock_popen.assert_not_called()
        self.assertTrue(any("Whisper" in m for m in logs))

    def test_passes_ffmpeg_location_for_srt_conversion(self):
        """--convert-subs shells out to ffmpeg, which is not assumed on PATH."""
        captured = {}

        def record(args, **kwargs):
            captured["args"] = args
            return fake_proc(returncode=0)

        with patch("subprocess.Popen", side_effect=record):
            downloader._download_subtitles("u", "/tmp/o.%(ext)s", "/opt/homebrew/bin", "en")

        args = captured["args"]
        self.assertIn("--ffmpeg-location", args)
        self.assertEqual(args[args.index("--ffmpeg-location") + 1], "/opt/homebrew/bin")


class TestSubtitleFailureIsolation(unittest.TestCase):
    @patch("pipeline.downloader._find_output", return_value="/tmp/out/video.mp4")
    @patch("pipeline.downloader._run_json",
           return_value={"title": "t", "automatic_captions": {"en": []}})
    @patch("subprocess.Popen")
    def test_subtitle_429_does_not_fail_the_download(self, mock_popen, _run_json, _find_output):
        video_proc = fake_proc(returncode=0)
        sub_proc = fake_proc(returncode=1, lines=["ERROR: HTTP Error 429: Too Many Requests"])
        mock_popen.side_effect = [video_proc, sub_proc]

        logs = []
        result = downloader.download_youtube("https://youtu.be/x", "/tmp/out", log_cb=logs.append)

        self.assertEqual(result[0], "/tmp/out/video.mp4")
        self.assertTrue(any("Whisper" in m for m in logs))

    @patch("pipeline.downloader._find_output", return_value="/tmp/out/video.mp4")
    @patch("pipeline.downloader._run_json",
           return_value={"title": "t", "automatic_captions": {"en": []}})
    @patch("subprocess.Popen")
    def test_subtitle_subprocess_exception_does_not_fail_the_download(
        self, mock_popen, _run_json, _find_output
    ):
        video_proc = fake_proc(returncode=0)
        mock_popen.side_effect = [video_proc, OSError("boom")]

        logs = []
        result = downloader.download_youtube("https://youtu.be/x", "/tmp/out", log_cb=logs.append)

        self.assertEqual(result[0], "/tmp/out/video.mp4")
        self.assertTrue(any("Whisper" in m for m in logs))

    @patch("pipeline.downloader._run_json",
           return_value={"title": "t", "automatic_captions": {"en": []}})
    @patch("subprocess.Popen")
    def test_video_download_failure_still_raises(self, mock_popen, _run_json):
        mock_popen.return_value = fake_proc(returncode=1)

        with self.assertRaises(RuntimeError):
            downloader.download_youtube("https://youtu.be/x", "/tmp/out")

    @patch("pipeline.downloader._find_output", return_value="/tmp/out/video.mp4")
    @patch("pipeline.downloader._run_json",
           return_value={"title": "t", "automatic_captions": {"en": []}})
    @patch("subprocess.Popen")
    def test_cancellation_during_subtitle_fetch_propagates(self, mock_popen, _run_json, _find_output):
        video_proc = fake_proc(returncode=0)
        sub_proc = fake_proc(returncode=0, lines=["line1"])
        mock_popen.side_effect = [video_proc, sub_proc]

        def cancel_check():
            raise PipelineCancelled()

        with self.assertRaises(PipelineCancelled):
            downloader.download_youtube(
                "https://youtu.be/x", "/tmp/out", cancel_check=cancel_check
            )


if __name__ == "__main__":
    unittest.main()

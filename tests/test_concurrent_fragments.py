"""Fragmented (DASH/HLS) downloads must fetch fragments in parallel.

Regression: a 1h Teams recording (~1,350 fragments) took 19.5 minutes because
yt-dlp fetched one fragment at a time.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import downloader, teams_downloader


def _fake_proc():
    proc = MagicMock()
    proc.stdout = iter([])
    proc.returncode = 0
    proc.poll.return_value = 0
    return proc


def _fragments_arg(args):
    return int(args[args.index("--concurrent-fragments") + 1])


class TestConcurrentFragments(unittest.TestCase):
    def test_teams_download_fetches_fragments_in_parallel(self):
        captured = {}

        def record(args, **kwargs):
            captured["args"] = args
            return _fake_proc()

        with patch("subprocess.Popen", side_effect=record):
            teams_downloader._run_download("u", "/tmp/o.%(ext)s", "/x", "edge")
        self.assertGreater(_fragments_arg(captured["args"]), 1)

    @patch("pipeline.downloader._download_subtitles")
    @patch("pipeline.downloader._find_output", return_value="/tmp/out/video.mp4")
    @patch("pipeline.downloader._run_json", return_value={"title": "t"})
    def test_youtube_download_fetches_fragments_in_parallel(self, *_):
        captured = []

        def record(args, **kwargs):
            captured.append(args)
            return _fake_proc()

        with patch("subprocess.Popen", side_effect=record):
            downloader.download_youtube("https://youtu.be/x", "/tmp/out")
        self.assertGreater(_fragments_arg(captured[0]), 1)


if __name__ == "__main__":
    unittest.main()

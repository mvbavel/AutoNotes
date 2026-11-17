"""End-to-end flows of the download and note-generation stages, with the
child processes and the Claude client faked at their boundaries."""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

from pipeline import downloader, note_generator as ng, teams_downloader
from pipeline._util import PipelineCancelled

NOTES_JSON = json.dumps({
    "title": "Planning review",
    "chapters": [{"title": "Budget", "start_time": 0, "end_time": 30,
                  "speakers": ["Ana"],
                  "key_points": [{"text": "Budget **approved**", "screenshot_idx": 1}]}],
    "screenshots": {"1": {"content_box": [0, 0, 1, 1]}},
})


class _Stream:
    def __init__(self, text):
        self._message = types.SimpleNamespace(
            stop_reason="end_turn", content=[types.SimpleNamespace(type="text", text=text)])

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        return iter([1, 2])

    def get_final_message(self):
        return self._message


class TestGenerateNotes(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = ng._DEBUG_DIR, ng._RETRY_BASE_WAIT
        ng._DEBUG_DIR, ng._RETRY_BASE_WAIT = self._tmp.name, 0
        self.image = os.path.join(self._tmp.name, "f.jpg")
        with open(self.image, "wb") as f:
            f.write(b"\xff\xd8\xff\xe0jpeg")
        self.requests = []

    def tearDown(self):
        ng._DEBUG_DIR, ng._RETRY_BASE_WAIT = self._orig
        self._tmp.cleanup()

    def _client(self, replies):
        replies = list(replies)
        client = MagicMock()

        def stream(**kwargs):
            self.requests.append(kwargs)
            return _Stream(replies.pop(0))
        client.messages.stream.side_effect = stream
        return patch.object(anthropic, "Anthropic", return_value=client)

    def test_single_call(self):
        segments = [{"start": 0, "end": 5, "speaker": "Ana", "text": "Budget is approved."}]
        frames = [{"path": self.image, "timestamp": 3}]
        progress = []
        with self._client([NOTES_JSON]):
            notes = ng.generate_notes(segments, frames, "Video", "sk",
                                      progress_cb=progress.append,
                                      description="About the plan", attendees=["Ana", "Ben"],
                                      ai_notes="Recap", yt_chapters=[{"start_time": 0,
                                                                     "title": "Intro"}])
        self.assertEqual(notes["title"], "Planning review")
        self.assertEqual(notes["screenshot_boxes"], {1: [0.0, 0.0, 1.0, 1.0]})
        self.assertEqual(progress[-1], 100)
        content = self.requests[0]["messages"][0]["content"]
        header = content[0]["text"]
        for expected in ("ATTENDEES: Ana, Ben", "DESCRIPTION", "YOUTUBE CHAPTERS",
                         "TEAMS AI NOTES", "[0:00] Ana: Budget is approved."):
            self.assertIn(expected, header)
        self.assertEqual(sum(1 for b in content if b["type"] == "image"), 1)
        self.assertEqual(self.requests[0]["model"], ng.MODEL)

    def test_long_transcript_is_split_in_two(self):
        segments = [{"start": i * 10, "end": i * 10 + 9, "speaker": "Ana", "text": "word " * 200}
                    for i in range(120)]
        frames = [{"path": self.image, "timestamp": 10}, {"path": self.image, "timestamp": 1000}]
        second = json.dumps({"title": "x", "chapters": [
            {"title": "Later", "key_points": [{"text": "y", "screenshot_idx": 2}]}]})
        with self._client([NOTES_JSON, second]), patch.object(ng, "_interruptible_sleep"):
            notes = ng.generate_notes(segments, frames, "Video", "sk")
        self.assertEqual(len(self.requests), 2)
        self.assertEqual([c["title"] for c in notes["chapters"]], ["Budget", "Later"])
        self.assertEqual(notes["title"], "Planning review")
        self.assertNotIn("degraded", notes)

    def test_one_bad_half_is_flagged(self):
        segments = [{"start": i * 10, "end": i * 10 + 9, "speaker": "Ana", "text": "word " * 200}
                    for i in range(120)]
        with self._client([NOTES_JSON, "garbage"]), patch.object(ng, "_interruptible_sleep"):
            notes = ng.generate_notes(segments, [], "Video", "sk")
        self.assertIn("for part of the recording", notes["degraded"])

    def test_cancel_during_stream(self):
        def cancel_check():
            raise PipelineCancelled()
        with self._client([NOTES_JSON]):
            with self.assertRaises(PipelineCancelled):
                ng.generate_notes([{"start": 0, "end": 1, "text": "x"}], [], "V", "sk",
                                  cancel_check=cancel_check)


def _streaming(lines, code=0, make_file=None):
    """A run_streaming stand-in that replays `lines` and optionally writes a file."""
    calls = []

    def fake(cmd, on_line=None, cancel_check=None):
        calls.append(cmd)
        if make_file:
            open(make_file, "w").close()
        for line in lines:
            if on_line:
                on_line(line + "\n")
        return code
    return fake, calls


class TestYouTubeDownload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_happy_path_reports_throttled_progress(self):
        info = {"title": "100% Proof", "description": "d",
                "chapters": [{"start_time": 0, "title": "Intro"}],
                "automatic_captions": {"en": []}}
        lines = [f"[download]  {p}.0% of 10MiB at 2.0MiB/s ETA 00:0{9 - p // 12}"
                 for p in range(0, 101, 1)]
        fake, calls = _streaming(lines, make_file=os.path.join(self.dir, "100% Proof.mp4"))
        logs, progress = [], []
        with patch.object(downloader, "_run_json", return_value=info), \
             patch.object(downloader, "run_streaming", side_effect=fake):
            path, title, desc, chapters = downloader.download_youtube(
                "https://youtu.be/x", self.dir, progress_cb=progress.append, log_cb=logs.append)
        self.assertTrue(path.endswith("100% Proof.mp4"))
        self.assertEqual((title, desc, len(chapters)), ("100% Proof", "d", 1))
        # One log line per 5%, not one per percent as before
        self.assertLessEqual(sum("Downloading" in m for m in logs), 22)
        self.assertEqual(max(progress), 95)
        self.assertIn("--sub-langs", calls[1])
        self.assertEqual(calls[1][calls[1].index("--sub-langs") + 1], "en")

    def test_no_file_produced(self):
        fake, _ = _streaming([])
        with patch.object(downloader, "_run_json", return_value={"title": "t"}), \
             patch.object(downloader, "run_streaming", side_effect=fake):
            with self.assertRaises(RuntimeError):
                downloader.download_youtube("https://youtu.be/x", self.dir)

    def test_metadata_timeout(self):
        import subprocess
        with patch.object(downloader, "run_capture",
                          side_effect=subprocess.TimeoutExpired("yt-dlp", 60)):
            with self.assertRaises(RuntimeError) as ctx:
                downloader._run_json(["yt-dlp"])
        self.assertIn("timed out", str(ctx.exception))


class TestTeamsDownload(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _download(self, write_video=True, vtt=None, fetched=None):
        def fake_run(url, template, ffmpeg_dir, browser, **kwargs):
            if write_video:
                open(os.path.join(self.dir, "Weekly sync.mp4"), "w").close()
            if vtt:
                with open(os.path.join(self.dir, "Weekly sync.en.vtt"), "w") as f:
                    f.write(vtt)
            return True
        with patch.object(teams_downloader, "_fetch_info",
                          return_value={"title": "Weekly sync", "duration": 60}), \
             patch.object(teams_downloader, "_run_download", side_effect=fake_run), \
             patch.object(teams_downloader, "fetch_transcript", return_value=fetched) as fetch:
            result = teams_downloader.download_teams_recording(
                "https://c.sharepoint.com/x", self.dir, log_cb=lambda m: None)
        return result, fetch

    def test_vtt_with_speakers_is_used(self):
        result, fetch = self._download(
            vtt="WEBVTT\n\n00:00:01.000 --> 00:00:03.000\n<v Ana>Hello</v>\n")
        self.assertEqual(result["transcript_segments"][0]["speaker"], "Ana")
        fetch.assert_not_called()

    def test_falls_back_to_stream_api_transcript(self):
        segs = [{"start": 0, "end": 1, "speaker": "Ben", "text": "Hi"}]
        result, fetch = self._download(fetched=segs)
        self.assertEqual(result["transcript_segments"], segs)
        self.assertTrue(result["video_path"].endswith("Weekly sync.mp4"))

    def test_no_video_is_an_error_not_a_crash_later(self):
        with self.assertRaises(RuntimeError) as ctx:
            self._download(write_video=False)
        self.assertIn("no video file", str(ctx.exception))

    def test_metadata_tries_each_browser(self):
        failed = types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: login required")
        ok = types.SimpleNamespace(returncode=0, stdout='{"title": "T"}', stderr="")
        logs = []
        with patch.object(teams_downloader, "run_capture", side_effect=[failed, ok]):
            info = teams_downloader._fetch_info("https://c.sharepoint.com/x", logs.append)
        self.assertEqual(info, {"title": "T"})
        self.assertIn("login required", logs[0])

    def test_metadata_cancel_propagates(self):
        with patch.object(teams_downloader, "run_capture", side_effect=PipelineCancelled()):
            with self.assertRaises(PipelineCancelled):
                teams_downloader._fetch_info("https://c.sharepoint.com/x")


if __name__ == "__main__":
    unittest.main()

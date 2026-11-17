"""Worker behaviour beyond per-stage details: what it saves, warns about, and
refuses — with every heavy stage patched out (no ffmpeg, Whisper or network)."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic

try:   # anthropic 1.x+ sits on httpx2 and no longer installs httpx
    import httpx2 as http
except ImportError:
    import httpx as http

import pipeline.worker as worker

SEGMENTS = [{"start": 0, "end": 2, "text": "Hello", "speaker": "Ana"}]
NOTES = {"title": "T", "chapters": [{"title": "C", "key_points": [{"text": "x"}]}]}


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.logs_dir = os.path.join(self.root, "logs")
        self.video = os.path.join(self.root, "Weekly sync.mp4")
        open(self.video, "w").close()
        self.patches = {
            "DEBUG_DIR": patch.object(worker, "DEBUG_DIR", self.logs_dir),
            "check_output_dir": patch.object(worker, "check_output_dir", return_value=None),
            "check_api_key": patch.object(worker, "check_api_key", return_value=None),
            "extract_audio": patch.object(worker, "extract_audio",
                                          return_value=os.path.join(self.root, "a.wav")),
            "transcribe": patch.object(worker, "transcribe",
                                       side_effect=lambda *a, **k: [dict(s) for s in SEGMENTS]),
            "diarize": patch.object(worker, "diarize", side_effect=lambda a, segs, tok, **k: segs),
            "extract_frames": patch.object(worker, "extract_frames", return_value=[]),
            "generate_notes": patch.object(worker, "generate_notes", return_value=dict(NOTES)),
            "write_docx": patch.object(worker, "write_docx",
                                       return_value=os.path.join(self.root, "out.docx")),
            "download_youtube": patch.object(worker, "download_youtube"),
        }
        self.mocks = {name: p.start() for name, p in self.patches.items()}

    def tearDown(self):
        for p in self.patches.values():
            p.stop()
        self._tmp.cleanup()

    def run_worker(self, source=None, **config):
        w = worker.ProcessingWorker(source or self.video,
                                    {"anthropic_key": "k", "output_dir": self.root, **config})
        events = {"warning": [], "log": [], "completed": []}
        w.warning.connect(events["warning"].append)
        w.log_message.connect(events["log"].append)
        w.completed.connect(events["completed"].append)
        w._run_pipeline(self.root)
        return events

    def save_transcript(self, video):
        os.makedirs(self.logs_dir, exist_ok=True)
        with open(os.path.join(self.logs_dir, "last_transcript.json"), "w") as f:
            json.dump({"video": video, "saved": "2026-11-01T10:00:00",
                       "segments": [{"start": 0, "end": 2, "text": "revenue figures"}]}, f)


class TestTranscriptReuse(WorkerCase):
    def test_reuse_does_not_relabel_the_saved_transcript(self):
        """Regression: reusing A's transcript for B rewrote the file as B's, so
        the different-recording warning never fired again."""
        self.save_transcript("Board meeting")
        first = self.run_worker(reuse_transcript=True)
        self.assertEqual(len(first["warning"]), 1)
        self.assertIn("Board meeting", first["warning"][0])

        saved, meta = worker.load_saved_transcript()
        self.assertEqual(meta["video"], "Board meeting")
        self.assertEqual(saved[0]["text"], "revenue figures")

        second = self.run_worker(reuse_transcript=True)
        self.assertEqual(len(second["warning"]), 1, "warning must fire every time")

    def test_same_recording_reuses_quietly(self):
        self.save_transcript("Weekly sync")
        events = self.run_worker(reuse_transcript=True)
        self.assertEqual(events["warning"], [])
        self.mocks["transcribe"].assert_not_called()

    def test_fresh_transcript_is_saved_owner_only(self):
        self.run_worker()
        path = os.path.join(self.logs_dir, "last_transcript.json")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.logs_dir).st_mode), 0o700)
        _, meta = worker.load_saved_transcript()
        self.assertEqual(meta["video"], "Weekly sync")


class TestPreflight(WorkerCase):
    def test_rejected_key_fails_before_downloading(self):
        request = http.Request("GET", "https://api.anthropic.com")
        self.mocks["check_api_key"].side_effect = anthropic.AuthenticationError(
            "bad key", response=http.Response(401, request=request), body=None)
        with self.assertRaises(anthropic.AuthenticationError):
            self.run_worker(source="https://www.youtube.com/watch?v=x")
        self.mocks["download_youtube"].assert_not_called()

    def test_missing_key(self):
        w = worker.ProcessingWorker(self.video, {"output_dir": self.root})
        with self.assertRaises(RuntimeError) as ctx:
            w._run_pipeline(self.root)
        self.assertIn("API key", str(ctx.exception))
        self.mocks["extract_audio"].assert_not_called()

    def test_check_note_is_logged(self):
        self.mocks["check_api_key"].return_value = "Couldn't check the API key ahead of time"
        events = self.run_worker()
        self.assertIn("Couldn't check the API key ahead of time", events["log"])


class TestFailuresAndWarnings(WorkerCase):
    def test_no_speech_is_a_clear_error(self):
        self.mocks["transcribe"].side_effect = lambda *a, **k: []
        with self.assertRaises(RuntimeError) as ctx:
            self.run_worker()
        self.assertIn("No speech", str(ctx.exception))
        self.mocks["generate_notes"].assert_not_called()

    def test_missing_local_file(self):
        with self.assertRaises(RuntimeError) as ctx:
            self.run_worker(source=os.path.join(self.root, "gone.mp4"))
        self.assertIn("no longer exists", str(ctx.exception))

    def test_degraded_notes_warn_the_user(self):
        self.mocks["generate_notes"].return_value = dict(NOTES, degraded="Claude's reply was cut off")
        events = self.run_worker()
        self.assertEqual(len(events["warning"]), 1)
        self.assertIn("transcript instead of AI notes", events["warning"][0])
        self.assertEqual(len(events["completed"]), 1, "the transcript is still saved")

    def test_document_metadata_names_the_site(self):
        self.mocks["download_youtube"].return_value = (self.video, "Talk", "About it", [])
        self.run_worker(source="https://vimeo.com/123")
        source_info = self.mocks["write_docx"].call_args.kwargs["source_info"]
        self.assertEqual(source_info["type"], "Vimeo")
        self.assertEqual(source_info["summary_label"], "Description")
        self.assertTrue(source_info["summary_is_description"])


class TestWebSourceType(unittest.TestCase):
    def test_names(self):
        cases = {
            "https://www.youtube.com/watch?v=x": "YouTube",
            "https://youtu.be/x": "YouTube",
            "https://sapvideo.cfapps.eu10.hana.ondemand.com/?entry_id=1_abcdefgh": "Kaltura",
            "https://vimeo.com/1": "Vimeo",
            "https://example.com/talk.mp4": "Web video",
        }
        for url, expected in cases.items():
            self.assertEqual(worker._web_source_type(url), expected, url)


class TestHousekeeping(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.logs_dir = os.path.join(self._tmp.name, "logs")
        self._patch = patch.object(worker, "DEBUG_DIR", self.logs_dir)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_log_rotates_and_is_owner_only(self):
        with patch.object(worker, "_LOG_MAX_BYTES", 200):
            for i in range(20):
                worker.append_log(f"line {i} " + "x" * 20)
        log = worker.log_path()
        self.assertTrue(os.path.exists(log + ".1"))
        self.assertLess(os.path.getsize(log), 400)
        self.assertEqual(stat.S_IMODE(os.stat(log).st_mode), 0o600)

    def test_clear_saved_data(self):
        worker.append_log("something")
        with patch("pipeline.graph_client.clear_token_cache") as clear_tokens:
            worker.clear_saved_data()
        self.assertFalse(os.path.exists(self.logs_dir))
        clear_tokens.assert_called_once()

    def test_sweep_removes_only_old_pipeline_dirs(self):
        with patch.object(worker.tempfile, "gettempdir", return_value=self._tmp.name):
            old = os.path.join(self._tmp.name, "autonotes_old")
            new = os.path.join(self._tmp.name, "autonotes_new")
            other = os.path.join(self._tmp.name, "someone_else")
            for d in (old, new, other):
                os.makedirs(d)
            past = time.time() - 7 * 3600
            os.utime(old, (past, past))
            os.utime(other, (past, past))
            self.assertEqual(worker.sweep_stale_temp_dirs(), 1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(new))
        self.assertTrue(os.path.exists(other))


if __name__ == "__main__":
    unittest.main()

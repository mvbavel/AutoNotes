"""Audio extraction, transcription, diarization cancel and frame extraction.

The frame tests run the real ffmpeg + OpenCV path on a few seconds of
synthetic video (ffmpeg is a hard dependency, installed in CI too).
"""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import diarizer, frame_extractor, sharepoint_transcript as sp, transcriber
from pipeline._paths import FFMPEG
from pipeline._util import PipelineCancelled

HAVE_FFMPEG = os.path.isfile(FFMPEG) or shutil.which("ffmpeg") is not None


def _fake_ffmpeg(directory: str, stderr: str, code: int) -> str:
    """A stand-in ffmpeg that prints `stderr` and exits with `code`."""
    path = os.path.join(directory, "ffmpeg")
    with open(path, "w") as f:
        f.write(f"#!/bin/sh\necho '{stderr}' >&2\nexit {code}\n")
    os.chmod(path, 0o755)
    return path


class TestExtractAudio(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_recording_without_audio_explains(self):
        fake = _fake_ffmpeg(self.dir, "Output file does not contain any stream", 1)
        with patch.object(transcriber, "FFMPEG", fake):
            with self.assertRaises(RuntimeError) as ctx:
                transcriber.extract_audio("v.mp4", self.dir)
        self.assertIn("no audio track", str(ctx.exception))

    def test_other_failures_carry_ffmpeg_text(self):
        fake = _fake_ffmpeg(self.dir, "moov atom not found", 1)
        with patch.object(transcriber, "FFMPEG", fake):
            with self.assertRaises(RuntimeError) as ctx:
                transcriber.extract_audio("v.mp4", self.dir)
        self.assertIn("moov atom not found", str(ctx.exception))

    def test_success(self):
        fake = _fake_ffmpeg(self.dir, "", 0)
        progress = []
        with patch.object(transcriber, "FFMPEG", fake):
            path = transcriber.extract_audio("v.mp4", self.dir, progress_cb=progress.append)
        self.assertEqual(path, os.path.join(self.dir, "audio.wav"))
        self.assertEqual(progress, [100])


class _Segment:
    def __init__(self, start, end, text):
        self.start, self.end, self.text = start, end, text


class TestTranscribe(unittest.TestCase):
    def _run(self, segments, **kwargs):
        model = MagicMock()
        model.transcribe.return_value = (iter(segments), types.SimpleNamespace(duration=100.0))
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = MagicMock(return_value=model)
        with patch.dict(sys.modules, {"faster_whisper": fake}), \
             patch.object(transcriber, "_model_is_cached", return_value=True), \
             patch.object(transcriber.time, "monotonic", side_effect=iter(range(0, 10_000, 5))):
            return transcriber.transcribe("a.wav", model_size="small", **kwargs)

    def test_segments_progress_and_eta(self):
        progress, etas = [], []
        out = self._run([_Segment(0, 40, " One "), _Segment(40, 100, "Two")],
                        progress_cb=progress.append, eta_cb=etas.append, log_cb=lambda m: None)
        self.assertEqual([s["text"] for s in out], ["One", "Two"])
        self.assertEqual(progress[-1], 100)
        self.assertTrue(etas and etas[0].endswith("left"), etas)

    def test_cancel_between_segments(self):
        def cancel_check():
            raise PipelineCancelled()
        with self.assertRaises(PipelineCancelled):
            self._run([_Segment(0, 1, "x")], cancel_check=cancel_check)

    def test_model_download_failure_explains(self):
        fake = types.ModuleType("faster_whisper")
        fake.WhisperModel = MagicMock(side_effect=OSError("offline"))
        with patch.dict(sys.modules, {"faster_whisper": fake}), \
             patch.object(transcriber, "_model_is_cached", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                transcriber.transcribe("a.wav", model_size="large-v3", log_cb=lambda m: None)
        self.assertIn("download failed", str(ctx.exception))


class TestDiarizeCancel(unittest.TestCase):
    def test_cancel_inside_pyannote_stops_the_run(self):
        """Regression: Cancel did nothing for the whole of a diarization, and
        the catch-all turned any exception into 'continuing without labels'."""
        def pipeline_call(audio_path, hook=None):
            hook("segmentation", None, total=10, completed=3)
            raise AssertionError("cancel should have stopped pyannote")

        pipeline = MagicMock(side_effect=pipeline_call)
        pipeline.to.return_value = pipeline
        fake_audio = types.ModuleType("pyannote.audio")
        fake_audio.Pipeline = MagicMock(from_pretrained=MagicMock(return_value=pipeline))
        fake_torch = types.ModuleType("torch")
        fake_torch.device = lambda name: name

        def cancel_check():
            raise PipelineCancelled()

        with patch.dict(sys.modules, {"pyannote.audio": fake_audio, "torch": fake_torch}):
            with self.assertRaises(PipelineCancelled):
                diarizer.diarize("a.wav", [{"start": 0, "end": 1, "text": "x"}], "hf_x",
                                 cancel_check=cancel_check)


@unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not installed")
class TestExtractFrames(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _video(self, seconds=40, audio_only=False):
        path = os.path.join(self.dir, "a.m4a" if audio_only else "v.mp4")
        src = (["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"] if audio_only
               else ["-f", "lavfi", "-i", f"testsrc2=size=640x360:rate=5:duration={seconds}"])
        subprocess.run([FFMPEG, "-y", "-loglevel", "error", *src, path], check=True)
        return path

    def test_selects_frames_and_writes_api_copies(self):
        logs = []
        frames = frame_extractor.extract_frames(self._video(), self.dir, log_cb=logs.append)
        self.assertGreater(len(frames), 0)
        self.assertEqual([f["timestamp"] for f in frames], sorted(f["timestamp"] for f in frames))
        for f in frames:
            self.assertTrue(os.path.isfile(f["path"]))
            self.assertTrue(os.path.isfile(f["api_path"]))
            self.assertFalse(frame_extractor._needs_jfif_rewrite(f["path"]))
        self.assertEqual(logs, [])

    def test_audio_only_explains_missing_screenshots(self):
        logs = []
        frames = frame_extractor.extract_frames(self._video(audio_only=True), self.dir,
                                                log_cb=logs.append)
        self.assertEqual(frames, [])
        self.assertTrue(any("without screenshots" in m for m in logs), logs)

    def test_unreadable_file_explains(self):
        bad = os.path.join(self.dir, "bad.mp4")
        with open(bad, "w") as f:
            f.write("not a video")
        logs = []
        self.assertEqual(frame_extractor.extract_frames(bad, self.dir, log_cb=logs.append), [])
        self.assertTrue(any("without screenshots" in m for m in logs), logs)

    def test_cancel(self):
        def cancel_check():
            raise PipelineCancelled()
        with self.assertRaises(PipelineCancelled):
            frame_extractor.extract_frames(self._video(), self.dir, cancel_check=cancel_check)


class TestFrameScoring(unittest.TestCase):
    def test_slide_scores_above_noise(self):
        import cv2
        import numpy as np
        slide = np.full((360, 640, 3), 255, np.uint8)
        for y in range(60, 300, 30):
            cv2.putText(slide, "Quarterly roadmap item", (40, y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 2)
        # Camera footage is smooth (not white noise, which is all edges)
        noise = np.random.default_rng(0).integers(0, 255, (360, 640, 3), dtype=np.uint8)
        camera = cv2.normalize(cv2.GaussianBlur(noise, (0, 0), 6), None, 0, 255,
                               cv2.NORM_MINMAX)
        self.assertGreater(frame_extractor._score_image(cv2, slide),
                           frame_extractor._score_image(cv2, camera) + 0.2)

    def test_projected_screen_is_detected(self):
        import cv2
        import numpy as np
        room = np.full((720, 1280, 3), 60, np.uint8)
        cv2.rectangle(room, (200, 120), (1080, 615), (250, 250, 250), -1)
        quad = frame_extractor._detect_screen_quad(cv2, room)
        self.assertIsNotNone(quad)
        crop = frame_extractor._warp_crop(cv2, room, quad)
        self.assertGreater(crop.shape[1], 800)


class TestSharePointFlow(unittest.TestCase):
    def test_transcript_with_speakers(self):
        page = MagicMock(url="https://c.sharepoint.com/teams/x/stream.aspx",
                         text='g_fileInfo = {".spItemUrl": "https://c.sharepoint.com/teams/x/'
                              '_api/v2.0/drives/d/items/i", "hasTranscripts": true};')
        meta = MagicMock(status_code=200)
        meta.json.return_value = {"media": {"transcripts": [
            {"isDefault": True, "languageTag": "en-us",
             "temporaryDownloadUrl": "https://c.sharepoint.com/t?sig=1"}]}}
        body = MagicMock(status_code=200)
        body.json.return_value = {"entries": [
            {"startOffset": "00:00:01.0000000", "endOffset": "00:00:03.0000000",
             "speakerDisplayName": "Ana", "text": "Hello"},
            {"startOffset": "00:00:09.0000000", "endOffset": "00:00:10.0000000",
             "speakerDisplayName": "Ben", "text": "Hi"}]}
        session = MagicMock()
        session.get.side_effect = [page, meta, body]
        segments = sp._fetch_with_session(session, page.url)
        self.assertEqual([(s["speaker"], s["text"]) for s in segments],
                         [("Ana", "Hello"), ("Ben", "Hi")])
        self.assertTrue(session.get.call_args_list[2].args[0].endswith("&format=json"))

    def test_sign_in_redirect(self):
        session = MagicMock()
        session.get.return_value = MagicMock(url="https://login.microsoftonline.com/x")
        self.assertIsNone(sp._fetch_with_session(session, "https://c.sharepoint.com/x"))

    def test_no_transcript(self):
        session = MagicMock()
        session.get.return_value = MagicMock(url="https://c.sharepoint.com/x",
                                             text='g_fileInfo = {"hasTranscripts": false};')
        self.assertIsNone(sp._fetch_with_session(session, "https://c.sharepoint.com/x"))


if __name__ == "__main__":
    unittest.main()

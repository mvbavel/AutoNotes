"""The worker reports a one-line fact per stage, and which stages it skipped.

Every heavy pipeline call is patched, so this runs the real stage sequencing
in-process with no ffmpeg, Whisper, network or Claude.
"""
import os
import tempfile
import unittest
from unittest.mock import patch

import pipeline.worker as worker

_SEGMENTS = [
    {"start": 0, "end": 2, "text": "Hello", "speaker": "Ana"},
    {"start": 2, "end": 4, "text": "Hi", "speaker": "Ben"},
    {"start": 4, "end": 6, "text": "Agenda", "speaker": "Ana"},
]
_FRAMES = [{"path": "/x/1.jpg", "timestamp": 10}, {"path": "/x/2.jpg", "timestamp": 40}]
_NOTES = {"title": "T", "chapters": [{"key_points": []}, {"key_points": []}]}


class TestStageDetails(unittest.TestCase):
    def run_worker(self, config, saved=([], {})):
        tmp = tempfile.mkdtemp()
        video = os.path.join(tmp, "Weekly sync.mp4")
        open(video, "w").close()
        w = worker.ProcessingWorker(video, {"anthropic_key": "k", "output_dir": tmp, **config})
        details = {}
        w.stage_detail.connect(lambda n, text, skipped: details.__setitem__(n, (text, skipped)))
        patches = [
            patch.object(worker, "DEBUG_DIR", tmp),
            patch.object(worker, "check_output_dir", return_value=None),
            patch.object(worker, "extract_audio", return_value=os.path.join(tmp, "a.wav")),
            patch.object(worker, "transcribe", return_value=[dict(s) for s in _SEGMENTS]),
            patch.object(worker, "diarize", side_effect=lambda a, segs, tok, **k: segs),
            patch.object(worker, "extract_frames", return_value=_FRAMES),
            patch.object(worker, "generate_notes", return_value=_NOTES),
            patch.object(worker, "write_docx", return_value=os.path.join(tmp, "Weekly sync.docx")),
            patch.object(worker, "load_saved_transcript", return_value=saved),
            patch.object(worker.ProcessingWorker, "_save_debug_frames"),
        ]
        for p in patches:
            p.start()
        try:
            w._run_pipeline(tmp)
        finally:
            for p in patches:
                p.stop()
        return details

    def test_local_file_without_token(self):
        d = self.run_worker({"whisper_model": "small"})
        self.assertEqual(d[1], ("Local file", False))
        self.assertFalse(d.get(2, ("", False))[1])  # audio really extracted
        self.assertIn("3 lines", d[3][0])
        # No token: diarization did nothing, and the row should say how to enable it
        text, skipped = d[4]
        self.assertTrue(skipped)
        self.assertIn("Hugging Face", text)
        self.assertEqual(d[5], ("2 screenshots", False))
        self.assertEqual(d[6], ("2 chapters", False))
        self.assertTrue(d[7][0].startswith("Saved to "), d[7])

    def test_with_token_counts_speakers(self):
        d = self.run_worker({"hf_token": "hf_x"})
        self.assertEqual(d[4], ("2 speakers", False))

    def test_reused_transcript_skips_audio_whisper_and_speakers(self):
        d = self.run_worker({"reuse_transcript": True},
                            saved=([dict(s) for s in _SEGMENTS], {"video": "Weekly sync"}))
        for n in (2, 3, 4):
            self.assertTrue(d[n][1], f"stage {n} should be skipped: {d[n]}")
        self.assertIn("last run", d[3][0])
        self.assertIn("3 lines", d[3][0])


if __name__ == "__main__":
    unittest.main()

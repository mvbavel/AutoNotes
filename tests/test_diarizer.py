"""Diarization against the pyannote.audio 4.x API.

Regression: 4.x renamed from_pretrained(use_auth_token=) to token= and returns
a DiarizeOutput instead of an Annotation. Both raised inside diarize()'s
catch-all, so every run silently fell back to a single "Speaker".
"""
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.diarizer import diarize


class _Turn:
    def __init__(self, start, end):
        self.start, self.end = start, end


class _Annotation:
    def __init__(self, tracks):
        self._tracks = tracks

    def itertracks(self, yield_label=False):
        for start, end, label in self._tracks:
            yield _Turn(start, end), None, label


class _Pipeline:
    """Strict stand-in for pyannote 4.x Pipeline: rejects the 3.x kwarg."""
    last_kwargs = None

    def __init__(self, output):
        self._output = output

    @classmethod
    def factory(cls, output):
        def from_pretrained(checkpoint, *, token=None, revision=None, cache_dir=None):
            cls.last_kwargs = {"token": token}
            return cls(output)
        return from_pretrained

    def to(self, device):
        return self

    def __call__(self, audio_path, **kwargs):
        return self._output


SEGMENTS = [
    {"start": 0.0, "end": 4.0, "text": "hello"},
    {"start": 5.0, "end": 9.0, "text": "hi there"},
]


def _run(output):
    pipeline_cls = MagicMock()
    pipeline_cls.from_pretrained = _Pipeline.factory(output)
    fake_audio = types.ModuleType("pyannote.audio")
    fake_audio.Pipeline = pipeline_cls
    fake_torch = types.ModuleType("torch")
    fake_torch.device = lambda name: name
    logs = []
    with patch.dict(sys.modules, {"pyannote.audio": fake_audio, "torch": fake_torch}):
        result = diarize("a.wav", SEGMENTS, "hf_x", log_cb=logs.append)
    return result, logs


class TestDiarize(unittest.TestCase):
    def test_pyannote4_output_labels_speakers(self):
        annotation = _Annotation([(0.0, 4.5, "SPK_1"), (4.5, 10.0, "SPK_2")])
        output = types.SimpleNamespace(speaker_diarization=annotation,
                                       exclusive_speaker_diarization=annotation)
        result, logs = _run(output)
        self.assertEqual([s["speaker"] for s in result], ["Speaker A", "Speaker B"])
        self.assertEqual(_Pipeline.last_kwargs["token"], "hf_x")
        self.assertFalse(any("failed" in m for m in logs), logs)

    def test_bare_annotation_output_still_supported(self):
        result, _ = _run(_Annotation([(0.0, 10.0, "SPK_1")]))
        self.assertEqual({s["speaker"] for s in result}, {"Speaker A"})

    def test_no_token_skips_pyannote(self):
        result = diarize("a.wav", SEGMENTS, None)
        self.assertEqual({s["speaker"] for s in result}, {"Speaker"})


if __name__ == "__main__":
    unittest.main()

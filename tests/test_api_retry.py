"""Tests for _call_with_retry: mid-stream network drops must be retried."""
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

from pipeline import note_generator


class _FakeStream:
    """Context manager mimicking client.messages.stream()."""

    def __init__(self, fail_with=None, message="ok"):
        self._fail_with = fail_with
        self._message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def __iter__(self):
        if self._fail_with is not None:
            raise self._fail_with
        return iter(())

    def get_final_message(self):
        return self._message


class _FakeClient:
    """Yields the queued stream behaviours in order, one per attempt."""

    def __init__(self, behaviours):
        self._behaviours = list(behaviours)
        self.attempts = 0
        self.messages = types.SimpleNamespace(stream=self._stream)

    def _stream(self, **kwargs):
        self.attempts += 1
        return self._behaviours.pop(0)


class TestCallWithRetry(unittest.TestCase):
    def setUp(self):
        self._orig_wait = note_generator._RETRY_BASE_WAIT
        note_generator._RETRY_BASE_WAIT = 0  # no real sleeping in tests

    def tearDown(self):
        note_generator._RETRY_BASE_WAIT = self._orig_wait

    def test_mid_stream_read_error_is_retried(self):
        client = _FakeClient([
            _FakeStream(fail_with=httpx.ReadError("connection reset by peer")),
            _FakeStream(message="recovered"),
        ])
        logs = []
        result = note_generator._call_with_retry(client, [], log_cb=logs.append)
        self.assertEqual(result, "recovered")
        self.assertEqual(client.attempts, 2)
        self.assertTrue(any("retrying" in m for m in logs))

    def test_persistent_failure_raises_after_max_retries(self):
        client = _FakeClient([
            _FakeStream(fail_with=httpx.ReadError("reset"))
            for _ in range(note_generator.MAX_RETRIES)
        ])
        logs = []
        with self.assertRaises(httpx.ReadError):
            note_generator._call_with_retry(client, [], log_cb=logs.append)
        self.assertEqual(client.attempts, note_generator.MAX_RETRIES)
        self.assertTrue(any("failed after" in m for m in logs))

    def test_non_retryable_error_propagates_immediately(self):
        client = _FakeClient([_FakeStream(fail_with=ValueError("boom"))])
        with self.assertRaises(ValueError):
            note_generator._call_with_retry(client, [])
        self.assertEqual(client.attempts, 1)


if __name__ == "__main__":
    unittest.main()

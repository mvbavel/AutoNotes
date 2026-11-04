"""Pipeline failures are explained as cause + fix, not raw exception strings."""
import unittest

from ui.messages import explain_error, format_duration


class TestExplainError(unittest.TestCase):
    def test_curated_runtime_error_drops_class_name(self):
        msg = explain_error(
            "RuntimeError: Can't save to the output folder /x (Permission denied). "
            "Choose another folder.", "downloading the recording")
        self.assertTrue(msg.startswith("Can't save to the output folder"), msg)
        self.assertNotIn("RuntimeError", msg)

    def test_ytdlp_failure_with_hint_keeps_the_hint(self):
        msg = explain_error(
            "RuntimeError: yt-dlp exited with code 1. YouTube refused the download "
            "(HTTP 403). This usually means the bundled yt-dlp is 90 days old — "
            "install a newer AutoNotes build.", "downloading the recording")
        self.assertIn("install a newer AutoNotes build", msg)
        self.assertNotIn("exited with code", msg)

    def test_ytdlp_failure_without_hint_says_what_to_check(self):
        msg = explain_error("RuntimeError: yt-dlp exited with code 1", "downloading the recording")
        self.assertNotIn("exited with code", msg)
        self.assertIn("link", msg.lower())

    def test_bad_api_key(self):
        msg = explain_error(
            "AuthenticationError: Error code: 401 - {'type': 'error', 'error': "
            "{'type': 'authentication_error', 'message': 'invalid x-api-key'}}",
            "writing notes with Claude")
        self.assertIn("API key", msg)
        self.assertNotIn("401", msg)

    def test_out_of_credit(self):
        msg = explain_error(
            "BadRequestError: Error code: 400 - Your credit balance is too low to "
            "access the Anthropic API.", "writing notes with Claude")
        self.assertIn("credit", msg)
        self.assertNotIn("BadRequestError", msg)

    def test_overloaded_points_at_reusing_the_transcript(self):
        msg = explain_error("OverloadedError: Error code: 529 - overloaded",
                            "writing notes with Claude")
        self.assertIn("Reuse", msg)

    def test_connection_error(self):
        msg = explain_error("APIConnectionError: Connection error.", "writing notes with Claude")
        self.assertIn("internet", msg.lower())

    def test_certificate_error(self):
        msg = explain_error(
            "SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed",
            "downloading the recording")
        self.assertIn("secure connection", msg)

    def test_missing_ffmpeg(self):
        msg = explain_error(
            "FileNotFoundError: [Errno 2] No such file or directory: '/opt/homebrew/bin/ffmpeg'",
            "extracting audio")
        self.assertIn("brew install ffmpeg", msg)

    def test_unknown_error_names_the_stage_not_the_exception(self):
        msg = explain_error("KeyError: 'chapters'", "writing notes with Claude")
        self.assertNotIn("KeyError", msg)
        self.assertIn("writing notes with claude", msg.lower())
        self.assertIn("log", msg.lower())


class TestFormatDuration(unittest.TestCase):
    def test_formats(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(42.7), "42s")
        self.assertEqual(format_duration(108), "1m 48s")
        self.assertEqual(format_duration(3725), "1h 02m")


if __name__ == "__main__":
    unittest.main()

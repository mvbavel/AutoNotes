"""Tests for yt-dlp staleness detection and download-failure diagnosis.

The regression these guard against is concrete: on 2026-09-16 the installed
yt-dlp was 2026.07.04, requirements.txt asked for >=2026.7.4, and every YouTube
download returned 403. The floor was satisfied the whole time.
"""
import datetime
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline.ytdlp_health import (
    STALE_AFTER_DAYS,
    age_days,
    diagnose,
    installed_version,
    release_date,
    staleness_warning,
)

# The exact conditions of the failure this module exists to catch.
_BROKEN_VERSION = "2026.07.04"
_BROKEN_ON = datetime.date(2026, 9, 16)


class TestReleaseDate(unittest.TestCase):
    def test_version_string_is_a_release_date(self):
        self.assertEqual(release_date("2026.08.19"), datetime.date(2026, 8, 19))

    def test_suffixed_nightly_version_still_parses(self):
        self.assertEqual(release_date("2026.08.19.232734"), datetime.date(2026, 8, 19))

    def test_unparseable_version_yields_none(self):
        for v in ("", None, "unknown", "1.2.3", "2026.13.99"):
            self.assertIsNone(release_date(v), v)


class TestStaleness(unittest.TestCase):
    def test_the_403_regression_is_flagged(self):
        """The reproduction: 2026.07.04 in use on 2026-09-16."""
        warning = staleness_warning(_BROKEN_VERSION, today=_BROKEN_ON)
        self.assertIsNotNone(warning)
        self.assertIn("74 days", warning)
        self.assertIn(_BROKEN_VERSION, warning)

    def test_a_version_floor_would_have_missed_it(self):
        """Why this module measures age instead of comparing to a floor.

        requirements.txt asked for >=2026.7.4 and 2026.07.04 met it exactly, so
        a floor check passed while every download failed. Kept as a test so the
        floor approach is not reintroduced as a "simplification".
        """
        floor_ok = release_date(_BROKEN_VERSION) >= datetime.date(2026, 7, 4)
        self.assertTrue(floor_ok, "the broken version satisfied the floor")
        self.assertIsNotNone(staleness_warning(_BROKEN_VERSION, today=_BROKEN_ON))

    def test_fresh_version_is_silent(self):
        fresh = datetime.date(2026, 8, 19) + datetime.timedelta(days=3)
        self.assertIsNone(staleness_warning("2026.08.19", today=fresh))

    def test_threshold_boundary(self):
        base = datetime.date(2026, 8, 19)
        day_before = base + datetime.timedelta(days=STALE_AFTER_DAYS - 1)
        on_day = base + datetime.timedelta(days=STALE_AFTER_DAYS)
        self.assertIsNone(staleness_warning("2026.08.19", today=day_before))
        self.assertIsNotNone(staleness_warning("2026.08.19", today=on_day))

    def test_unparseable_version_never_warns(self):
        """A packaging change must not produce a permanent false alarm."""
        self.assertIsNone(staleness_warning("unknown", today=_BROKEN_ON))

    def test_missing_yt_dlp_is_silent(self):
        with patch("pipeline.ytdlp_health.installed_version", return_value=None):
            self.assertIsNone(staleness_warning())
            self.assertIsNone(age_days())

    def test_installed_version_matches_the_package_that_runs(self):
        import yt_dlp.version
        self.assertEqual(installed_version(), yt_dlp.version.__version__)


class TestDiagnose(unittest.TestCase):
    def test_403_is_attributed_to_staleness(self):
        msg = diagnose(["ERROR: unable to download video data: HTTP Error 403: Forbidden"],
                       version=_BROKEN_VERSION, today=_BROKEN_ON)
        self.assertIsNotNone(msg)
        self.assertIn("403", msg)
        self.assertIn("74 days", msg)

    def test_format_unavailable_is_recognised(self):
        msg = diagnose(["ERROR: Requested format is not available"],
                       version=_BROKEN_VERSION, today=_BROKEN_ON)
        self.assertIsNotNone(msg)

    def test_signature_extraction_is_recognised(self):
        msg = diagnose(["WARNING: nsig extraction failed: Some formats may be missing"],
                       version=_BROKEN_VERSION, today=_BROKEN_ON)
        self.assertIsNotNone(msg)

    def test_bot_check_is_recognised(self):
        msg = diagnose(["ERROR: Sign in to confirm you're not a bot"],
                       version=_BROKEN_VERSION, today=_BROKEN_ON)
        self.assertIsNotNone(msg)

    def test_unrelated_failure_is_not_blamed_on_the_version(self):
        """A full disk must not be reported as a stale extractor."""
        self.assertIsNone(diagnose(["ERROR: No space left on device"],
                                   version=_BROKEN_VERSION, today=_BROKEN_ON))

    def test_diagnosis_works_without_a_usable_version(self):
        """yt_dlp missing or unparseable: still explain, just without the age."""
        with patch("pipeline.ytdlp_health.installed_version", return_value=None):
            msg = diagnose(["ERROR: HTTP Error 403: Forbidden"], today=_BROKEN_ON)
        self.assertIsNotNone(msg)
        self.assertIn("403", msg)
        self.assertNotIn("days old", msg)

    def test_empty_output_yields_nothing(self):
        self.assertIsNone(diagnose([], version=_BROKEN_VERSION, today=_BROKEN_ON))


class TestDownloaderIntegration(unittest.TestCase):
    """The diagnosis is only worth anything if the failure path actually calls it."""

    @staticmethod
    def _fake_proc(returncode, lines):
        from unittest.mock import MagicMock
        proc = MagicMock()
        proc.stdout = iter(lines)
        proc.returncode = returncode
        proc.poll.return_value = returncode
        return proc

    def _run_failing_download(self, lines):
        from pipeline import downloader
        with patch.object(downloader, "_run_json",
                          return_value={"title": "t", "automatic_captions": {}}), \
             patch.object(downloader.subprocess, "Popen",
                          return_value=self._fake_proc(1, lines)):
            with self.assertRaises(RuntimeError) as ctx:
                downloader.download_youtube("https://youtu.be/x", "/tmp")
        return str(ctx.exception)

    def test_403_failure_explains_itself(self):
        msg = self._run_failing_download(
            ["[youtube] Extracting URL",
             "ERROR: unable to download video data: HTTP Error 403: Forbidden"]
        )
        self.assertIn("exited with code 1", msg)
        self.assertIn("403", msg)
        self.assertIn("yt-dlp", msg)

    def test_unrelated_failure_keeps_the_bare_error(self):
        msg = self._run_failing_download(["ERROR: No space left on device"])
        self.assertEqual(msg, "yt-dlp exited with code 1")


class TestUiIntegration(unittest.TestCase):
    def test_startup_warning_reaches_the_log(self):
        """Proves the UI hook is wired, without constructing a real MainWindow."""
        import ui.main_window as mw
        logged = []
        fake = type("W", (), {
            "_on_log": lambda self, m: logged.append(m),
            "_warn_if_ytdlp_stale": mw.MainWindow._warn_if_ytdlp_stale,
        })()
        with patch.object(mw, "staleness_warning", return_value="yt-dlp X is 99 days old."):
            fake._warn_if_ytdlp_stale()
        self.assertEqual(len(logged), 1)
        self.assertIn("99 days old", logged[0])

    def test_no_warning_means_no_log_noise(self):
        import ui.main_window as mw
        logged = []
        fake = type("W", (), {
            "_on_log": lambda self, m: logged.append(m),
            "_warn_if_ytdlp_stale": mw.MainWindow._warn_if_ytdlp_stale,
        })()
        with patch.object(mw, "staleness_warning", return_value=None):
            fake._warn_if_ytdlp_stale()
        self.assertEqual(logged, [])


if __name__ == "__main__":
    unittest.main()

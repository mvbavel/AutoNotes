"""Tests for Teams/SharePoint browser cookie preference order and error text.

Regression: SharePoint auth cookies (FedAuth/rtFa) are stored as *session*
cookies in Chrome but *persistent* in Edge. --cookies-from-browser reads the
on-disk SQLite DB, so Chrome's copy is stale and always redirects to
login.microsoftonline.com. Trying Chrome first cost ~10s of guaranteed
failures and buried the real cause in confusing log errors.
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import teams_downloader
from pipeline.teams_downloader import _BROWSERS, download_teams_recording


class TestBrowserOrder(unittest.TestCase):
    def test_edge_before_chrome(self):
        """Edge holds persistent SharePoint auth; Chrome's is in-memory only."""
        self.assertEqual(_BROWSERS.index("edge"), 0)
        self.assertLess(_BROWSERS.index("edge"), _BROWSERS.index("chrome"))

    def test_edge_tried_first_at_runtime(self):
        """Order must actually reach the subprocess, not just the constant."""
        attempted = []

        def fake_download(url, out_template, ffmpeg_dir, browser, **kwargs):
            attempted.append(browser)
            return False  # force every browser to be tried

        with mock.patch.object(teams_downloader, "_fetch_info", return_value=None), \
             mock.patch.object(teams_downloader, "_run_download", fake_download):
            with self.assertRaises(RuntimeError):
                download_teams_recording("https://x.sharepoint.com/a", "/tmp")

        self.assertEqual(attempted, ["edge", "chrome"])


class TestFailureMessage(unittest.TestCase):
    def _message(self):
        with mock.patch.object(teams_downloader, "_fetch_info", return_value=None), \
             mock.patch.object(teams_downloader, "_run_download", return_value=False):
            with self.assertRaises(RuntimeError) as ctx:
                download_teams_recording("https://x.sharepoint.com/a", "/tmp")
        return str(ctx.exception)

    def test_names_the_actual_remedy(self):
        """The old text said 'make sure you are logged in' — the user was.

        The real fix is opening the recording in Edge to mint a fresh
        session for that site collection, so the message must say so.
        """
        msg = self._message()
        self.assertIn("Edge", msg)
        self.assertIn("open", msg.lower())

    def test_does_not_only_blame_login_state(self):
        msg = self._message().lower()
        self.assertNotEqual(
            msg,
            "could not download the recording. make sure you are logged into "
            "microsoft/teams in chrome or edge and the url is accessible.",
        )


if __name__ == "__main__":
    unittest.main()

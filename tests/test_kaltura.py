"""Tests for pipeline.kaltura's wrapper-page resolution.

Portals like sapvideo.cfapps.*.hana.ondemand.com/?entry_id=… are a bare page
that loads the Kaltura player from JavaScript, so yt-dlp's generic extractor
finds no embed and fails with "Unsupported URL". yt-dlp's own Kaltura
extractor handles the entry fine once given kaltura:<partner>:<entry> — but
the player script's partner ID is rejected ("Invalid entry id"); the partner
that owns the content is the one in the thumbnail URL.
"""
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import downloader, kaltura

WRAPPER_URL = "https://sapvideo.cfapps.eu10-004.hana.ondemand.com/?entry_id=1_o683wj9q"
WRAPPER_HTML = """
<meta property="og:image" content="https://cdnapisec.kaltura.com/p/1921661/thumbnail/entry_id/1_o683wj9q/width/1200/quality/100/">
<div id="kaltura_player_54486672"></div>
<script src="https://cdnapisec.kaltura.com/p/1936251/embedPlaykitJs/uiconf_id/54486672?autoembed=true&amp;targetId=kaltura_player_54486672&amp;entry_id=1_o683wj9q"></script>
"""


class TestEntryId(unittest.TestCase):
    def test_reads_entry_id_query(self):
        self.assertEqual(kaltura.entry_id(WRAPPER_URL), "1_o683wj9q")

    def test_ignores_non_kaltura_urls(self):
        self.assertIsNone(kaltura.entry_id("https://www.youtube.com/watch?v=abc"))
        self.assertIsNone(kaltura.entry_id("https://x.example/?entry_id=not-an-id"))


class TestPartnerIds(unittest.TestCase):
    def test_thumbnail_partner_comes_before_player_partner(self):
        self.assertEqual(kaltura.partner_ids(WRAPPER_HTML), ["1921661", "1936251"])

    def test_no_kaltura_references(self):
        self.assertEqual(kaltura.partner_ids("<html></html>"), [])


class TestCandidateUrls(unittest.TestCase):
    def test_wrapper_page_yields_kaltura_urls_then_original(self):
        resp = MagicMock(status_code=200, text=WRAPPER_HTML)
        with patch("requests.get", return_value=resp):
            self.assertEqual(
                kaltura.candidate_urls(WRAPPER_URL),
                ["kaltura:1921661:1_o683wj9q", "kaltura:1936251:1_o683wj9q", WRAPPER_URL],
            )

    def test_plain_url_skips_page_fetch(self):
        with patch("requests.get") as get:
            self.assertEqual(kaltura.candidate_urls("https://youtu.be/abc"), ["https://youtu.be/abc"])
            get.assert_not_called()

    def test_fetch_failure_falls_back_to_original(self):
        with patch("requests.get", side_effect=OSError("offline")):
            self.assertEqual(kaltura.candidate_urls(WRAPPER_URL), [WRAPPER_URL])


class TestDownloaderUsesResolvedUrl(unittest.TestCase):
    def test_skips_rejected_partner_and_downloads_resolved_url(self):
        calls = []

        def fake_run_json(cmd):
            calls.append(cmd[-1])
            if cmd[-1] == "kaltura:1921661:1_o683wj9q":
                raise RuntimeError("Kaltura said: Invalid entry id")
            return {"title": "Meeting"}

        cands = ["kaltura:1921661:1_o683wj9q", "kaltura:1936251:1_o683wj9q", WRAPPER_URL]
        proc = MagicMock(stdout=iter([]), returncode=0)
        proc.poll.return_value = 0
        with patch.object(downloader, "_run_json", side_effect=fake_run_json), \
             patch.object(kaltura, "candidate_urls", return_value=cands), \
             patch.object(downloader.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(downloader, "_find_output", return_value="/tmp/Meeting.mp4"), \
             patch.object(downloader, "_download_subtitles") as subs:
            downloader.download_youtube(WRAPPER_URL, "/tmp")

        self.assertEqual(calls, cands[:2])
        self.assertEqual(popen.call_args[0][0][-1], "kaltura:1936251:1_o683wj9q")
        self.assertEqual(subs.call_args[0][0], "kaltura:1936251:1_o683wj9q")

    def test_reports_original_error_when_every_candidate_fails(self):
        def fake_run_json(cmd):
            raise RuntimeError(f"failed {cmd[-1]}")

        with patch.object(downloader, "_run_json", side_effect=fake_run_json), \
             patch.object(kaltura, "candidate_urls", return_value=["kaltura:1:1_aaaaaaaa", WRAPPER_URL]):
            with self.assertRaisesRegex(RuntimeError, re.escape(f"failed {WRAPPER_URL}")):
                downloader.download_youtube(WRAPPER_URL, "/tmp")


if __name__ == "__main__":
    unittest.main()

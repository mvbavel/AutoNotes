"""Tests for the SharePoint/Stream transcript fetcher.

Fixtures mirror the shapes observed on a real tenant recording: a Teams channel
site (/teams/...), 7-digit fractional offsets, and speaker display names.
"""
import unittest

from pipeline.sharepoint_transcript import (
    _entries_to_segments,
    _extract_g_file_info,
    _parse_sp_item_url,
    _pick_transcript,
)


class TestParseSpItemUrl(unittest.TestCase):
    def test_teams_site_collection(self):
        # Teams channel meetings land under /teams/, not /sites/ or /personal/
        url = ("https://contoso.sharepoint.com/teams/ITTeamsite/_api/v2.1"
               "/drives/b!AbCdEf/items/01MIKU6TO6XLBEH477FRB2L2A4N7UTV6UI")
        self.assertEqual(
            _parse_sp_item_url(url),
            ("/teams/ITTeamsite", "b!AbCdEf", "01MIKU6TO6XLBEH477FRB2L2A4N7UTV6UI"),
        )

    def test_sites_and_personal_collections(self):
        sites = ("https://contoso.sharepoint.com/sites/Marketing/_api/v2.0"
                 "/drives/b!X/items/01ABC")
        self.assertEqual(_parse_sp_item_url(sites), ("/sites/Marketing", "b!X", "01ABC"))

        personal = ("https://contoso-my.sharepoint.com/personal/jo_contoso_com/_api/v2.1"
                    "/drives/b!Y/items/01DEF")
        self.assertEqual(_parse_sp_item_url(personal),
                         ("/personal/jo_contoso_com", "b!Y", "01DEF"))

    def test_rejects_unusable_values(self):
        self.assertIsNone(_parse_sp_item_url(""))
        self.assertIsNone(_parse_sp_item_url("https://contoso.sharepoint.com/nope"))


class TestExtractGFileInfo(unittest.TestCase):
    def test_extracts_embedded_json(self):
        html = ('<script>var x = 1; g_fileInfo = {"hasTranscripts": true, '
                '"name": "Meeting.mp4"}; more();</script>')
        info = _extract_g_file_info(html)
        self.assertTrue(info["hasTranscripts"])
        self.assertEqual(info["name"], "Meeting.mp4")

    def test_missing_or_malformed_returns_none(self):
        self.assertIsNone(_extract_g_file_info("<html>nothing here</html>"))
        self.assertIsNone(_extract_g_file_info("g_fileInfo = {not json};"))


class TestPickTranscript(unittest.TestCase):
    def test_expand_shape(self):
        data = {"media": {"transcripts": [{"id": "a", "temporaryDownloadUrl": "u"}]}}
        self.assertEqual(_pick_transcript(data)["id"], "a")

    def test_collection_shape_prefers_default(self):
        data = {"value": [
            {"id": "a", "isDefault": False, "temporaryDownloadUrl": "u1"},
            {"id": "b", "isDefault": True, "temporaryDownloadUrl": "u2"},
        ]}
        self.assertEqual(_pick_transcript(data)["id"], "b")

    def test_single_object_shape(self):
        self.assertEqual(_pick_transcript({"temporaryDownloadUrl": "u"})["temporaryDownloadUrl"], "u")

    def test_never_transcribed_shapes_return_none(self):
        self.assertIsNone(_pick_transcript({"media": {}}))
        self.assertIsNone(_pick_transcript({"media": {"transcripts": []}}))
        self.assertIsNone(_pick_transcript({"value": []}))


class TestEntriesToSegments(unittest.TestCase):
    def _entry(self, start, end, speaker, text):
        return {"startOffset": start, "endOffset": end,
                "speakerDisplayName": speaker, "text": text}

    def test_converts_offsets_and_speakers(self):
        # 7-digit fractional seconds, as the live API returns
        entries = [self._entry("00:00:03.3744343", "00:00:23.3744343",
                               "Gemma Cave", "Good day, everyone.")]
        segments = _entries_to_segments(entries)
        self.assertEqual(len(segments), 1)
        self.assertAlmostEqual(segments[0]["start"], 3.3744343, places=5)
        self.assertAlmostEqual(segments[0]["end"], 23.3744343, places=5)
        self.assertEqual(segments[0]["speaker"], "Gemma Cave")
        self.assertEqual(segments[0]["text"], "Good day, everyone.")

    def test_merges_consecutive_same_speaker(self):
        entries = [
            self._entry("00:00:00.000", "00:00:05.000", "Ana", "First part"),
            self._entry("00:00:05.200", "00:00:09.000", "Ana", "second part"),
            self._entry("00:00:09.100", "00:00:12.000", "Bo", "Different speaker"),
        ]
        segments = _entries_to_segments(entries)
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["text"], "First part second part")
        self.assertEqual(segments[0]["end"], 9.0)
        self.assertEqual(segments[1]["speaker"], "Bo")

    def test_hour_offsets(self):
        entries = [self._entry("01:02:03.500", "01:02:04.500", "Ana", "Late")]
        self.assertAlmostEqual(_entries_to_segments(entries)[0]["start"], 3723.5, places=3)

    def test_skips_unusable_entries(self):
        entries = [
            self._entry("00:00:00.000", "00:00:01.000", "Ana", "   "),   # blank text
            {"startOffset": "00:00:02.000", "text": "no end offset"},     # missing field
            {"startOffset": "bogus", "endOffset": "00:00:04.000", "text": "bad ts"},
            "not a dict",
            self._entry("00:00:05.000", "00:00:06.000", "Ana", "Kept"),
        ]
        segments = _entries_to_segments(entries)
        self.assertEqual([s["text"] for s in segments], ["Kept"])

    def test_missing_speaker_falls_back(self):
        entries = [{"startOffset": "00:00:00.000", "endOffset": "00:00:01.000",
                    "text": "Anonymous line"}]
        self.assertEqual(_entries_to_segments(entries)[0]["speaker"], "Speaker")

    def test_orders_by_start_time(self):
        entries = [
            self._entry("00:00:30.000", "00:00:31.000", "Ana", "Later"),
            self._entry("00:00:10.000", "00:00:11.000", "Bo", "Earlier"),
        ]
        self.assertEqual([s["text"] for s in _entries_to_segments(entries)],
                         ["Earlier", "Later"])

    def test_empty_input(self):
        self.assertEqual(_entries_to_segments([]), [])


if __name__ == "__main__":
    unittest.main()

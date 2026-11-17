"""Security-relevant behaviour: which URLs get the browser's cookies, where
Microsoft refresh tokens and API keys are stored, and what is trusted."""
import os
import sys
import tempfile
import time
import types
import unittest
from http.cookiejar import Cookie
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _certs, graph_client, sharepoint_transcript as sp
from pipeline.teams_downloader import is_teams_url
from ui import secure_store


class TestTeamsUrl(unittest.TestCase):
    """Teams URLs are downloaded with the browser's cookies, so only a real
    Microsoft host may qualify — not a URL that merely mentions one."""

    def test_real_hosts(self):
        for url in ("https://contoso.sharepoint.com/sites/x/stream.aspx?id=1",
                    "https://contoso-my.sharepoint.com/personal/a/x",
                    "https://teams.microsoft.com/l/meetup-join/x",
                    "https://web.microsoftstream.com/video/1"):
            self.assertTrue(is_teams_url(url), url)

    def test_lookalikes(self):
        for url in ("https://www.youtube.com/watch?v=abc&ref=sharepoint.com",
                    "https://evil.example/sharepoint.com/video.mp4",
                    "https://notsharepoint.com/x",
                    "https://sharepoint.com.evil.example/x",
                    "/Users/me/sharepoint.com.mp4",
                    "not a url"):
            self.assertFalse(is_teams_url(url), url)


def _cookie(domain):
    return Cookie(0, "FedAuth", "v", None, False, domain, True, domain.startswith("."),
                  "/", True, True, None, False, None, None, {})


class TestSharePointTranscript(unittest.TestCase):
    def test_only_sharepoint_cookies_are_sent(self):
        jar = [_cookie(".contoso.sharepoint.com"), _cookie("sharepoint.com"),
               _cookie(".notsharepoint.com"), _cookie(".google.com")]
        sent = []
        fake_cookies = types.ModuleType("yt_dlp.cookies")
        fake_cookies.extract_cookies_from_browser = lambda browser, logger=None: jar
        with patch.dict(sys.modules, {"yt_dlp.cookies": fake_cookies}), \
             patch.object(sp, "_fetch_with_session",
                          side_effect=lambda s, url, log: sent.extend(c.domain for c in s.cookies)):
            sp._try_browser("https://contoso.sharepoint.com/x", "edge")
        self.assertEqual(sorted(sent), [".contoso.sharepoint.com", "sharepoint.com"])

    def test_player_metadata_with_brace_in_a_string(self):
        html = ('<script>var g_fileInfo = {"name":"Q3 };plan",'
                '".spItemUrl":"https://c.sharepoint.com/sites/x/_api/v2.0/drives/d/items/i",'
                '"hasTranscripts":true};</script>')
        info = sp._extract_g_file_info(html)
        self.assertEqual(info["name"], "Q3 };plan")
        self.assertTrue(info["hasTranscripts"])

    def test_non_https_download_link_is_not_followed(self):
        page = MagicMock(url="https://c.sharepoint.com/sites/x/stream.aspx",
                         text='g_fileInfo = {".spItemUrl": '
                              '"https://c.sharepoint.com/sites/x/_api/v2.0/drives/d/items/i"};')
        transcripts = MagicMock(status_code=200)
        transcripts.json.return_value = {"media": {"transcripts": [
            {"temporaryDownloadUrl": "http://attacker.example/t"}]}}
        session = MagicMock()
        session.get.side_effect = [page, transcripts]
        logs = []
        self.assertIsNone(sp._fetch_with_session(session, page.url, logs.append))
        self.assertEqual(session.get.call_count, 2, "must not fetch the http:// link")
        self.assertTrue(any("not HTTPS" in m for m in logs))


class _FakeKeyring:
    class errors:
        class PasswordDeleteError(Exception):
            pass

    def __init__(self, fail=False):
        self.store, self.fail = {}, fail

    def set_password(self, service, name, value):
        if self.fail:
            raise RuntimeError("keychain locked")
        self.store[(service, name)] = value

    def get_password(self, service, name):
        if self.fail:
            raise RuntimeError("keychain locked")
        return self.store.get((service, name))

    def delete_password(self, service, name):
        if (service, name) not in self.store:
            raise self.errors.PasswordDeleteError()
        del self.store[(service, name)]


class _Settings(dict):
    def setValue(self, k, v):
        self[k] = v

    def value(self, k, default=None):
        return self.get(k, default)

    def remove(self, k):
        self.pop(k, None)


class TestSecureStore(unittest.TestCase):
    def test_saves_to_keychain_and_purges_plaintext(self):
        kr, settings = _FakeKeyring(), _Settings(api_key="old-plain")
        with patch.object(secure_store, "keyring", kr):
            self.assertTrue(secure_store.save_secret(settings, "api_key", "sk-1"))
            self.assertEqual(secure_store.load_secret(settings, "api_key"), "sk-1")
        self.assertNotIn("api_key", settings)

    def test_migrates_legacy_plaintext(self):
        kr, settings = _FakeKeyring(), _Settings(hf_token="hf_legacy")
        with patch.object(secure_store, "keyring", kr):
            self.assertEqual(secure_store.load_secret(settings, "hf_token"), "hf_legacy")
        self.assertEqual(kr.store[("AutoNotes", "hf_token")], "hf_legacy")
        self.assertNotIn("hf_token", settings)

    def test_keychain_failure_is_reported_not_silent(self):
        settings = _Settings()
        with patch.object(secure_store, "keyring", _FakeKeyring(fail=True)):
            self.assertFalse(secure_store.save_secret(settings, "api_key", "sk-1"))
        self.assertEqual(settings["api_key"], "sk-1")

    def test_clearing_a_secret(self):
        kr, settings = _FakeKeyring(), _Settings()
        with patch.object(secure_store, "keyring", kr):
            secure_store.save_secret(settings, "api_key", "sk-1")
            self.assertTrue(secure_store.save_secret(settings, "api_key", ""))
            self.assertTrue(secure_store.save_secret(settings, "api_key", ""))  # already gone
        self.assertEqual(kr.store, {})


class TestGraphTokenCache(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.legacy = os.path.join(self._tmp.name, ".autonotes_graph_tokens.json")
        self._patch = patch.object(graph_client, "_CACHE_PATH", self.legacy)
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_refresh_tokens_move_from_file_to_keychain(self):
        with open(self.legacy, "w") as f:
            f.write('{"legacy": true}')
        kr = _FakeKeyring()
        with patch.object(graph_client, "_keyring", return_value=kr):
            self.assertEqual(graph_client._load_cache_text(), '{"legacy": true}')
            graph_client._save_cache_text('{"new": true}')
            self.assertEqual(graph_client._load_cache_text(), '{"new": true}')
        self.assertFalse(os.path.exists(self.legacy), "plaintext copy must be removed")

    def test_falls_back_to_owner_only_file(self):
        with patch.object(graph_client, "_keyring", return_value=_FakeKeyring(fail=True)):
            graph_client._save_cache_text("{}")
        self.assertEqual(os.stat(self.legacy).st_mode & 0o777, 0o600)

    def test_clear(self):
        open(self.legacy, "w").close()
        kr = _FakeKeyring()
        kr.store[("AutoNotes", "graph_token_cache")] = "{}"
        with patch.object(graph_client, "_keyring", return_value=kr):
            graph_client.clear_token_cache()
        self.assertEqual(kr.store, {})
        self.assertFalse(os.path.exists(self.legacy))

    def test_sign_in_has_a_time_limit(self):
        """Closing the sign-in tab used to block the pipeline forever."""
        app = MagicMock()
        app.get_accounts.return_value = []
        app.acquire_token_interactive.return_value = {"access_token": "t"}
        cache = MagicMock(has_state_changed=False)
        fake_msal = types.SimpleNamespace(SerializableTokenCache=lambda: cache,
                                          PublicClientApplication=lambda *a, **k: app)
        with patch.dict(sys.modules, {"msal": fake_msal}), \
             patch.object(graph_client, "_load_cache_text", return_value=""):
            self.assertEqual(graph_client.get_token("client"), "t")
        self.assertEqual(app.acquire_token_interactive.call_args.kwargs["timeout"],
                         graph_client._SIGN_IN_TIMEOUT)


class TestGraphMeetingLookup(unittest.TestCase):
    def test_join_url_quote_is_escaped(self):
        with patch.object(graph_client, "_get", return_value={"value": []}) as get:
            graph_client.find_meeting("t", "https://teams.microsoft.com/l/x?ctx='a'")
        flt = get.call_args.args[2]["$filter"]
        self.assertEqual(flt, "joinWebUrl eq 'https://teams.microsoft.com/l/x?ctx=''a'''")

    INSIGHTS = "/copilot/users/u-42/onlineMeetings/m1/aiInsights"

    def _fake_get(self, token, path, params=None):
        self.calls.append((token, path))
        if path == "/me/onlineMeetings":
            return {"value": [{"id": "m1", "subject": "Planning"}]}
        if path == "/me":
            return {"id": "u-42"}
        if path == self.INSIGHTS:
            return {"value": [{"id": "old", "createdDateTime": "2026-11-01T10:00:00Z"},
                              {"id": "new", "createdDateTime": "2026-11-02T10:00:00Z"}]}
        if path == self.INSIGHTS + "/new":
            return {"meetingNotes": [{"title": "Budget", "text": "Agreed the plan.",
                                      "subpoints": [{"title": "Q1", "text": "Hire two"}]}],
                    "actionItems": [{"text": "Send the draft", "ownerDisplayName": "Ana"}]}
        return {"value": [{"onlineMeeting": {"joinUrl": "https://j/"},
                           "attendees": [{"emailAddress": {"name": "Ana"}},
                                         {"emailAddress": {}}]}]}

    def test_full_context_uses_the_copilot_insights_api(self):
        self.calls = []
        with patch.object(graph_client, "get_token", return_value="t"), \
             patch.object(graph_client, "get_recap_token", return_value="recap-t"), \
             patch.object(graph_client, "_get", side_effect=self._fake_get):
            ctx = graph_client.fetch_meeting_context("c", "https://j")
        self.assertEqual(ctx["title"], "Planning")
        self.assertEqual(ctx["attendees"], ["Ana"])
        self.assertEqual(ctx["ai_notes"],
                         "Budget: Agreed the plan.\n  \u2022 Hire two\n\n"
                         "Action items:\n  \u2022 Send the draft (Ana)")
        # /me with the main token (User.Read); insights with the recap token
        self.assertIn(("t", "/me"), self.calls)
        self.assertIn(("recap-t", self.INSIGHTS + "/new"), self.calls)

    def test_recap_declined_still_returns_attendees(self):
        self.calls = []
        with patch.object(graph_client, "get_token", return_value="t"), \
             patch.object(graph_client, "get_recap_token", return_value=None), \
             patch.object(graph_client, "_get", side_effect=self._fake_get):
            ctx = graph_client.fetch_meeting_context("c", "https://j")
        self.assertIsNone(ctx["ai_notes"])
        self.assertEqual(ctx["attendees"], ["Ana"])

    def test_recap_consent_is_asked_once(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(graph_client, "_RECAP_DECLINED", os.path.join(tmp, "declined")), \
             patch.object(graph_client, "_acquire",
                          return_value={"error": "consent_required"}) as acquire:
            logs = []
            self.assertIsNone(graph_client.get_recap_token("c", logs.append))
            self.assertIsNone(graph_client.get_recap_token("c", logs.append))
        self.assertEqual([c.kwargs["interactive"] for c in acquire.call_args_list], [True, False])
        self.assertEqual(sum("Copilot" in m for m in logs), 1)

    def test_auth_failure_degrades(self):
        with patch.object(graph_client, "get_token", side_effect=RuntimeError("no")):
            ctx = graph_client.fetch_meeting_context("c", "https://j")
        self.assertEqual(ctx, {"title": None, "attendees": [], "ai_notes": None})


class TestCaBundleFreshness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.certifi = os.path.join(self._tmp.name, "cacert.pem")
        with open(self.certifi, "w") as f:
            f.write("-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----\n")
        self.dest = os.path.join(self._tmp.name, "ca-bundle.pem")
        self._patch = patch.object(_certs, "_export_keychain", return_value="")
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_new_certifi_rebuilds_even_if_older_on_disk(self):
        """A new build's certifi can carry an older mtime than the bundle;
        the bundle kept the previous build's roots until a keychain changed."""
        _certs.build_ca_bundle(self.certifi, self.dest)
        self.assertFalse(_certs._is_stale(self.dest, self.certifi))
        with open(self.certifi, "a") as f:
            f.write("-----BEGIN CERTIFICATE-----\nBBBB\n-----END CERTIFICATE-----\n")
        old = time.time() - 86400
        os.utime(self.certifi, (old, old))
        self.assertTrue(_certs._is_stale(self.dest, self.certifi))
        _certs.build_ca_bundle(self.certifi, self.dest)
        with open(self.dest) as f:
            self.assertIn("BBBB", f.read())


if __name__ == "__main__":
    unittest.main()

"""Tests for the combined macOS-keychain + certifi CA bundle.

Regression cover for TLS-inspecting proxies (Zscaler etc.): forcing certifi
alone leaves the corporate root — which lives only in the macOS keychain —
untrusted, and every HTTPS request fails CERTIFICATE_VERIFY_FAILED.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline import _certs

CERT_A = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"
CERT_B = "-----BEGIN CERTIFICATE-----\nBBBB\n-----END CERTIFICATE-----"
CORP = "-----BEGIN CERTIFICATE-----\nZSCALERROOT\n-----END CERTIFICATE-----"


class TestPemBlocks(unittest.TestCase):
    def test_splits_multiple(self):
        self.assertEqual(_certs._pem_blocks(CERT_A + "\n" + CERT_B),
                         [CERT_A, CERT_B])

    def test_ignores_surrounding_noise(self):
        text = f"a comment\n{CERT_A}\ntrailing junk\n"
        self.assertEqual(_certs._pem_blocks(text), [CERT_A])

    def test_empty(self):
        self.assertEqual(_certs._pem_blocks(""), [])
        self.assertEqual(_certs._pem_blocks("no certs here"), [])


class TestBuildCaBundle(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.certifi_pem = os.path.join(self._tmp.name, "cacert.pem")
        with open(self.certifi_pem, "w") as f:
            f.write(CERT_A + "\n" + CERT_B + "\n")
        self.dest = os.path.join(self._tmp.name, "out", "ca-bundle.pem")
        self._orig_export = _certs._export_keychain

    def tearDown(self):
        _certs._export_keychain = self._orig_export
        self._tmp.cleanup()

    def _fake_keychain(self, text):
        _certs._export_keychain = lambda path: text

    def test_corporate_root_is_included(self):
        """The Zscaler case: a keychain-only root must end up in the bundle."""
        self._fake_keychain(CORP)
        path = _certs.build_ca_bundle(self.certifi_pem, self.dest, force=True)
        blocks = _certs._pem_blocks(open(path).read())
        self.assertIn(CORP, blocks)
        self.assertIn(CERT_A, blocks)   # certifi roots retained

    def test_duplicates_collapsed(self):
        self._fake_keychain(CERT_A)     # keychain repeats a certifi root
        path = _certs.build_ca_bundle(self.certifi_pem, self.dest, force=True)
        blocks = _certs._pem_blocks(open(path).read())
        self.assertEqual(blocks.count(CERT_A), 1)
        self.assertEqual(len(blocks), 2)

    def test_keychain_unavailable_still_yields_certifi(self):
        self._fake_keychain("")         # e.g. `security` missing or denied
        path = _certs.build_ca_bundle(self.certifi_pem, self.dest, force=True)
        self.assertEqual(_certs._pem_blocks(open(path).read()), [CERT_A, CERT_B])

    def test_missing_certifi_returns_none(self):
        self._fake_keychain(CORP)
        self.assertIsNone(_certs.build_ca_bundle(
            os.path.join(self._tmp.name, "nope.pem"), self.dest, force=True))

    def test_reuses_fresh_bundle(self):
        self._fake_keychain(CORP)
        _certs.build_ca_bundle(self.certifi_pem, self.dest, force=True)
        calls = []
        _certs._export_keychain = lambda path: calls.append(path) or ""
        _certs.build_ca_bundle(self.certifi_pem, self.dest)   # not forced
        self.assertEqual(calls, [], "should reuse the existing bundle")

    def test_no_partial_file_on_success(self):
        self._fake_keychain(CORP)
        _certs.build_ca_bundle(self.certifi_pem, self.dest, force=True)
        self.assertFalse(os.path.exists(self.dest + ".tmp"))


class TestConfigureSslEnv(unittest.TestCase):
    def test_sets_all_four_variables(self):
        saved = {k: os.environ.get(k) for k in
                 ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
                  "CURL_CA_BUNDLE")}
        try:
            ca = _certs.configure_ssl_env()
            if ca is None:
                self.skipTest("certifi unavailable in this environment")
            self.assertTrue(os.path.exists(ca))
            for var in ("SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
                self.assertEqual(os.environ[var], ca)
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main()

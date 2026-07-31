"""Tests for TLS trust setup on TLS-inspected corporate networks.

Two defects this covers, both of which broke the Whisper model download behind
Zscaler:

A. main.py applied cert configuration only under `if sys.frozen`, so running
   from source got certifi alone -> "unable to get local issuer certificate".

B. Even with certifi + keychain roots merged, Python 3.13+ enables
   VERIFY_X509_STRICT in ssl.create_default_context(), which rejects
   "Zscaler Root CA" because its basicConstraints is not marked critical
   (RFC 5280 requires critical) -> "Basic Constraints of CA cert not marked
   critical". macOS native verification accepts it, so truststore is what
   actually fixes this; merging the bundle alone does not.
"""
import os
import ssl
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from pipeline import _certs


class TestConfigureTrust(unittest.TestCase):
    def test_installs_macos_native_verification(self):
        """Defect B: strict OpenSSL rejects the corporate root; macOS does not."""
        code = (
            "import ssl;"
            "from pipeline._certs import configure_trust;"
            "r = configure_trust();"
            "print(type(ssl.create_default_context()).__module__, r['native_trust'])"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        module, native = out.stdout.split()
        self.assertTrue(module.startswith("truststore"), f"got {module}")
        self.assertEqual(native, "True")

    def test_still_exports_ca_env_for_subprocesses(self):
        """truststore only patches in-process ssl; yt-dlp/libcurl need env vars."""
        code = (
            "import os;"
            "from pipeline._certs import configure_trust;"
            "configure_trust();"
            "print(all(os.environ.get(k) for k in "
            "('SSL_CERT_FILE','REQUESTS_CA_BUNDLE','CURL_CA_BUNDLE')))"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                             capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "True", out.stderr)

    def test_survives_missing_truststore(self):
        """Degrade to certifi+keychain rather than crashing at startup."""
        code = (
            "import sys;"
            "sys.modules['truststore'] = None;"   # import raises TypeError
            "from pipeline._certs import configure_trust;"
            "r = configure_trust();"
            "print(r['native_trust'], bool(r['ca_bundle']))"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                             capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.split(), ["False", "True"])


class TestAppliedAtStartup(unittest.TestCase):
    def test_dev_mode_gets_trust_configured(self):
        """Defect A: this is the regression — unfrozen startup must configure too."""
        code = (
            "import main, ssl;"
            "print(type(ssl.create_default_context()).__module__)"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO,
                             capture_output=True, text=True,
                             env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.strip().startswith("truststore"),
                        f"dev-mode startup did not configure trust: {out.stdout!r}")

    def test_not_gated_on_frozen(self):
        with open(os.path.join(REPO, "main.py"), encoding="utf-8") as f:
            src = f.read()
        # Anchor on the dispatch statement itself, not the word in a comment.
        head = src[:src.index('sys.argv[1] == "--yt-dlp"')]
        self.assertIn("configure_trust", head,
                      "trust setup must run before the yt-dlp dispatch")
        # The old bug: the cert call sat inside an `if getattr(sys, "frozen"...)`
        gate = head.rindex('getattr(sys, "frozen"') if 'getattr(sys, "frozen"' in head else -1
        if gate != -1:
            self.assertGreater(head.index("configure_trust"), gate,
                               "configure_trust appears inside the frozen-only gate")


class TestStrictVerifyIsTheBlocker(unittest.TestCase):
    def test_python_313_plus_enables_strict(self):
        """Documents why merging the CA bundle was not sufficient on its own."""
        if sys.version_info < (3, 13):
            self.skipTest("VERIFY_X509_STRICT is only default from 3.13")
        ctx = ssl.create_default_context()
        self.assertTrue(ctx.verify_flags & ssl.VERIFY_X509_STRICT)


if __name__ == "__main__":
    unittest.main()

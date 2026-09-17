"""Tests for the yt-dlp invocation strategy (bundled package + re-exec)."""
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline._paths import ytdlp_command


class TestYtdlpCommand(unittest.TestCase):
    def test_dev_mode_reexecs_through_main_py(self):
        """A bare system binary gets no TLS trust: on an inspected network the
        keychain-only corporate root fails VERIFY_X509_STRICT, and truststore
        only patches ssl in-process. The child must come up through main.py."""
        cmd = ytdlp_command()
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith("main.py"))
        self.assertEqual(cmd[2], "--yt-dlp")

    def test_dev_mode_falls_back_to_system_binary(self):
        """Installed as a package with no main.py alongside it."""
        with patch("pipeline._paths._MAIN_PY", "/nonexistent/main.py"):
            cmd = ytdlp_command()
        self.assertEqual(len(cmd), 1)
        self.assertTrue(cmd[0].endswith("yt-dlp"))

    def test_frozen_mode_reexecs_self(self):
        sys.frozen = True
        try:
            cmd = ytdlp_command()
        finally:
            del sys.frozen
        self.assertEqual(cmd, [sys.executable, "--yt-dlp"])

    def test_ytdlp_package_importable(self):
        """The bundled-package strategy requires the pip yt_dlp package."""
        import yt_dlp
        self.assertTrue(callable(yt_dlp.main))

    def test_cookie_decryption_dep_importable(self):
        """--cookies-from-browser chrome needs Cryptodome on macOS."""
        import Cryptodome  # noqa: F401


if __name__ == "__main__":
    unittest.main()

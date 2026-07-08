"""Tests for the yt-dlp invocation strategy (bundled package + re-exec)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pipeline._paths import ytdlp_command


class TestYtdlpCommand(unittest.TestCase):
    def test_dev_mode_uses_system_binary(self):
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

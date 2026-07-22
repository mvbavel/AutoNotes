#!/usr/bin/env python3
#
# AutoNotes — turn recordings into structured notes with screenshots.
# Copyright (C) 2026 Mark van Bavel
#
# This program is free software: you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by the
# Free Software Foundation, either version 3 of the License, or (at your
# option) any later version.
#
# This program is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# General Public License for more details.
#
# You should have received a copy of the GNU General Public License along
# with this program.  If not, see <https://www.gnu.org/licenses/>.
#
# Bundled third-party components: see THIRD-PARTY-NOTICES.md
import os
import sys

# In the frozen app, the bundled Python links Homebrew's OpenSSL, whose
# compiled-in default CA path (/opt/homebrew/etc/openssl@3/cert.pem) doesn't
# exist on machines without Homebrew — so every HTTPS request fails with
# CERTIFICATE_VERIFY_FAILED. Point OpenSSL at the bundled certifi CA bundle.
# Must run before any ssl/network import and before the --yt-dlp dispatch so
# the re-exec'd yt-dlp subprocess inherits it too.
if getattr(sys, "frozen", False):
    try:
        import certifi
        _ca = certifi.where()
        if os.path.exists(_ca):
            os.environ["SSL_CERT_FILE"] = _ca
            os.environ["SSL_CERT_DIR"] = os.path.dirname(_ca)
            os.environ.setdefault("REQUESTS_CA_BUNDLE", _ca)
    except Exception:
        pass

# Frozen-app dispatch: the pipeline invokes yt-dlp by re-running this same
# executable with --yt-dlp, so the bundled yt_dlp package works on machines
# without a system yt-dlp install. Must run before any Qt import.
if len(sys.argv) > 1 and sys.argv[1] == "--yt-dlp":
    sys.argv = ["yt-dlp"] + sys.argv[2:]
    from yt_dlp import main as ytdlp_main
    ytdlp_main()  # exits the process

from PyQt6.QtWidgets import QApplication
from PyQt6.QtGui import QFont

from ui.main_window import MainWindow


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("AutoNotes")
    app.setOrganizationName("AutoNotes")
    app.setFont(QFont("Helvetica Neue", 12))
    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

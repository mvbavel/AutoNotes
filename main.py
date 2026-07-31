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

# Configure TLS trust before any ssl/network import and before the --yt-dlp
# dispatch, so the re-exec'd yt-dlp inherits it too.
#
# Two problems this solves. The frozen app's bundled Python links Homebrew's
# OpenSSL, whose compiled-in CA path doesn't exist on machines without
# Homebrew. And on a TLS-inspecting network (Zscaler et al.) the proxy re-signs
# every connection with a corporate root that is keychain-only, which Python
# 3.13+ then rejects under VERIFY_X509_STRICT because it isn't RFC-compliant —
# so macOS-native verification, not just a merged CA bundle, is required.
#
# Runs unconditionally: running from source hits the inspected-network case
# exactly as the frozen app does, and gating this on sys.frozen silently broke
# every download outside the built .app.
try:
    from pipeline._certs import configure_trust
    configure_trust()
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

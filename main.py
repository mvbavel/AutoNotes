#!/usr/bin/env python3
import sys

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

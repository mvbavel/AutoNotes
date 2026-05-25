#!/usr/bin/env python3
import sys

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

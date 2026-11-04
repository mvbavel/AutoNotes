"""MainWindow state and legibility, rendered under Qt's offscreen platform.

Real settings, Keychain secrets and the saved transcript are patched out so a
test never reads the user's API key or Teams Join URL, and never writes back.
"""
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings  # noqa: E402
from PyQt6.QtGui import QColor, QPalette  # noqa: E402
from PyQt6.QtWidgets import QApplication, QLabel  # noqa: E402

import ui.main_window as mw  # noqa: E402

_app = QApplication.instance() or QApplication([])

# WCAG AA for small text; most of the window's labels are 10-12 px
_MIN_CONTRAST = 4.5


def _luminance(c: QColor) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(c.red()) + 0.7152 * ch(c.green()) + 0.0722 * ch(c.blue())


def _contrast(a: QColor, b: QColor) -> float:
    la, lb = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _label_color(lbl: QLabel):
    m = re.search(r"(?<![-\w])color:\s*(#[0-9a-fA-F]{3,6})", lbl.styleSheet())
    return QColor(m.group(1)) if m else None


def _dark_palette() -> QPalette:
    p = QPalette()
    for role, hexv in [
        (QPalette.ColorRole.Window, "#323232"),
        (QPalette.ColorRole.WindowText, "#ffffff"),
        (QPalette.ColorRole.Base, "#1e1e1e"),
        (QPalette.ColorRole.Text, "#ffffff"),
        (QPalette.ColorRole.Button, "#3c3c3c"),
        (QPalette.ColorRole.ButtonText, "#ffffff"),
    ]:
        p.setColor(role, QColor(hexv))
    return p


class MainWindowTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        ini = os.path.join(self._tmp.name, "settings.ini")
        self._patches = [
            patch.object(mw, "QSettings",
                         lambda *a: QSettings(ini, QSettings.Format.IniFormat)),
            patch.object(mw, "load_secret", return_value=""),
            patch.object(mw, "save_secret"),
            patch.object(mw, "staleness_warning", return_value=None),
            patch.object(mw, "load_saved_transcript", return_value=([], {})),
        ]
        for p in self._patches:
            p.start()
        self.win = mw.MainWindow()

    def tearDown(self):
        self.win.close()
        self.win.deleteLater()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def browse(self, path="/videos/talk.mp4"):
        with patch.object(mw.QFileDialog, "getOpenFileName", return_value=(path, "")):
            self.win._browse_file()


class TestInputSource(MainWindowTestCase):
    def test_typing_url_after_browse_drops_the_file(self):
        # Otherwise the old file name stays on screen while the URL silently wins
        self.browse()
        self.win.url_edit.setText("https://www.youtube.com/watch?v=abc")
        self.assertEqual(self.win._local_file, "")
        self.assertNotIn("talk.mp4", self.win.file_path_label.text())

    def test_browse_shows_ready_status(self):
        self.win.api_key_edit.setText("dummy")
        self.browse()
        self.assertTrue(self.win.process_btn.isEnabled())
        self.assertIn("Ready", self.win.input_status.text())

    def test_browse_without_key_asks_for_key(self):
        self.browse()
        self.assertFalse(self.win.process_btn.isEnabled())
        self.assertIn("API key", self.win.input_status.text())


class TestRunOutcome(MainWindowTestCase):
    def test_error_marks_current_stage_failed(self):
        self.win._on_stage_changed("Transcribing speech", 3, 7)
        self.win._on_error("KeyError: 'chapters'")
        self.win._set_idle()
        states = self.win._stage_states
        self.assertEqual(states[2], "failed")
        self.assertIn("transcribing", self.win.run_status.text().lower())
        # Earlier stages really did finish; later ones never ran
        self.assertEqual(states[1], "done")
        self.assertEqual(states[3], "pending")

    def test_cancel_does_not_leave_stage_active(self):
        self.win._worker = MagicMock(**{"isRunning.return_value": True})
        self.win._on_stage_changed("Extracting audio", 2, 7)
        self.win._cancel_processing()
        self.win._set_idle()
        self.assertEqual(self.win._stage_states[1], "cancelled")
        self.assertIn("Cancelled", self.win.run_status.text())

    def test_cancelling_label_fits_button(self):
        self.win._worker = MagicMock(**{"isRunning.return_value": True})
        self.win._cancel_processing()
        btn = self.win.cancel_btn
        self.assertGreaterEqual(btn.maximumWidth(), btn.sizeHint().width())

    def test_new_run_clears_previous_outcome(self):
        self.win._on_stage_changed("Transcribing speech", 3, 7)
        self.win._on_error("boom")
        self.win._set_idle()
        self.win._reset_progress()
        self.assertEqual(self.win.run_status.text(), "")
        self.assertEqual(set(self.win._stage_states), {"pending"})
        self.assertEqual({l.text() for l in self.win.stage_details}, {""})


class TestStageRows(MainWindowTestCase):
    def test_skipped_stage_is_not_shown_as_done(self):
        self.win._on_stage_changed("Extracting audio", 2, 7)
        self.win._on_stage_detail(2, "Not needed, transcript included", True)
        self.win._on_stage_changed("Transcribing speech", 3, 7)
        self.assertEqual(self.win._stage_states[1], "skipped")
        self.assertIn("transcript included", self.win.stage_details[1].text())

    def test_finished_stage_keeps_its_detail(self):
        self.win._on_stage_changed("Extracting screenshots", 5, 7)
        self.win._on_stage_detail(5, "34 screenshots", False)
        self.win._on_stage_changed("Generating AI notes", 6, 7)
        self.assertEqual(self.win._stage_states[4], "done")
        self.assertTrue(self.win.stage_details[4].text().startswith("34 screenshots"))

    def test_active_stage_shows_progress(self):
        self.win._on_stage_changed("Transcribing speech", 3, 7)
        self.win._on_stage_progress(43)
        self.assertIn("43%", self.win.stage_details[2].text())

    def test_completion_reports_total_time(self):
        self.win._on_stage_changed("Writing document", 7, 7)
        self.win._on_completed("/out/Weekly sync.docx")
        self.assertIn("Weekly sync.docx", self.win.run_status.text())
        self.assertEqual(set(self.win._stage_states), {"done"})


class TestCopy(MainWindowTestCase):
    def test_quality_labels_map_to_whisper_models(self):
        combo = self.win.model_combo
        self.assertEqual(combo.currentData(), "medium")
        self.assertNotEqual(combo.currentText(), "medium")
        models = [combo.itemData(i) for i in range(combo.count())]
        self.assertEqual(models, ["tiny", "base", "small", "medium", "large-v3"])

    def test_saved_model_is_restored(self):
        self.win._settings.setValue("whisper_model", "small")
        self.win._load_settings()
        self.assertEqual(self.win.model_combo.currentData(), "small")

    def test_no_templated_separators(self):
        for lbl in self.win.findChildren(QLabel):
            self.assertNotIn(" · ", lbl.text(), lbl.text())
            self.assertNotIn(" — ", lbl.text(), lbl.text())
        self.assertNotIn(" — ", self.win.url_edit.placeholderText())


def _qss_color(qss: str, selector: str, prop: str = "background"):
    block = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", qss)
    m = block and re.search(rf"(?<![-\w]){prop}:\s*(#[0-9a-fA-F]{{3,6}})", block.group(1))
    return QColor(m.group(1)) if m else None


class TestNativeLayout(MainWindowTestCase):
    """Issues seen only under the native macOS style, not offscreen Fusion."""

    def test_form_fields_fill_the_column(self):
        # macOS style defaults QFormLayout to FieldsStayAtSizeHint, which left
        # the key and link fields about 180 px wide in a 500 px column
        from PyQt6.QtWidgets import QFormLayout
        form = self.win.api_key_edit.parentWidget().layout()
        self.assertEqual(form.fieldGrowthPolicy(),
                         QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

    def test_token_field_is_labelled_as_a_token(self):
        # "Speaker names:" over a row of dots read as if names were hidden
        form = self.win.hf_token_edit.parentWidget().layout()
        self.assertIn("Hugging Face", form.labelForField(self.win.hf_token_edit).text())

    def test_join_link_shows_its_start(self):
        # QLineEdit scrolls to the end on setText, which displayed the meeting
        # token instead of the recognisable teams.microsoft.com prefix
        self.win._settings.setValue(
            "ms_join_url", "https://teams.microsoft.com/l/meetup-join/" + "x" * 200)
        self.win._load_settings()
        self.assertEqual(self.win.ms_join_url_edit.cursorPosition(), 0)

    def test_no_placeholder_file_row(self):
        self.assertTrue(self.win.file_path_label.isHidden())
        self.browse()
        self.assertFalse(self.win.file_path_label.isHidden())

    def test_no_duplicate_app_title(self):
        # The window title bar already says AutoNotes
        self.assertNotIn("AutoNotes", [l.text() for l in self.win.findChildren(QLabel)])

    def test_log_is_a_titled_section_like_the_others(self):
        from PyQt6.QtWidgets import QGroupBox
        titles = {b.title(): b for b in self.win.findChildren(QGroupBox)}
        self.assertIn("Log", titles)
        self.assertTrue(titles["Log"].isAncestorOf(self.win.log_text))


class TestLegibility(MainWindowTestCase):
    def _assert_labels_legible(self):
        bg = self.win.palette().color(QPalette.ColorRole.Window)
        checked = 0
        for lbl in self.win.findChildren(QLabel):
            fg = _label_color(lbl)
            if fg is None or not lbl.text():
                continue
            checked += 1
            ratio = _contrast(fg, bg)
            self.assertGreaterEqual(
                ratio, _MIN_CONTRAST,
                f"{lbl.text()!r}: {fg.name()} on {bg.name()} is {ratio:.2f}:1")
        self.assertGreater(checked, 5)

    def _exercise_states(self):
        self.browse()
        self.win._on_stage_changed("Extracting audio", 2, 7)

    def test_light_theme(self):
        self._exercise_states()
        self._assert_labels_legible()

    def _assert_buttons_legible(self):
        win_bg = self.win.palette().color(QPalette.ColorRole.Window)
        for btn, selector in [(self.win.process_btn, "QPushButton"),
                              (self.win.open_btn, "QPushButton")]:
            qss = btn.styleSheet()
            bg, fg = _qss_color(qss, selector), _qss_color(qss, selector, "color")
            self.assertGreaterEqual(_contrast(fg, bg), _MIN_CONTRAST,
                                    f"{btn.text()}: {fg.name()} on {bg.name()}")
        # A disabled button shouldn't be the brightest thing in a dark window
        qss = self.win.process_btn.styleSheet()
        bg = _qss_color(qss, "QPushButton:disabled")
        fg = _qss_color(qss, "QPushButton:disabled", "color")
        self.assertLess(abs(bg.lightness() - win_bg.lightness()), 60,
                        f"disabled {bg.name()} vs window {win_bg.name()}")
        self.assertGreaterEqual(_contrast(fg, bg), 3.0)

    def _assert_log_follows_theme(self):
        bg = _qss_color(self.win.log_text.styleSheet(), "QTextEdit")
        base = self.win.palette().color(QPalette.ColorRole.Base)
        self.assertEqual(bg.name(), base.name())

    def test_light_buttons_and_log(self):
        self._assert_buttons_legible()
        self._assert_log_follows_theme()

    def test_dark_buttons_and_log(self):
        original = QApplication.palette()
        try:
            QApplication.setPalette(_dark_palette())
            QApplication.processEvents()
            self._assert_buttons_legible()
            self._assert_log_follows_theme()
        finally:
            QApplication.setPalette(original)

    def test_dark_theme(self):
        original = QApplication.palette()
        try:
            QApplication.setPalette(_dark_palette())
            QApplication.processEvents()
            # Guard against a vacuous pass where the palette never reached the window
            self.assertTrue(self.win._is_dark())
            self._exercise_states()
            self._assert_labels_legible()
        finally:
            QApplication.setPalette(original)


if __name__ == "__main__":
    unittest.main()

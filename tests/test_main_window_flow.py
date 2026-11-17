"""Main-window behaviour across a run: locking, warnings, progress, closing.

Runs offscreen with the worker replaced by a mock, so no thread, network,
Keychain or real settings are touched.
"""
import os
import sys
import tempfile
import unittest
from typing import ClassVar
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from PyQt6.QtCore import QMimeData, QSettings, QUrl  # noqa: E402
from PyQt6.QtGui import QCloseEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import ui.main_window as mw  # noqa: E402

_app = QApplication.instance() or QApplication(sys.argv)


class FlowCase(unittest.TestCase):
    saved: ClassVar[tuple] = ([], {})
    stale: ClassVar[str | None] = None
    settings: ClassVar[dict] = {}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        ini = os.path.join(self._tmp.name, "settings.ini")
        seed = QSettings(ini, QSettings.Format.IniFormat)
        for k, v in self.settings.items():
            seed.setValue(k, v)
        seed.sync()
        self.save_secret = MagicMock(return_value=True)
        self._patches = [
            patch.object(mw, "QSettings", lambda *a: QSettings(ini, QSettings.Format.IniFormat)),
            patch.object(mw, "load_secret", return_value=""),
            patch.object(mw, "save_secret", self.save_secret),
            patch.object(mw, "staleness_warning", return_value=self.stale),
            patch.object(mw, "load_saved_transcript", return_value=self.saved),
            patch.object(mw.MainWindow, "_notify"),
            patch.object(mw.MainWindow, "_start_keep_awake"),
            patch.object(mw, "ProcessingWorker"),
        ]
        for p in self._patches:
            p.start()
        self.win = mw.MainWindow()
        self.video = os.path.join(self._tmp.name, "talk.mp4")
        open(self.video, "w").close()

    def tearDown(self):
        self.win._running = False   # never prompt on close in tests
        self.win.close()
        self.win.deleteLater()
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()

    def start_run(self):
        self.win.api_key_edit.setText("sk-ant-x")
        self.win._choose_file(self.video)
        self.win._start_processing()


class TestRunLifecycle(FlowCase):
    def test_inputs_lock_during_a_run_and_unlock_after(self):
        self.start_run()
        self.assertTrue(self.win._is_running())
        for w in (self.win.url_edit, self.win.api_key_edit, self.win.model_combo,
                  self.win.output_dir_edit, self.win.browse_btn):
            self.assertFalse(w.isEnabled(), w)
        self.assertEqual(self.win.input_status.text(), "")
        self.win._on_completed(os.path.join(self._tmp.name, "out.docx"))
        self.win._set_idle()
        self.assertTrue(self.win.url_edit.isEnabled())
        self.assertTrue(self.win.process_btn.isEnabled(),
                        "Generate must come back even if the thread still says running")

    def test_reuse_is_a_one_off(self):
        self.start_run()
        self.win.reuse_transcript_check.setEnabled(True)
        self.win.reuse_transcript_check.setChecked(True)
        self.win._on_completed(os.path.join(self._tmp.name, "out.docx"))
        self.assertFalse(self.win.reuse_transcript_check.isChecked())

    def test_warning_is_shown_and_survives_completion(self):
        self.start_run()
        self.win._on_warning("The reused transcript is from a different recording.")
        self.assertIn("different recording", self.win.run_status.text())
        self.win._on_completed(os.path.join(self._tmp.name, "out.docx"))
        text = self.win.run_status.text()
        self.assertIn("Notes saved", text)
        self.assertIn("different recording", text)
        self.assertEqual(self.win.run_status.property("tone"), "warning")

    def test_clean_completion_is_success(self):
        self.start_run()
        self.win._on_completed(os.path.join(self._tmp.name, "out.docx"))
        self.assertEqual(self.win.run_status.property("tone"), "success")
        self.assertTrue(self.win.reveal_btn.isVisibleTo(self.win))

    def test_progress_is_weighted_by_stage_length(self):
        self.start_run()
        self.win._on_stage_changed("Transcribing speech", 3, 7)
        self.win._on_stage_progress(50)
        # Equal weights read 36% here; transcription is most of the run
        self.assertTrue(35 < self.win.progress_bar.value() < 50, self.win.progress_bar.value())
        self.win._on_stage_changed("Writing document", 7, 7)
        self.assertGreater(self.win.progress_bar.value(), 95)

    def test_eta_shows_on_the_active_row(self):
        self.start_run()
        self.win._on_stage_changed("Transcribing speech", 3, 7)
        self.win._on_stage_eta("about 14m left")
        self.assertIn("about 14m left", self.win.stage_details[2].text())
        self.win._on_stage_changed("Identifying speakers", 4, 7)
        self.assertNotIn("left", self.win.stage_details[3].text())

    def test_cancel_kills_children_once(self):
        self.start_run()
        self.win._worker.isRunning.return_value = True
        with patch.object(mw, "kill_active_processes") as kill:
            self.win._cancel_processing()
            self.win._cancel_processing()   # Escape pressed twice
        kill.assert_called_once()
        self.win._worker.cancel.assert_called_once()


class TestClosing(FlowCase):
    def _close(self, answer):
        event = QCloseEvent()
        with patch.object(mw.QMessageBox, "question", return_value=answer) as ask:
            self.win.closeEvent(event)
        return event, ask

    def test_closing_while_idle_does_not_ask(self):
        event, ask = self._close(QMessageBox.StandardButton.Yes)
        ask.assert_not_called()
        self.assertTrue(event.isAccepted())

    def test_closing_mid_run_can_be_refused(self):
        self.start_run()
        event, ask = self._close(QMessageBox.StandardButton.Cancel)
        ask.assert_called_once()
        self.assertFalse(event.isAccepted())
        self.win._worker.cancel.assert_not_called()

    def test_closing_mid_run_stops_and_cleans_up(self):
        self.start_run()
        leftover = os.path.join(self._tmp.name, "autonotes_run")
        os.makedirs(leftover)
        self.win._worker.temp_dir = leftover
        self.win._worker.wait.return_value = True
        with patch.object(mw, "kill_active_processes") as kill:
            event, _ = self._close(QMessageBox.StandardButton.Yes)
        self.assertTrue(event.isAccepted())
        self.win._worker.cancel.assert_called_once()
        kill.assert_called_once()
        self.assertFalse(os.path.exists(leftover), "the recording copy must not be left behind")

    def test_run_that_will_not_stop_quits_when_it_does(self):
        """Destroying a still-running QThread crashes Qt; wait it out hidden."""
        self.start_run()
        self.win.show()
        self.win._worker.wait.return_value = False
        with patch.object(mw, "kill_active_processes"):
            event, _ = self._close(QMessageBox.StandardButton.Yes)
        self.assertFalse(event.isAccepted())
        self.assertFalse(self.win.isVisible())
        self.win._worker.finished.connect.assert_called_with(QApplication.quit)


class TestInputs(FlowCase):
    def test_status_names_the_source(self):
        self.win.api_key_edit.setText("k")
        self.win.url_edit.setText("https://contoso.sharepoint.com/sites/x/stream.aspx?id=1")
        self.assertIn("Teams or SharePoint", self.win.input_status.text())
        self.win.url_edit.setText("https://youtu.be/abc")
        self.assertIn("YouTube", self.win.input_status.text())

    def test_text_that_is_not_a_link_warns(self):
        self.win.api_key_edit.setText("k")
        self.win.url_edit.setText("youtube dot com slash something")
        self.assertIn("https://", self.win.input_status.text())

    def test_return_only_starts_when_ready(self):
        self.win._start_if_ready()
        mw.ProcessingWorker.assert_not_called()
        self.win.api_key_edit.setText("k")
        self.win.url_edit.setText("https://youtu.be/abc")
        self.win._start_if_ready()
        mw.ProcessingWorker.assert_called_once()

    def test_dropped_sources(self):
        def mime(urls=(), text=None):
            m = QMimeData()
            if urls:
                m.setUrls([QUrl(u) for u in urls])
            if text is not None:
                m.setText(text)
            return m
        doc = os.path.join(self._tmp.name, "notes.pdf")
        open(doc, "w").close()
        self.assertEqual(self.win._dropped_source(mime([QUrl.fromLocalFile(self.video).toString()])),
                         ("file", self.video))
        self.assertIsNone(self.win._dropped_source(mime([QUrl.fromLocalFile(doc).toString()])))
        self.assertEqual(self.win._dropped_source(mime(["https://youtu.be/x"])),
                         ("url", "https://youtu.be/x"))
        self.assertEqual(self.win._dropped_source(mime(text="https://youtu.be/y")),
                         ("url", "https://youtu.be/y"))
        self.assertIsNone(self.win._dropped_source(mime(text="hello")))

    def test_settings_save_as_they_change(self):
        self.save_secret.reset_mock()
        self.win.api_key_edit.setText("sk-ant-new")
        self.win.api_key_edit.editingFinished.emit()
        self.assertIn(("api_key", "sk-ant-new"),
                      [(c.args[1], c.args[2]) for c in self.save_secret.call_args_list])

    def test_keychain_fallback_is_reported(self):
        self.save_secret.return_value = False
        self.win._save_settings()
        self.assertIn("Keychain", self.win.log_text.toPlainText())

    def test_teams_options_hidden_until_wanted(self):
        self.assertFalse(self.win.ms_client_id_edit.isVisibleTo(self.win))
        self.win.teams_toggle.setChecked(True)
        self.assertTrue(self.win.ms_client_id_edit.isVisibleTo(self.win))


class TestTeamsOptionsInUse(FlowCase):
    settings: ClassVar[dict] = {"ms_client_id": "abc"}

    def test_shown_when_configured(self):
        self.assertTrue(self.win.ms_client_id_edit.isVisibleTo(self.win))


class TestStaleBanner(FlowCase):
    stale = "yt-dlp 2026.08.19 is 89 days old."

    def test_banner_outlives_the_log(self):
        self.assertTrue(self.win.stale_banner.isVisibleTo(self.win))
        self.start_run()   # clears the log
        self.assertIn("89 days old", self.win.stale_banner.text())


class TestKeepAwake(unittest.TestCase):
    def test_caffeinate_is_tied_to_this_process(self):
        win = MagicMock(_keep_awake=None)
        with patch.object(mw.subprocess, "Popen") as popen, \
             patch.object(mw.os.path, "exists", return_value=True):
            mw.MainWindow._start_keep_awake(win)
        args = popen.call_args.args[0]
        self.assertEqual(args[:3], ["/usr/bin/caffeinate", "-i", "-w"])
        self.assertEqual(args[3], str(os.getpid()))


if __name__ == "__main__":
    unittest.main()

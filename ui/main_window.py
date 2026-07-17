import os
import subprocess

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtGui import QFont, QColor, QPalette
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QComboBox, QTextEdit, QProgressBar, QGroupBox,
    QFormLayout, QSplitter, QFrame, QSizePolicy, QCheckBox,
)

from pipeline.worker import ProcessingWorker, load_saved_transcript
from ui.secure_store import load_secret, save_secret
from version import __version__


def _elide_middle(text: str, head: int = 15, tail: int = 10) -> str:
    """Shorten a long string to 'first head…last tail' so the info label
    doesn't force the config panel wide. Short strings are left as-is."""
    if len(text) <= head + tail + 1:
        return text
    return f"{text[:head]}…{text[-tail:]}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoNotes")
        self.setMinimumSize(900, 680)
        self._worker = None
        self._local_file = ""
        self._out_path = ""
        self._current_stage = 1
        self._total_stages = len(ProcessingWorker.STAGES)
        self._settings = QSettings("AutoNotes", "AutoNotes")
        self._build_ui()
        self._load_settings()

    # ── UI Construction ──────────────────────────────────────────────────

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Title bar
        title = QLabel("AutoNotes")
        title.setFont(QFont("Helvetica Neue", 22, QFont.Weight.Bold))
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        root.addWidget(title)

        subtitle = QLabel(f"AI-powered video notes with screenshots  ·  v{__version__}")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle.setStyleSheet("color: #666; font-size: 12px;")
        root.addWidget(subtitle)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        root.addWidget(splitter, stretch=1)

        splitter.addWidget(self._build_left_panel())
        splitter.addWidget(self._build_right_panel())
        splitter.setSizes([420, 480])

    def _build_left_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 8, 0)
        layout.setSpacing(12)

        layout.addWidget(self._build_input_group())
        layout.addWidget(self._build_config_group())
        layout.addWidget(self._build_output_group())
        layout.addStretch()
        layout.addWidget(self._build_action_bar())

        return panel

    def _build_input_group(self) -> QGroupBox:
        box = QGroupBox("Video Input")
        layout = QVBoxLayout(box)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste YouTube URL, Teams recording URL, or SharePoint URL  —  or browse for a local file")
        self.url_edit.textChanged.connect(self._on_input_changed)
        layout.addWidget(self.url_edit)

        self.input_status = QLabel("")
        self.input_status.setStyleSheet("color: #1a73e8; font-size: 11px;")
        layout.addWidget(self.input_status)

        row = QHBoxLayout()
        self.file_path_label = QLabel("No file selected")
        self.file_path_label.setStyleSheet("color: #888; font-size: 11px;")
        self.file_path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.file_path_label)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(90)
        browse_btn.clicked.connect(self._browse_file)
        row.addWidget(browse_btn)
        layout.addLayout(row)

        return box

    def _build_config_group(self) -> QGroupBox:
        box = QGroupBox("Configuration")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-ant-…")
        self.api_key_edit.textChanged.connect(self._on_input_changed)
        form.addRow("Claude API Key:", self.api_key_edit)

        self.hf_token_edit = QLineEdit()
        self.hf_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.hf_token_edit.setPlaceholderText("hf_… (optional — enables speaker ID)")
        form.addRow("HuggingFace Token:", self.hf_token_edit)

        self.model_combo = QComboBox()
        self.model_combo.addItems(["tiny", "base", "small", "medium", "large-v3"])
        self.model_combo.setCurrentText("medium")
        form.addRow("Whisper Model:", self.model_combo)

        hint = QLabel(
            "tiny/base = fast, lower accuracy  ·  large-v3 = best, slow on CPU"
        )
        hint.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow("", hint)

        self.reuse_transcript_check = QCheckBox("Reuse last transcript (skip transcription)")
        form.addRow("", self.reuse_transcript_check)

        self.reuse_info_label = QLabel("")
        self.reuse_info_label.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow("", self.reuse_info_label)

        # ── Microsoft Teams / Graph API (optional) ────────────────────────
        teams_header = QLabel("Microsoft Teams (optional)")
        teams_header.setStyleSheet("color: #aaa; font-size: 10px; font-weight: bold; margin-top: 6px;")
        form.addRow("", teams_header)

        self.ms_client_id_edit = QLineEdit()
        self.ms_client_id_edit.setPlaceholderText("Azure app Client ID (for attendees, AI notes, transcript)")
        form.addRow("MS Client ID:", self.ms_client_id_edit)

        self.ms_join_url_edit = QLineEdit()
        self.ms_join_url_edit.setPlaceholderText("Teams meeting Join URL (from calendar invite — enables full metadata)")
        form.addRow("Join URL:", self.ms_join_url_edit)

        ms_hint = QLabel("Leave blank to use recording URL only (video + VTT transcript via yt-dlp)")
        ms_hint.setStyleSheet("color: #888; font-size: 10px;")
        form.addRow("", ms_hint)

        return box

    def _build_output_group(self) -> QGroupBox:
        box = QGroupBox("Output")
        row = QHBoxLayout(box)

        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText(os.path.expanduser("~/Desktop"))
        row.addWidget(self.output_dir_edit)

        btn = QPushButton("Browse…")
        btn.setFixedWidth(90)
        btn.clicked.connect(self._browse_output_dir)
        row.addWidget(btn)

        return box

    def _build_action_bar(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)

        self.process_btn = QPushButton("Generate Notes")
        self.process_btn.setFixedHeight(40)
        self.process_btn.setEnabled(False)
        self.process_btn.setStyleSheet(
            "QPushButton { background: #1a73e8; color: white; border-radius: 6px; font-size: 14px; font-weight: bold; }"
            "QPushButton:hover { background: #1558b0; }"
            "QPushButton:disabled { background: #ccc; color: #888; }"
        )
        self.process_btn.clicked.connect(self._start_processing)
        row.addWidget(self.process_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setFixedHeight(40)
        self.cancel_btn.setFixedWidth(90)
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self._cancel_processing)
        row.addWidget(self.cancel_btn)

        return w

    def _build_right_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(8, 0, 0, 0)
        layout.setSpacing(10)

        # Stage indicators
        stages_box = QGroupBox("Progress")
        stages_layout = QVBoxLayout(stages_box)
        stages_layout.setSpacing(4)

        self.stage_labels: list[QLabel] = []
        self._stage_names = [
            "Download / load video",
            "Extract audio",
            "Transcribe speech",
            "Identify speakers",
            "Extract screenshots",
            "Generate AI notes",
            "Write document",
        ]
        for name in self._stage_names:
            lbl = QLabel()
            stages_layout.addWidget(lbl)
            self.stage_labels.append(lbl)
            self._render_stage(lbl, name, "pending")

        layout.addWidget(stages_box)

        # Overall progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(20)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # Log
        log_label = QLabel("Log")
        log_label.setFont(QFont("Helvetica Neue", 11, QFont.Weight.Bold))
        layout.addWidget(log_label)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Menlo", 10))
        self.log_text.setStyleSheet("background: #1e1e1e; color: #d4d4d4; border-radius: 4px;")
        layout.addWidget(self.log_text, stretch=1)

        # Open output button
        self.open_btn = QPushButton("Open Document")
        self.open_btn.setFixedHeight(38)
        self.open_btn.setVisible(False)
        self.open_btn.setStyleSheet(
            "QPushButton { background: #34a853; color: white; border-radius: 6px; font-size: 13px; font-weight: bold; }"
            "QPushButton:hover { background: #288543; }"
        )
        self.open_btn.clicked.connect(self._open_output)
        layout.addWidget(self.open_btn)

        return panel

    # ── Slots ────────────────────────────────────────────────────────────

    def _on_input_changed(self):
        has_input = bool(self.url_edit.text().strip()) or bool(self._local_file)
        has_key = bool(self.api_key_edit.text().strip())
        ready = has_input and has_key
        self.process_btn.setEnabled(ready)

        if ready:
            self.input_status.setText("Ready — click Generate Notes to start")
            self.input_status.setStyleSheet("color: #34a853; font-size: 11px;")
        elif has_input and not has_key:
            self.input_status.setText("Enter your Claude API key to continue")
            self.input_status.setStyleSheet("color: #e8710a; font-size: 11px;")
        else:
            self.input_status.setText("")

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", os.path.expanduser("~"),
            "Video files (*.mp4 *.mpeg *.mpg *.mov *.avi *.mkv);;All files (*)"
        )
        if path:
            self._local_file = path
            self.url_edit.clear()
            self.file_path_label.setText(os.path.basename(path))
            self.file_path_label.setStyleSheet("color: #ffffff; font-size: 11px;")
            self.process_btn.setEnabled(bool(self.api_key_edit.text().strip()))

    def _browse_output_dir(self):
        d = QFileDialog.getExistingDirectory(
            self, "Select Output Folder", os.path.expanduser("~/Desktop")
        )
        if d:
            self.output_dir_edit.setText(d)

    def _start_processing(self):
        self._save_settings()
        self._reset_progress()

        url = self.url_edit.text().strip()
        source = url or self._local_file

        config = {
            "anthropic_key": self.api_key_edit.text().strip(),
            "hf_token": self.hf_token_edit.text().strip(),
            "whisper_model": self.model_combo.currentText(),
            "reuse_transcript": self.reuse_transcript_check.isChecked(),
            "output_dir": self.output_dir_edit.text().strip() or os.path.expanduser("~/Desktop"),
            "ms_client_id": self.ms_client_id_edit.text().strip(),
            "ms_join_url": self.ms_join_url_edit.text().strip(),
        }

        self._worker = ProcessingWorker(source, config)
        self._worker.stage_changed.connect(self._on_stage_changed)
        self._worker.stage_progress.connect(self._on_stage_progress)
        self._worker.log_message.connect(self._on_log)
        self._worker.completed.connect(self._on_completed)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._set_idle)

        self.process_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.progress_bar.setVisible(True)
        self.open_btn.setVisible(False)

        self._worker.start()
        self.file_path_label.setStyleSheet("color: #1a73e8; font-size: 11px;")

    def _cancel_processing(self):
        if self._worker and self._worker.isRunning():
            # Cooperative cancel: the worker checks the flag at safe points and
            # kills its subprocesses; _set_idle runs when the thread finishes
            self._worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")
            self._on_log("Cancelling — waiting for current step to stop…")
        self.file_path_label.setStyleSheet("color: #ffffff; font-size: 11px;")

    def _render_stage(self, lbl: QLabel, name: str, state: str):
        if state == "done":
            lbl.setText(f"✓  {name}")
            lbl.setStyleSheet("color: #34a853; font-size: 12px;")
        elif state == "active":
            lbl.setText(f"▶  {name}")
            lbl.setStyleSheet("color: #1a73e8; font-size: 12px; font-weight: bold;")
        else:
            lbl.setText(f"○  {name}")
            lbl.setStyleSheet("color: #999; font-size: 12px;")

    def _on_stage_changed(self, label: str, current: int, total: int):
        self._current_stage = current
        self._total_stages = total
        for i, lbl in enumerate(self.stage_labels):
            stage_n = i + 1
            state = ("done" if stage_n < current
                     else "active" if stage_n == current
                     else "pending")
            self._render_stage(lbl, self._stage_names[i], state)

        self.progress_bar.setValue(int(((current - 1) / total) * 100))

    def _on_stage_progress(self, pct: int):
        # Overall = completed stages + fractional progress of the current one
        overall = int(((self._current_stage - 1) + pct / 100) / self._total_stages * 100)
        self.progress_bar.setValue(max(self.progress_bar.value(), overall))

    def _on_log(self, msg: str):
        self.log_text.append(msg)

    def _on_completed(self, out_path: str):
        self._on_log(f"\nDone! Output: {out_path}")
        for i, lbl in enumerate(self.stage_labels):
            self._render_stage(lbl, self._stage_names[i], "done")
        self.progress_bar.setValue(100)

        self.file_path_label.setStyleSheet("color: #34a853; font-size: 11px;")
        self._out_path = out_path
        self.open_btn.setVisible(True)

    def _on_error(self, msg: str):
        self._on_log(f"\nERROR: {msg}")

    def _set_idle(self):
        self.cancel_btn.setVisible(False)
        self.cancel_btn.setEnabled(True)
        self.cancel_btn.setText("Cancel")
        self.process_btn.setEnabled(True)
        self._refresh_transcript_info()

    def _refresh_transcript_info(self):
        """Enable the reuse checkbox only when a saved transcript exists,
        and describe it so the user knows what they'd be reusing."""
        segments, meta = load_saved_transcript()
        if segments:
            src = meta.get("video", "")
            saved = meta.get("saved", "").replace("T", " ")
            detail = f"'{_elide_middle(src)}', " if src else ""
            when = f", saved {saved}" if saved else ""
            self.reuse_transcript_check.setEnabled(True)
            self.reuse_info_label.setText(
                f"Available: {detail}{len(segments)} segments{when}")
            # Full title on hover, since the display is elided
            self.reuse_info_label.setToolTip(src)
        else:
            self.reuse_transcript_check.setEnabled(False)
            self.reuse_transcript_check.setChecked(False)
            self.reuse_info_label.setToolTip("")
            self.reuse_info_label.setText(
                "No saved transcript yet — one is kept after each transcription")

    def _reset_progress(self):
        for i, lbl in enumerate(self.stage_labels):
            self._render_stage(lbl, self._stage_names[i], "pending")
        self._current_stage = 1
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self.open_btn.setVisible(False)

    def _open_output(self):
        if self._out_path:
            subprocess.run(["open", self._out_path])

    # ── Settings persistence ─────────────────────────────────────────────

    def _save_settings(self):
        s = self._settings
        save_secret(s, "api_key", self.api_key_edit.text())
        save_secret(s, "hf_token", self.hf_token_edit.text())
        s.setValue("whisper_model", self.model_combo.currentText())
        s.setValue("output_dir", self.output_dir_edit.text())
        s.setValue("ms_client_id", self.ms_client_id_edit.text())
        s.setValue("ms_join_url", self.ms_join_url_edit.text())

    def _load_settings(self):
        s = self._settings
        self.api_key_edit.setText(load_secret(s, "api_key"))
        self.hf_token_edit.setText(load_secret(s, "hf_token"))
        model = s.value("whisper_model", "medium")
        idx = self.model_combo.findText(model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.output_dir_edit.setText(s.value("output_dir", ""))
        self.ms_client_id_edit.setText(s.value("ms_client_id", ""))
        self.ms_join_url_edit.setText(s.value("ms_join_url", ""))

        # Refresh button state based on loaded settings
        self._on_input_changed()
        self._refresh_transcript_info()

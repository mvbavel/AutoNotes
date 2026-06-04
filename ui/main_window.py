import os
import subprocess

from PyQt6.QtCore import Qt, QSettings
from PyQt6.QtGui import QFont, QColor, QPalette
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QComboBox, QTextEdit, QProgressBar, QGroupBox,
    QFormLayout, QSplitter, QFrame, QSizePolicy,
)

from pipeline.worker import ProcessingWorker
from version import __version__


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoNotes")
        self.setMinimumSize(900, 680)
        self._worker = None
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
        self.url_edit.setPlaceholderText("Paste YouTube URL  —  or browse for a local file")
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
        stage_names = [
            "Download / load video",
            "Extract audio",
            "Transcribe speech",
            "Identify speakers",
            "Extract screenshots",
            "Generate AI notes",
            "Write document",
        ]
        for name in stage_names:
            lbl = QLabel(f"○  {name}")
            lbl.setStyleSheet("color: #999; font-size: 12px;")
            stages_layout.addWidget(lbl)
            self.stage_labels.append(lbl)

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
        layout.addWidget(self.open_btn)

        return panel

    # ── Slots ────────────────────────────────────────────────────────────

    def _on_input_changed(self):
        has_input = bool(self.url_edit.text().strip()) or bool(getattr(self, "_local_file", ""))
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
        local = getattr(self, "_local_file", "")
        source = url or local

        config = {
            "anthropic_key": self.api_key_edit.text().strip(),
            "hf_token": self.hf_token_edit.text().strip(),
            "whisper_model": self.model_combo.currentText(),
            "output_dir": self.output_dir_edit.text().strip() or os.path.expanduser("~/Desktop"),
        }

        self._worker = ProcessingWorker(source, config)
        self._worker.stage_changed.connect(self._on_stage_changed)
        self._worker.stage_progress.connect(self._on_stage_progress)
        self._worker.log_message.connect(self._on_log)
        self._worker.completed.connect(self._on_completed)
        self._worker.error.connect(self._on_error)

        self.process_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.progress_bar.setVisible(True)
        self.open_btn.setVisible(False)

        self._worker.start()
        self.file_path_label.setStyleSheet("color: #1a73e8; font-size: 11px;")

    def _cancel_processing(self):
        if self._worker:
            self._worker.cancel()
            self._worker.terminate()
        self._on_log("Cancelled by user.")
        self.file_path_label.setStyleSheet("color: #ffffff; font-size: 11px;")
        self._set_idle()

    def _on_stage_changed(self, label: str, current: int, total: int):
        for i, lbl in enumerate(self.stage_labels):
            stage_n = i + 1
            if stage_n < current:
                lbl.setText(f"✓  {lbl.text()[3:]}")
                lbl.setStyleSheet("color: #34a853; font-size: 12px;")
            elif stage_n == current:
                lbl.setText(f"▶  {lbl.text()[3:]}")
                lbl.setStyleSheet("color: #1a73e8; font-size: 12px; font-weight: bold;")
            else:
                lbl.setStyleSheet("color: #999; font-size: 12px;")

        overall = int(((current - 1) / total) * 100)
        self.progress_bar.setValue(overall)

    def _on_stage_progress(self, pct: int):
        # Blend stage progress into overall
        current_val = self.progress_bar.value()
        self.progress_bar.setValue(max(current_val, pct // 10 + current_val))

    def _on_log(self, msg: str):
        self.log_text.append(msg)

    def _on_completed(self, out_path: str):
        self._on_log(f"\nDone! Output: {out_path}")
        for lbl in self.stage_labels:
            text = lbl.text()[3:]
            lbl.setText(f"✓  {text}")
            lbl.setStyleSheet("color: #34a853; font-size: 12px;")
        self.progress_bar.setValue(100)

        self.file_path_label.setStyleSheet("color: #34a853; font-size: 11px;")
        self.open_btn.setVisible(True)
        self.open_btn.clicked.connect(lambda: self._open_file(out_path))
        self._set_idle()

    def _on_error(self, msg: str):
        self._on_log(f"\nERROR: {msg}")
        self._set_idle()

    def _set_idle(self):
        self.cancel_btn.setVisible(False)
        self.process_btn.setEnabled(True)

    def _reset_progress(self):
        stage_names = [lbl.text()[3:] for lbl in self.stage_labels]
        for i, lbl in enumerate(self.stage_labels):
            lbl.setText(f"○  {stage_names[i]}")
            lbl.setStyleSheet("color: #999; font-size: 12px;")
        self.progress_bar.setValue(0)
        self.log_text.clear()
        self.open_btn.setVisible(False)

    def _open_file(self, path: str):
        subprocess.run(["open", path])

    # ── Settings persistence ─────────────────────────────────────────────

    def _save_settings(self):
        s = self._settings
        s.setValue("api_key", self.api_key_edit.text())
        s.setValue("hf_token", self.hf_token_edit.text())
        s.setValue("whisper_model", self.model_combo.currentText())
        s.setValue("output_dir", self.output_dir_edit.text())

    def _load_settings(self):
        s = self._settings
        self.api_key_edit.setText(s.value("api_key", ""))
        self.hf_token_edit.setText(s.value("hf_token", ""))
        model = s.value("whisper_model", "medium")
        idx = self.model_combo.findText(model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)
        self.output_dir_edit.setText(s.value("output_dir", ""))

        # Refresh button state based on loaded settings
        self._on_input_changed()

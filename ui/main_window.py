import os
import subprocess
import time
from datetime import datetime

from PyQt6.QtCore import Qt, QSettings, QEvent, QPointF, QRectF, QTimer
from PyQt6.QtGui import QFont, QColor, QPalette, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QFileDialog,
    QComboBox, QTextEdit, QProgressBar, QGroupBox,
    QFormLayout, QSplitter, QFrame, QSizePolicy, QCheckBox,
)

from pipeline.worker import ProcessingWorker, load_saved_transcript
from pipeline.ytdlp_health import staleness_warning
from ui.messages import explain_error, format_duration
from ui.secure_store import load_secret, save_secret
from version import __version__


# Semantic text colours per theme. Each clears WCAG AA (4.5:1) against both
# the window and the slightly darker group-box background, which the old
# fixed hex values (#888, #aaa, #34a853, and #ffffff on a light window) did not.
_TONES = {
    "light": {"muted": "#5f6368", "success": "#137333", "info": "#185abc",
              "warning": "#a14f00", "error": "#c5221f"},
    "dark":  {"muted": "#a8adb3", "success": "#81c995", "info": "#8ab4f8",
              "warning": "#fbbc5e", "error": "#f28b82"},
}


# (row label, what's under way, for "Something went wrong while …")
_STAGES = [
    ("Download recording", "downloading the recording"),
    ("Extract audio", "extracting audio"),
    ("Transcribe speech", "transcribing speech"),
    ("Identify speakers", "identifying speakers"),
    ("Pick screenshots", "picking screenshots"),
    ("Write notes with Claude", "writing notes with Claude"),
    ("Build document", "building the document"),
]

# Whisper model sizes behind plain-language labels; the model name stays
# visible because people who know Whisper will look for it
_QUALITY = [
    ("Fastest (tiny)", "tiny"),
    ("Fast (base)", "base"),
    ("Balanced (small)", "small"),
    ("Accurate (medium)", "medium"),
    ("Most accurate (large-v3)", "large-v3"),
]

# state -> (icon colour tone, name tone, name bold)
_STAGE_LOOK = {
    "pending":   ("muted", "muted", False),
    "active":    ("info", "", True),
    "done":      ("success", "", False),
    "skipped":   ("muted", "muted", False),
    "failed":    ("error", "error", True),
    "cancelled": ("warning", "warning", True),
}

_ICON_PX = 14


def _stage_icon(state: str, color: QColor, dpr: float) -> QPixmap:
    """Draw the row marker. Text glyphs (✓ ▶ ✕ ○) come from different fallback
    fonts on macOS and render at visibly different sizes, so paint them."""
    pm = QPixmap(int(_ICON_PX * dpr), int(_ICON_PX * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QPen(color, 1.6, cap=Qt.PenCapStyle.RoundCap, join=Qt.PenJoinStyle.RoundJoin))
    c, r = _ICON_PX / 2, _ICON_PX / 2 - 1.5
    if state in ("pending", "active"):
        p.drawEllipse(QPointF(c, c), r, r)
        if state == "active":
            p.setBrush(color)
            p.drawEllipse(QPointF(c, c), r / 2.2, r / 2.2)
    elif state == "done":
        p.drawPolyline([QPointF(3, 7.5), QPointF(6, 10.5), QPointF(11, 4)])
    elif state == "skipped":
        p.drawLine(QPointF(3.5, c), QPointF(10.5, c))
    elif state == "failed":
        p.drawLine(QPointF(4, 4), QPointF(10, 10))
        p.drawLine(QPointF(10, 4), QPointF(4, 10))
    elif state == "cancelled":
        p.setBrush(color)
        p.drawRect(QRectF(4, 4, 6, 6))
    p.end()
    return pm


def _elide_middle(text: str, head: int = 15, tail: int = 10) -> str:
    """Shorten a long string to 'first head…last tail' so the info label
    doesn't force the config panel wide. Short strings are left as-is."""
    if len(text) <= head + tail + 1:
        return text
    return f"{text[:head]}…{text[-tail:]}"


def _friendly_time(iso: str) -> str:
    """'2026-11-03T15:14:05' -> '3 Nov 15:14'; unparseable values pass through."""
    try:
        t = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    return f"{t.day} {t:%b %H:%M}"


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("AutoNotes")
        self.setMinimumSize(900, 680)
        self._worker = None
        self._local_file = ""
        self._out_path = ""
        self._current_stage = 1
        self._cancel_requested = False
        self._run_finished = False
        self._run_started = 0.0
        self._stage_states = ["pending"] * len(_STAGES)
        self._stage_facts = [""] * len(_STAGES)
        self._stage_skipped = [False] * len(_STAGES)
        self._stage_started = [0.0] * len(_STAGES)
        self._stage_elapsed = [0.0] * len(_STAGES)
        self._stage_pct = 0
        # Ticks the active row's elapsed time; runs only while a stage is active
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self._total_stages = len(ProcessingWorker.STAGES)
        self._settings = QSettings("AutoNotes", "AutoNotes")
        self._build_ui()
        self._load_settings()
        self._warn_if_ytdlp_stale()

    # ── Theming ──────────────────────────────────────────────────────────

    def _is_dark(self) -> bool:
        return self.palette().color(QPalette.ColorRole.Window).lightness() < 128

    def _set_tone(self, lbl: QLabel, tone: str = "", size: int = 11, bold: bool = False):
        """Style a label by meaning, not colour, so it can be re-coloured when
        the system theme flips. An empty tone inherits the palette's text colour."""
        lbl.setProperty("tone", tone)
        lbl.setProperty("tone_size", size)
        lbl.setProperty("tone_bold", bold)
        style = f"font-size: {size}px;"
        if tone:
            style += f" color: {_TONES['dark' if self._is_dark() else 'light'][tone]};"
        if bold:
            style += " font-weight: bold;"
        lbl.setStyleSheet(style)

    def _restyle(self):
        for lbl in self.findChildren(QLabel):
            if lbl.property("tone") is not None:
                self._set_tone(lbl, lbl.property("tone"),
                               lbl.property("tone_size"), lbl.property("tone_bold"))

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange):
            self._restyle()
            # Icons are pixmaps, so they don't follow the label tones on their own
            if hasattr(self, "stage_icons"):
                self._render_stages()
        super().changeEvent(event)

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

        subtitle = QLabel("Turns a meeting recording into Word notes with screenshots. "
                          f"Version {__version__}")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_tone(subtitle, "muted", 12)
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
        box = QGroupBox("Recording")
        layout = QVBoxLayout(box)

        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste a Teams, SharePoint or YouTube link, or browse for a video file")
        self.url_edit.textChanged.connect(self._on_input_changed)
        layout.addWidget(self.url_edit)

        self.input_status = QLabel("")
        self._set_tone(self.input_status, "info")
        layout.addWidget(self.input_status)

        row = QHBoxLayout()
        self.file_path_label = QLabel("No file selected")
        self._set_tone(self.file_path_label, "muted")
        self.file_path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        row.addWidget(self.file_path_label)

        browse_btn = QPushButton("Browse…")
        browse_btn.setFixedWidth(90)
        browse_btn.clicked.connect(self._browse_file)
        row.addWidget(browse_btn)
        layout.addLayout(row)

        return box

    def _build_config_group(self) -> QGroupBox:
        box = QGroupBox("Settings")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-ant-…")
        self.api_key_edit.textChanged.connect(self._on_input_changed)
        form.addRow("Claude API key:", self.api_key_edit)

        self.hf_token_edit = QLineEdit()
        self.hf_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.hf_token_edit.setPlaceholderText("Hugging Face token, optional")
        form.addRow("Speaker names:", self.hf_token_edit)

        self.model_combo = QComboBox()
        for label, model in _QUALITY:
            self.model_combo.addItem(label, model)
        self._select_model("medium")
        form.addRow("Transcription:", self.model_combo)

        hint = QLabel("More accurate transcription takes longer on this Mac.")
        self._set_tone(hint, "muted", 10)
        form.addRow("", hint)

        self.reuse_transcript_check = QCheckBox("Reuse the last transcript instead of transcribing")
        form.addRow("", self.reuse_transcript_check)

        self.reuse_info_label = QLabel("")
        self._set_tone(self.reuse_info_label, "muted", 10)
        form.addRow("", self.reuse_info_label)

        # ── Microsoft Teams / Graph API (optional) ────────────────────────
        teams_header = QLabel("Microsoft Teams (optional)")
        self._set_tone(teams_header, "muted", 10, bold=True)
        teams_header.setContentsMargins(0, 6, 0, 0)
        form.addRow("", teams_header)

        self.ms_client_id_edit = QLineEdit()
        self.ms_client_id_edit.setPlaceholderText("Azure app client ID")
        form.addRow("MS client ID:", self.ms_client_id_edit)

        self.ms_join_url_edit = QLineEdit()
        self.ms_join_url_edit.setPlaceholderText("Join link from the meeting's calendar invite")
        form.addRow("Join link:", self.ms_join_url_edit)

        ms_hint = QLabel("Both add attendees and the Teams recap. "
                         "The transcript comes from the recording either way.")
        self._set_tone(ms_hint, "muted", 10)
        form.addRow("", ms_hint)

        return box

    def _build_output_group(self) -> QGroupBox:
        box = QGroupBox("Save to")
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
        self.cancel_btn.setMinimumWidth(90)
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
        grid = QGridLayout(stages_box)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnStretch(2, 1)

        self._stage_names = [name for name, _ in _STAGES]
        self.stage_icons: list[QLabel] = []
        self.stage_labels: list[QLabel] = []
        self.stage_details: list[QLabel] = []
        for row, name in enumerate(self._stage_names):
            icon, lbl, detail = QLabel(), QLabel(name), QLabel("")
            icon.setFixedSize(_ICON_PX, _ICON_PX)
            detail.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            # Long facts (file names) must not widen the panel
            detail.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            grid.addWidget(icon, row, 0, Qt.AlignmentFlag.AlignVCenter)
            grid.addWidget(lbl, row, 1)
            grid.addWidget(detail, row, 2)
            self.stage_icons.append(icon)
            self.stage_labels.append(lbl)
            self.stage_details.append(detail)
        self._render_stages()

        layout.addWidget(stages_box)

        # Overall progress bar
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setFixedHeight(20)
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # One-line outcome so a failure or cancel isn't only visible in the log
        self.run_status = QLabel("")
        self.run_status.setWordWrap(True)
        self._set_tone(self.run_status, "", 12, bold=True)
        layout.addWidget(self.run_status)

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
        self.open_btn = QPushButton("Open Notes")
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
        # A typed URL replaces a browsed file; otherwise the stale file name
        # stays on screen while _start_processing silently uses the URL
        if self.url_edit.text().strip() and self._local_file:
            self._set_local_file("")

        has_input = bool(self.url_edit.text().strip()) or bool(self._local_file)
        has_key = bool(self.api_key_edit.text().strip())
        ready = has_input and has_key
        if not self._is_running():
            self.process_btn.setEnabled(ready)

        if ready:
            self.input_status.setText("Ready to generate notes.")
            self._set_tone(self.input_status, "success")
        elif has_input and not has_key:
            self.input_status.setText("Add your Claude API key to continue.")
            self._set_tone(self.input_status, "warning")
        else:
            self.input_status.setText("")

    def _is_running(self) -> bool:
        return bool(self._worker and self._worker.isRunning())

    def _set_local_file(self, path: str):
        self._local_file = path
        if path:
            self.file_path_label.setText(os.path.basename(path))
            self.file_path_label.setToolTip(path)
            self._set_tone(self.file_path_label, "")
        else:
            self.file_path_label.setText("No file selected")
            self.file_path_label.setToolTip("")
            self._set_tone(self.file_path_label, "muted")

    def _browse_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Video File", os.path.expanduser("~"),
            "Video files (*.mp4 *.mpeg *.mpg *.mov *.avi *.mkv);;All files (*)"
        )
        if path:
            # Clear the URL first: its textChanged must not see the new file
            self.url_edit.clear()
            self._set_local_file(path)
            self._on_input_changed()

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
            "whisper_model": self.model_combo.currentData(),
            "reuse_transcript": self.reuse_transcript_check.isChecked(),
            "output_dir": self.output_dir_edit.text().strip() or os.path.expanduser("~/Desktop"),
            "ms_client_id": self.ms_client_id_edit.text().strip(),
            "ms_join_url": self.ms_join_url_edit.text().strip(),
        }

        self._worker = ProcessingWorker(source, config)
        self._worker.stage_changed.connect(self._on_stage_changed)
        self._worker.stage_progress.connect(self._on_stage_progress)
        self._worker.stage_detail.connect(self._on_stage_detail)
        self._worker.log_message.connect(self._on_log)
        self._worker.completed.connect(self._on_completed)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._set_idle)

        self.process_btn.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.progress_bar.setVisible(True)
        self.open_btn.setVisible(False)

        self._run_started = time.monotonic()
        self._clock.start()
        self._worker.start()

    def _cancel_processing(self):
        if self._worker and self._worker.isRunning():
            # Cooperative cancel: the worker checks the flag at safe points and
            # kills its subprocesses; _set_idle runs when the thread finishes
            self._worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")
            self._on_log("Cancelling — waiting for current step to stop…")
            self._cancel_requested = True

    def _render_stages(self):
        dpr = self.devicePixelRatioF() or 1.0
        palette = _TONES["dark" if self._is_dark() else "light"]
        for i, state in enumerate(self._stage_states):
            icon_tone, name_tone, bold = _STAGE_LOOK[state]
            self.stage_icons[i].setPixmap(
                _stage_icon(state, QColor(palette[icon_tone]), dpr))
            self._set_tone(self.stage_labels[i], name_tone, 12, bold)
            self.stage_details[i].setText(self._stage_detail_text(i))
            self.stage_details[i].setToolTip(self._stage_facts[i])
            self._set_tone(self.stage_details[i], "muted", 11)

    def _stage_detail_text(self, i: int) -> str:
        """Right-hand fact for a row: what it produced, and how long it took."""
        state, fact = self._stage_states[i], self._stage_facts[i]
        if state == "pending":
            return ""
        if state == "skipped":
            return fact
        if state == "active":
            elapsed = time.monotonic() - self._stage_started[i]
            parts = [fact] if fact else []
            if 0 < self._stage_pct < 100:
                parts.append(f"{self._stage_pct}%")
            parts.append(format_duration(elapsed))
            return ", ".join(parts)
        if self._stage_elapsed[i] < 1:
            return fact  # "0s" on instant stages is noise
        elapsed = format_duration(self._stage_elapsed[i])
        return f"{fact}, {elapsed}" if fact else elapsed

    def _set_run_status(self, text: str, tone: str = ""):
        self.run_status.setText(text)
        self._set_tone(self.run_status, tone, 12, bold=True)

    def _close_stage(self, i: int, outcome: str):
        """Finish row i: freeze its time and settle its final state."""
        if self._stage_states[i] == "active":
            self._stage_elapsed[i] = time.monotonic() - self._stage_started[i]
        if outcome == "done" and self._stage_skipped[i]:
            outcome = "skipped"
        self._stage_states[i] = outcome

    def _on_stage_changed(self, label: str, current: int, total: int):
        self._current_stage = current
        self._total_stages = total
        for i in range(len(self._stage_states)):
            if i < current - 1 and self._stage_states[i] in ("pending", "active"):
                self._close_stage(i, "done")
        self._stage_states[current - 1] = "active"
        self._stage_started[current - 1] = time.monotonic()
        self._stage_pct = 0
        self._render_stages()

        self.progress_bar.setValue(int(((current - 1) / total) * 100))

    def _on_stage_progress(self, pct: int):
        # Overall = completed stages + fractional progress of the current one
        overall = int(((self._current_stage - 1) + pct / 100) / self._total_stages * 100)
        self.progress_bar.setValue(max(self.progress_bar.value(), overall))
        self._stage_pct = pct
        idx = self._current_stage - 1
        self.stage_details[idx].setText(self._stage_detail_text(idx))

    def _on_stage_detail(self, stage: int, text: str, skipped: bool):
        i = stage - 1
        self._stage_facts[i] = text
        self._stage_skipped[i] = skipped
        self._render_stages()

    def _tick(self):
        idx = self._current_stage - 1
        if self._stage_states[idx] == "active":
            self.stage_details[idx].setText(self._stage_detail_text(idx))

    def _warn_if_ytdlp_stale(self):
        """Flag an aged-out yt-dlp at launch rather than mid-download.

        Otherwise the first sign is a 403 several minutes into a run, which
        reads as a network fault rather than a version that needs updating.
        """
        warning = staleness_warning()
        if warning:
            self._on_log(f"Warning: {warning}")

    def _on_log(self, msg: str):
        self.log_text.append(msg)

    def _on_completed(self, out_path: str):
        self._on_log(f"\nDone! Output: {out_path}")
        for i in range(len(self._stage_states)):
            self._close_stage(i, "done")
        self._render_stages()
        self.progress_bar.setValue(100)

        self._out_path = out_path
        self._run_finished = True
        took = format_duration(time.monotonic() - self._run_started) if self._run_started else ""
        self._set_run_status(
            f"Notes saved as {os.path.basename(out_path)}" + (f" in {took}." if took else "."),
            "success")
        self.open_btn.setVisible(True)

    def _on_error(self, msg: str):
        self._on_log(f"\nERROR: {msg}")
        self._run_finished = True
        idx = self._current_stage - 1
        self._close_stage(idx, "failed")
        self._render_stages()
        self.progress_bar.setFormat("Stopped")
        self._set_run_status(explain_error(msg, _STAGES[idx][1]), "error")
        # The raw error stays reachable without being the headline
        self.run_status.setToolTip(msg)

    def _set_idle(self):
        self._clock.stop()
        if self._cancel_requested and not self._run_finished:
            idx = self._current_stage - 1
            self._close_stage(idx, "cancelled")
            self._render_stages()
            self.progress_bar.setFormat("Cancelled")
            self._set_run_status("Cancelled. Nothing was saved.", "warning")
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
            detail = f"'{_elide_middle(src)}', " if src else ""
            when = f", saved {_friendly_time(meta.get('saved', ''))}" if meta.get("saved") else ""
            self.reuse_transcript_check.setEnabled(True)
            self.reuse_info_label.setText(
                f"Last transcript: {detail}{len(segments):,} lines{when}")
            # Full title on hover, since the display is elided
            self.reuse_info_label.setToolTip(src)
        else:
            self.reuse_transcript_check.setEnabled(False)
            self.reuse_transcript_check.setChecked(False)
            self.reuse_info_label.setToolTip("")
            self.reuse_info_label.setText(
                "No saved transcript yet. One is kept after each transcription.")

    def _reset_progress(self):
        n = len(_STAGES)
        self._stage_states = ["pending"] * n
        self._stage_facts = [""] * n
        self._stage_skipped = [False] * n
        self._stage_elapsed = [0.0] * n
        self._stage_pct = 0
        self._render_stages()
        self.run_status.setToolTip("")
        self._current_stage = 1
        self._cancel_requested = False
        self._run_finished = False
        self.progress_bar.setValue(0)
        self.progress_bar.setFormat("%p%")
        self._set_run_status("")
        self.log_text.clear()
        self.open_btn.setVisible(False)

    def _select_model(self, model: str):
        idx = self.model_combo.findData(model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)

    def _open_output(self):
        if self._out_path:
            subprocess.run(["open", self._out_path])

    # ── Settings persistence ─────────────────────────────────────────────

    def _save_settings(self):
        s = self._settings
        save_secret(s, "api_key", self.api_key_edit.text())
        save_secret(s, "hf_token", self.hf_token_edit.text())
        s.setValue("whisper_model", self.model_combo.currentData())
        s.setValue("output_dir", self.output_dir_edit.text())
        s.setValue("ms_client_id", self.ms_client_id_edit.text())
        s.setValue("ms_join_url", self.ms_join_url_edit.text())

    def _load_settings(self):
        s = self._settings
        self.api_key_edit.setText(load_secret(s, "api_key"))
        self.hf_token_edit.setText(load_secret(s, "hf_token"))
        self._select_model(s.value("whisper_model", "medium"))
        self.output_dir_edit.setText(s.value("output_dir", ""))
        self.ms_client_id_edit.setText(s.value("ms_client_id", ""))
        self.ms_join_url_edit.setText(s.value("ms_join_url", ""))

        # Refresh button state based on loaded settings
        self._on_input_changed()
        self._refresh_transcript_info()

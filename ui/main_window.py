import os
import shutil
import subprocess
import time
from datetime import datetime
from urllib.parse import urlparse

from PyQt6.QtCore import Qt, QSettings, QEvent, QPointF, QRectF, QTimer
from PyQt6.QtGui import (
    QAction, QColor, QFont, QKeySequence, QPainter, QPalette, QPen, QPixmap,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QMessageBox, QWidget, QVBoxLayout, QHBoxLayout,
    QGridLayout, QLabel, QLineEdit, QPushButton, QFileDialog,
    QComboBox, QTextEdit, QProgressBar, QGroupBox,
    QFormLayout, QSplitter, QFrame, QSizePolicy, QCheckBox,
)

from pipeline._util import kill_active_processes
from pipeline.teams_downloader import is_teams_url
from pipeline.worker import (
    ProcessingWorker, clear_saved_data, load_saved_transcript, log_path,
)
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

# Share of the whole run each stage typically takes, for the overall bar. With
# equal shares the bar read 34% while transcription — most of the wait — was
# not yet half done. Skipped stages simply complete instantly.
_STAGE_WEIGHTS = [14, 3, 48, 10, 10, 13, 2]

# Everything ffmpeg can read that people actually record into. Audio-only
# files work too; they just produce notes without screenshots.
_MEDIA_EXTS = ("mp4", "m4v", "mov", "mkv", "webm", "avi", "mpeg", "mpg",
               "m4a", "mp3", "wav", "aac")

# Wait this long for a cancelled run to stop when the window is closed
_CLOSE_WAIT_MS = 10_000

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

# Filled buttons and the log surface. White text clears 4.5:1 on both fills;
# the disabled fill sits near the window colour so an unavailable button isn't
# the brightest thing in a dark window (the old #ccc was).
_FILLS = {
    "light": {"primary": "#1a62d6", "primary_hover": "#1550b0",
              "success": "#137333", "success_hover": "#0f5c29",
              "disabled": "#d9d9d9", "disabled_text": "#6b6b6b"},
    "dark":  {"primary": "#1a62d6", "primary_hover": "#2f74e0",
              "success": "#137333", "success_hover": "#1a8a40",
              "disabled": "#4a4a4a", "disabled_text": "#a8a8a8"},
}

_ICON_PX = 14

# Fire-and-forget helpers (`open`, notifications), kept so they are reaped
_helpers: list = []


def _spawn(cmd: list[str]) -> None:
    """Start a short-lived helper without waiting for it."""
    _helpers[:] = [p for p in _helpers if p.poll() is None]
    try:
        _helpers.append(subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    except OSError:
        pass


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
        self._running = False
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
        self._stage_eta = ""
        self._run_warnings: list[str] = []
        self._stale_warning: str | None = None
        self._keep_awake = None      # caffeinate child while a run is in progress
        # Ticks the active row's elapsed time; runs only while a stage is active
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)
        self._total_stages = len(ProcessingWorker.STAGES)
        self._settings = QSettings("AutoNotes", "AutoNotes")
        self._loading_settings = False
        self.setAcceptDrops(True)
        self._build_ui()
        self._build_menu()
        self._load_settings()
        self._connect_autosave()
        self._warn_if_ytdlp_stale()
        self._show_stale_banner()

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

    def _style_surfaces(self):
        f = _FILLS["dark" if self._is_dark() else "light"]
        self.process_btn.setStyleSheet(
            f"QPushButton {{ background: {f['primary']}; color: #ffffff; border-radius: 6px;"
            " font-size: 14px; font-weight: bold; }"
            f"QPushButton:hover {{ background: {f['primary_hover']}; }}"
            f"QPushButton:disabled {{ background: {f['disabled']}; color: {f['disabled_text']}; }}"
        )
        self.open_btn.setStyleSheet(
            f"QPushButton {{ background: {f['success']}; color: #ffffff; border-radius: 6px;"
            " font-size: 13px; font-weight: bold; }"
            f"QPushButton:hover {{ background: {f['success_hover']}; }}"
        )
        pal = self.palette()
        self.log_text.setStyleSheet(
            f"QTextEdit {{ background: {pal.color(QPalette.ColorRole.Base).name()};"
            f" color: {pal.color(QPalette.ColorRole.Text).name()};"
            " border-radius: 4px; padding: 4px; }")

    def _restyle(self):
        for lbl in self.findChildren(QLabel):
            if lbl.property("tone") is not None:
                self._set_tone(lbl, lbl.property("tone"),
                               lbl.property("tone_size"), lbl.property("tone_bold"))

    def changeEvent(self, event):
        if event.type() in (QEvent.Type.PaletteChange,
                            QEvent.Type.ApplicationPaletteChange):
            self._restyle()
            if hasattr(self, "open_btn"):
                self._style_surfaces()
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

        # No in-window "AutoNotes" heading: the title bar already names the app
        subtitle = QLabel("Turns a meeting recording into Word notes with screenshots. "
                          f"Version {__version__}")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_tone(subtitle, "muted", 12)
        root.addWidget(subtitle)

        # Persistent, unlike the log, which every run clears
        self.stale_banner = QLabel("")
        self.stale_banner.setWordWrap(True)
        self.stale_banner.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._set_tone(self.stale_banner, "warning", 12)
        self.stale_banner.setVisible(False)
        root.addWidget(self.stale_banner)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        # The native handle draws a lone grip dot mid-window; keep the drag area
        # but paint nothing
        splitter.setStyleSheet("QSplitter::handle { background: transparent; }")
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

        row = QHBoxLayout()
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText("Paste a Teams, SharePoint or YouTube link, or browse for a video file")
        self.url_edit.textChanged.connect(self._on_input_changed)
        self.url_edit.returnPressed.connect(self._start_if_ready)
        # A long SharePoint link otherwise shows only its URL-encoded tail
        self.url_edit.editingFinished.connect(lambda: self.url_edit.setCursorPosition(0))
        row.addWidget(self.url_edit)

        self.browse_btn = QPushButton("Browse…")
        self.browse_btn.setMinimumWidth(90)
        self.browse_btn.clicked.connect(self._browse_file)
        row.addWidget(self.browse_btn)
        layout.addLayout(row)

        # Shown only once a file is chosen; "No file selected" was a permanent
        # line of noise for the usual paste-a-link case
        self.file_path_label = QLabel("")
        self.file_path_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.file_path_label.setVisible(False)
        layout.addWidget(self.file_path_label)

        self.input_status = QLabel("")
        self._set_tone(self.input_status, "info")
        layout.addWidget(self.input_status)

        return box

    def _build_config_group(self) -> QGroupBox:
        box = QGroupBox("Settings")
        form = QFormLayout(box)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        # The macOS style defaults to FieldsStayAtSizeHint, which leaves every
        # line edit ~180 px wide no matter how wide the column is
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-ant-…")
        self.api_key_edit.textChanged.connect(self._on_input_changed)
        form.addRow("Claude API key:", self.api_key_edit)

        self.hf_token_edit = QLineEdit()
        self.hf_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.hf_token_edit.setPlaceholderText("Optional, adds speaker names")
        form.addRow("Hugging Face token:", self.hf_token_edit)

        self.model_combo = QComboBox()
        for label, model in _QUALITY:
            self.model_combo.addItem(label, model)
        self._select_model("medium")
        form.addRow("Transcription:", self.model_combo)

        hint = QLabel("More accurate transcription takes longer on this Mac.")
        self._set_tone(hint, "muted", 11)
        form.addRow("", hint)

        self.reuse_transcript_check = QCheckBox("Reuse the last transcript instead of transcribing")
        form.addRow("", self.reuse_transcript_check)

        self.reuse_info_label = QLabel("")
        self._set_tone(self.reuse_info_label, "muted", 11)
        form.addRow("", self.reuse_info_label)

        # ── Microsoft Teams / Graph API (optional) ────────────────────────
        # Collapsed unless in use: most runs never need it, and two empty
        # fields with a paragraph of explanation crowded the settings
        self.teams_toggle = QPushButton("\u25b8 Microsoft Teams options")
        self.teams_toggle.setFlat(True)
        self.teams_toggle.setCheckable(True)
        self.teams_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        # Link-coloured so it reads as something to click, not a caption
        self.teams_toggle.setStyleSheet(
            "QPushButton { text-align: left; padding: 2px 0; border: none; color: palette(link); }"
            "QPushButton:disabled { color: palette(placeholder-text); }")
        self.teams_toggle.toggled.connect(self._set_teams_visible)
        form.addRow("", self.teams_toggle)

        self.ms_client_id_edit = QLineEdit()
        self.ms_client_id_edit.setPlaceholderText("Azure app client ID")
        form.addRow("MS client ID:", self.ms_client_id_edit)

        self.ms_join_url_edit = QLineEdit()
        self.ms_join_url_edit.setPlaceholderText("Join link from the meeting's calendar invite")
        form.addRow("Join link:", self.ms_join_url_edit)

        self.ms_hint = QLabel("Both add attendees and the Teams recap. "
                              "The transcript comes from the recording either way.")
        self.ms_hint.setWordWrap(True)
        self._set_tone(self.ms_hint, "muted", 11)
        form.addRow("", self.ms_hint)
        self._config_form = form
        self._set_teams_visible(False)

        return box

    def _set_teams_visible(self, visible: bool):
        for field in (self.ms_client_id_edit, self.ms_join_url_edit, self.ms_hint):
            self._config_form.setRowVisible(field, visible)
        self.teams_toggle.setText(("\u25be" if visible else "\u25b8") + " Microsoft Teams options")
        if self.teams_toggle.isChecked() != visible:
            self.teams_toggle.setChecked(visible)

    def _build_output_group(self) -> QGroupBox:
        box = QGroupBox("Save to")
        row = QHBoxLayout(box)

        self.output_dir_edit = QLineEdit()
        self.output_dir_edit.setPlaceholderText(os.path.expanduser("~/Desktop"))
        row.addWidget(self.output_dir_edit)

        self.output_browse_btn = QPushButton("Browse…")
        self.output_browse_btn.setMinimumWidth(90)
        self.output_browse_btn.clicked.connect(self._browse_output_dir)
        row.addWidget(self.output_browse_btn)

        return box

    def _build_action_bar(self) -> QWidget:
        w = QWidget()
        row = QHBoxLayout(w)
        row.setContentsMargins(0, 0, 0, 0)

        self.process_btn = QPushButton("Generate Notes")
        self.process_btn.setFixedHeight(40)
        self.process_btn.setEnabled(False)
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

        # Log: a titled section like the others, on the theme's text surface
        # rather than a fixed black slab
        log_box = QGroupBox("Log")
        log_layout = QVBoxLayout(log_box)
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setFont(QFont("Menlo", 10))
        self.log_text.setFrameShape(QFrame.Shape.NoFrame)
        log_layout.addWidget(self.log_text)
        # Errors say "the log has the details"; give a way to hand it over
        log_actions = QHBoxLayout()
        log_actions.addStretch()
        copy_log_btn = QPushButton("Copy Log")
        copy_log_btn.clicked.connect(self._copy_log)
        log_actions.addWidget(copy_log_btn)
        reveal_log_btn = QPushButton("Show Log File")
        reveal_log_btn.clicked.connect(self._reveal_log)
        log_actions.addWidget(reveal_log_btn)
        log_layout.addLayout(log_actions)
        layout.addWidget(log_box, stretch=1)

        # Open output buttons
        done_row = QHBoxLayout()
        self.open_btn = QPushButton("Open Notes")
        self.open_btn.setFixedHeight(38)
        self.open_btn.setVisible(False)
        self.open_btn.clicked.connect(self._open_output)
        done_row.addWidget(self.open_btn, stretch=1)
        self.reveal_btn = QPushButton("Show in Finder")
        self.reveal_btn.setFixedHeight(38)
        self.reveal_btn.setVisible(False)
        self.reveal_btn.clicked.connect(self._reveal_output)
        done_row.addWidget(self.reveal_btn)
        layout.addLayout(done_row)

        self._style_surfaces()

        return panel

    def _build_menu(self):
        """Standard Mac menus, so the usual shortcuts work."""
        bar = self.menuBar()

        file_menu = bar.addMenu("File")
        self._add_action(file_menu, "Open Video…", QKeySequence.StandardKey.Open,
                         self._browse_file)
        self._add_action(file_menu, "Choose Save Folder…", "Ctrl+Shift+O",
                         self._browse_output_dir)
        file_menu.addSeparator()
        self.open_notes_action = self._add_action(
            file_menu, "Open Last Notes", "Ctrl+Shift+N", self._open_output)
        self.open_notes_action.setEnabled(False)

        run_menu = bar.addMenu("Run")
        self.generate_action = self._add_action(
            run_menu, "Generate Notes", "Ctrl+Return", self._start_if_ready)
        self.cancel_action = self._add_action(
            run_menu, "Cancel", "Ctrl+.", self._cancel_processing)
        self.cancel_action.setEnabled(False)

        help_menu = bar.addMenu("Help")
        self._add_action(help_menu, "Copy Log", None, self._copy_log)
        self._add_action(help_menu, "Show Log File", None, self._reveal_log)
        help_menu.addSeparator()
        self._add_action(help_menu, "Delete Saved Data…", None, self._confirm_clear_data)

        # Escape cancels too, as in a Mac sheet
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, activated=self._cancel_processing)

    def _add_action(self, menu, text, shortcut, slot) -> QAction:
        action = QAction(text, self)
        if shortcut is not None:
            action.setShortcut(QKeySequence(shortcut))
        action.triggered.connect(slot)
        menu.addAction(action)
        return action

    # ── Slots ────────────────────────────────────────────────────────────

    def _on_input_changed(self):
        # A typed URL replaces a browsed file; otherwise the stale file name
        # stays on screen while _start_processing silently uses the URL
        if self.url_edit.text().strip() and self._local_file:
            self._set_local_file("")

        url = self.url_edit.text().strip()
        has_input = bool(url) or bool(self._local_file)
        has_key = bool(self.api_key_edit.text().strip())
        ready = has_input and has_key
        if self._is_running():
            # "Ready to generate notes" under a running job read as an invitation
            self.input_status.setText("")
            return
        self.process_btn.setEnabled(ready)
        if hasattr(self, "generate_action"):
            self.generate_action.setEnabled(ready)

        if url and not url.startswith(("http://", "https://")) and not os.path.isfile(url):
            self.input_status.setText("Paste a link starting with https://, or use Browse.")
            self._set_tone(self.input_status, "warning")
        elif ready:
            kind = _describe_source(url) if url else "Video file"
            self.input_status.setText(f"{kind}, ready to generate notes.")
            self._set_tone(self.input_status, "success")
        elif has_input and not has_key:
            self.input_status.setText("Add your Claude API key to continue.")
            self._set_tone(self.input_status, "warning")
        else:
            self.input_status.setText("")

    def _is_running(self) -> bool:
        # Our own flag, not QThread.isRunning(): the thread can still report
        # running while its finished() signal is being handled, which left the
        # Generate button disabled after a run
        return self._running

    def _set_local_file(self, path: str):
        self._local_file = path
        self.file_path_label.setText(f"File: {os.path.basename(path)}" if path else "")
        self.file_path_label.setToolTip(path)
        self._set_tone(self.file_path_label, "")
        self.file_path_label.setVisible(bool(path))

    def _browse_file(self):
        if self._is_running():
            return
        patterns = " ".join(f"*.{ext}" for ext in _MEDIA_EXTS)
        path, _ = QFileDialog.getOpenFileName(
            self, "Select Recording", os.path.expanduser("~"),
            f"Recordings ({patterns});;All files (*)"
        )
        if path:
            self._choose_file(path)

    def _choose_file(self, path: str):
        # Clear the URL first: its textChanged must not see the new file
        self.url_edit.clear()
        self._set_local_file(path)
        self._on_input_changed()

    # Drag a recording onto the window, or a link from the browser
    def dragEnterEvent(self, event):
        if not self._is_running() and self._dropped_source(event.mimeData()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        source = self._dropped_source(event.mimeData())
        if not source or self._is_running():
            return
        kind, value = source
        if kind == "file":
            self._choose_file(value)
        else:
            self.url_edit.setText(value)
            self.url_edit.setCursorPosition(0)
        event.acceptProposedAction()

    @staticmethod
    def _dropped_source(mime):
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    path = url.toLocalFile()
                    if os.path.isfile(path) and path.lower().endswith(tuple("." + e for e in _MEDIA_EXTS)):
                        return "file", path
                elif url.scheme() in ("http", "https"):
                    return "url", url.toString()
        if mime.hasText():
            text = mime.text().strip()
            if text.startswith(("http://", "https://")) and "\n" not in text:
                return "url", text
        return None

    def _start_if_ready(self):
        if self.process_btn.isEnabled() and not self._is_running():
            self._start_processing()

    def _browse_output_dir(self):
        if self._is_running():
            return
        start = self.output_dir_edit.text().strip() or os.path.expanduser("~/Desktop")
        d = QFileDialog.getExistingDirectory(self, "Select Output Folder", start)
        if d:
            self.output_dir_edit.setText(d)
            self._save_settings()

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
        self._worker.stage_eta.connect(self._on_stage_eta)
        self._worker.warning.connect(self._on_warning)
        self._worker.log_message.connect(self._on_log)
        self._worker.completed.connect(self._on_completed)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._set_idle)

        self.process_btn.setEnabled(False)
        self.generate_action.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.cancel_action.setEnabled(True)
        self.progress_bar.setVisible(True)
        self.open_btn.setVisible(False)
        self.reveal_btn.setVisible(False)
        self._set_inputs_locked(True)
        self.input_status.setText("")

        self._run_started = time.monotonic()
        self._running = True
        self._clock.start()
        self._start_keep_awake()
        self._worker.start()

    def _set_inputs_locked(self, locked: bool):
        """Settings can't change mid-run: the worker already has its copy, so
        an edit would silently not apply to this run."""
        for w in (self.url_edit, self.browse_btn, self.api_key_edit, self.hf_token_edit,
                  self.model_combo, self.reuse_transcript_check, self.teams_toggle,
                  self.ms_client_id_edit, self.ms_join_url_edit,
                  self.output_dir_edit, self.output_browse_btn):
            w.setEnabled(not locked)
        # A long SharePoint link otherwise shows only its URL-encoded tail
        self.url_edit.setCursorPosition(0)
        if not locked:
            self._refresh_transcript_info()   # re-applies the checkbox's own rule

    def _start_keep_awake(self):
        """Stop the Mac idle-sleeping through an hour-long transcription."""
        self._stop_keep_awake()
        caffeinate = "/usr/bin/caffeinate"
        if os.path.exists(caffeinate):
            try:
                # -w: exits by itself if AutoNotes dies, so it can't leak
                self._keep_awake = subprocess.Popen(
                    [caffeinate, "-i", "-w", str(os.getpid())])
            except OSError:
                self._keep_awake = None

    def _stop_keep_awake(self):
        proc, self._keep_awake = self._keep_awake, None
        if proc is not None and proc.poll() is None:
            proc.terminate()

    def _cancel_processing(self):
        if self._worker and self._worker.isRunning() and not self._cancel_requested:
            # Cooperative cancel: the worker checks the flag at safe points and
            # kills its subprocesses; _set_idle runs when the thread finishes
            self._worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.cancel_btn.setText("Cancelling…")
            self._on_log("Cancelling — waiting for current step to stop…")
            self._cancel_requested = True
            self.cancel_action.setEnabled(False)
            # Don't wait for the next progress line: stop ffmpeg / yt-dlp now
            kill_active_processes()

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
            if self._stage_eta:
                parts.append(self._stage_eta)
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
        self._stage_eta = ""
        self._render_stages()

        self.progress_bar.setValue(max(self.progress_bar.value(), _overall_pct(current, 0)))

    def _on_stage_progress(self, pct: int):
        # Overall = completed stages + fractional progress of the current one,
        # each weighted by how long that stage usually takes
        overall = _overall_pct(self._current_stage, pct)
        self.progress_bar.setValue(max(self.progress_bar.value(), overall))
        self._stage_pct = pct
        idx = self._current_stage - 1
        self.stage_details[idx].setText(self._stage_detail_text(idx))

    def _on_stage_detail(self, stage: int, text: str, skipped: bool):
        i = stage - 1
        self._stage_facts[i] = text
        self._stage_skipped[i] = skipped
        self._render_stages()

    def _on_stage_eta(self, text: str):
        self._stage_eta = text
        idx = self._current_stage - 1
        self.stage_details[idx].setText(self._stage_detail_text(idx))

    def _on_warning(self, text: str):
        """Something the user needs to see now, not find later in the log."""
        if text not in self._run_warnings:
            self._run_warnings.append(text)
        self._set_run_status("\n".join(self._run_warnings), "warning")

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
        self._stale_warning = warning
        if warning:
            self._on_log(f"Warning: {warning}")

    def _show_stale_banner(self):
        self.stale_banner.setText(self._stale_warning or "")
        self.stale_banner.setVisible(bool(self._stale_warning))

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
        saved = f"Notes saved as {os.path.basename(out_path)}" + (f" in {took}." if took else ".")
        if self._run_warnings:
            # A run that fell back to the transcript, or reused the wrong one,
            # is not the same green success as a clean one
            self._set_run_status("\n".join([saved, *self._run_warnings]), "warning")
        else:
            self._set_run_status(saved, "success")
        self.open_btn.setVisible(True)
        self.reveal_btn.setVisible(True)
        self.open_notes_action.setEnabled(True)
        # Reusing is a one-off retry; left ticked, the next recording would
        # silently get this one's transcript
        if self.reuse_transcript_check.isChecked():
            self.reuse_transcript_check.setChecked(False)
        self._notify("Notes ready", os.path.basename(out_path))

    def _on_error(self, msg: str):
        self._on_log(f"\nERROR: {msg}")
        self._run_finished = True
        idx = self._current_stage - 1
        self._close_stage(idx, "failed")
        self._render_stages()
        self.progress_bar.setFormat("Stopped")
        explanation = explain_error(msg, _STAGES[idx][1])
        self._set_run_status(explanation, "error")
        # The raw error stays reachable without being the headline
        self.run_status.setToolTip(msg)
        self._notify("Couldn't make notes", explanation)

    def _set_idle(self):
        self._running = False
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
        self.cancel_action.setEnabled(False)
        self._stop_keep_awake()
        self._set_inputs_locked(False)
        self._on_input_changed()

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
        self._stage_eta = ""
        self._run_warnings = []
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
        self.reveal_btn.setVisible(False)

    def _select_model(self, model: str):
        idx = self.model_combo.findData(model)
        if idx >= 0:
            self.model_combo.setCurrentIndex(idx)

    def _open_output(self):
        if self._out_path and os.path.exists(self._out_path):
            # Popen: `open` can take a moment when Word is starting, and
            # waiting on it froze the window
            _spawn(["open", self._out_path])

    def _reveal_output(self):
        if self._out_path and os.path.exists(self._out_path):
            _spawn(["open", "-R", self._out_path])

    def _copy_log(self):
        QApplication.clipboard().setText(self.log_text.toPlainText())
        self.statusBar().showMessage("Log copied to the clipboard.", 3000)

    def _reveal_log(self):
        path = log_path()
        if os.path.exists(path):
            _spawn(["open", "-R", path])
        else:
            self.statusBar().showMessage("No log file yet.", 3000)

    def _confirm_clear_data(self):
        if self._is_running():
            return
        answer = QMessageBox.question(
            self, "Delete saved data?",
            "This deletes the log, the saved transcript, the last run's screenshots "
            "and Claude responses, and your Microsoft sign-in.\n\n"
            "Your notes documents, settings and API keys are kept.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel)
        if answer == QMessageBox.StandardButton.Yes:
            clear_saved_data()
            self._refresh_transcript_info()
            self.log_text.clear()
            self.statusBar().showMessage("Saved data deleted.", 4000)

    def _notify(self, title: str, message: str):
        """Tell someone who has switched away that the run has ended."""
        # Offscreen means no one is looking at a screen (tests, the agent driver)
        if self.isActiveWindow() or QApplication.platformName() == "offscreen":
            return
        QApplication.alert(self)   # Dock bounce
        osascript = "/usr/bin/osascript"
        if os.path.exists(osascript):
            # Text goes in as argv, never spliced into the script
            _spawn([osascript,
                    "-e", "on run argv",
                    "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
                    "-e", "end run",
                    "--", f"AutoNotes: {title}", message[:200]])

    def closeEvent(self, event):
        """Don't let a close abandon a run: the worker would be killed mid-step,
        leaving its temp folder (a full copy of the recording) behind."""
        if self._is_running():
            answer = QMessageBox.question(
                self, "Stop and quit?",
                "Notes are still being generated. Quitting stops the run and "
                "nothing is saved.",
                QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
                QMessageBox.StandardButton.Cancel)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if not self._shutdown_worker():
                # A step that can't be interrupted (loading a model) is still
                # running. Destroying a live thread crashes Qt, and deleting its
                # temp folder under it is unsafe, so finish quitting once it
                # stops; its own cleanup then removes the temp folder.
                event.ignore()
                self.hide()
                self._worker.finished.connect(QApplication.quit)
                return
        self._save_settings()
        self._stop_keep_awake()
        super().closeEvent(event)

    def _shutdown_worker(self) -> bool:
        """Cancel the run and wait for it. False if it is still running."""
        worker = self._worker
        if worker is None:
            return True
        self._save_settings()
        worker.cancel()
        kill_active_processes()
        if not worker.wait(_CLOSE_WAIT_MS):
            return False
        # The worker's own cleanup has run; this only covers it failing
        if worker.temp_dir:
            shutil.rmtree(worker.temp_dir, ignore_errors=True)
        return True

    # ── Settings persistence ─────────────────────────────────────────────

    def _connect_autosave(self):
        """Save each setting as it changes, not only when Generate is clicked —
        a key typed in and the app quit was otherwise lost."""
        for edit in (self.api_key_edit, self.hf_token_edit, self.output_dir_edit,
                     self.ms_client_id_edit, self.ms_join_url_edit):
            edit.editingFinished.connect(self._save_settings)
        self.model_combo.currentIndexChanged.connect(self._save_settings)

    def _save_settings(self):
        if self._loading_settings:
            return
        s = self._settings
        in_keychain = all([
            save_secret(s, "api_key", self.api_key_edit.text()) is not False,
            save_secret(s, "hf_token", self.hf_token_edit.text()) is not False,
        ])
        s.setValue("whisper_model", self.model_combo.currentData())
        s.setValue("output_dir", self.output_dir_edit.text())
        s.setValue("ms_client_id", self.ms_client_id_edit.text())
        s.setValue("ms_join_url", self.ms_join_url_edit.text())
        if not in_keychain and not getattr(self, "_warned_keychain", False):
            self._warned_keychain = True
            self._on_log("Warning: the macOS Keychain couldn't be used, so your API "
                         "keys are saved in AutoNotes' preferences file instead.")

    def _load_settings(self):
        self._loading_settings = True
        try:
            s = self._settings
            self.api_key_edit.setText(load_secret(s, "api_key"))
            self.hf_token_edit.setText(load_secret(s, "hf_token"))
            self._select_model(s.value("whisper_model", "medium"))
            self.output_dir_edit.setText(s.value("output_dir", ""))
            self.ms_client_id_edit.setText(s.value("ms_client_id", ""))
            self.ms_join_url_edit.setText(s.value("ms_join_url", ""))
            # setText leaves the view scrolled to the end, which shows the meeting
            # token; show the recognisable teams.microsoft.com start instead
            self.ms_join_url_edit.setCursorPosition(0)
            self._set_teams_visible(bool(self.ms_client_id_edit.text().strip()
                                         or self.ms_join_url_edit.text().strip()))
        finally:
            self._loading_settings = False

        # Refresh button state based on loaded settings
        self._on_input_changed()
        self._refresh_transcript_info()


def _overall_pct(stage: int, stage_pct: int) -> int:
    """Whole-run progress from the current stage (1-based) and its own 0-100."""
    done = sum(_STAGE_WEIGHTS[:stage - 1])
    current = _STAGE_WEIGHTS[stage - 1] * max(0, min(100, stage_pct)) / 100
    return int((done + current) / sum(_STAGE_WEIGHTS) * 100)


def _describe_source(url: str) -> str:
    """What kind of recording a link is, for the status line under it."""
    if is_teams_url(url):
        return "Teams or SharePoint recording"
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "Link"
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "YouTube video"
    if os.path.isfile(url):
        return "Video file"
    return "Web video"

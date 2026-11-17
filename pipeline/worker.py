import json
import os
import shutil
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from typing import ClassVar
from datetime import datetime
from urllib.parse import urlparse

from PyQt6.QtCore import QThread, pyqtSignal

from pipeline._util import PipelineCancelled, private_dir, safe_filename, write_private
from pipeline.downloader import download_youtube, find_transcript
from pipeline.teams_downloader import is_teams_url, download_teams_recording
from pipeline.transcriber import extract_audio, transcribe
from pipeline.diarizer import diarize
from pipeline.frame_extractor import extract_frames
from pipeline.note_generator import check_api_key, generate_notes
from output.docx_writer import check_output_dir, write_docx

# Debug artifacts from the most recent run (log + selected frames),
# kept outside the temp dir so they survive pipeline cleanup. They hold
# meeting content, so the directory and its files are owner-only.
DEBUG_DIR = os.path.expanduser("~/Library/Logs/AutoNotes")

_TEMP_PREFIX = "autonotes_"
# A pipeline temp dir older than this belongs to a run that crashed or was
# force-quit; it holds a full copy of the recording, so it is swept at launch.
_STALE_TEMP_SECONDS = 6 * 3600

# The log is appended to on every run; keep it bounded.
_LOG_MAX_BYTES = 2 * 1024 * 1024


@dataclass
class _Run:
    """What the stages learn about the recording, passed from one to the next."""
    source_type: str = ""
    is_url: bool = False
    video_path: str = ""
    title: str = ""
    description: str = ""
    yt_chapters: list = field(default_factory=list)
    attendees: list = field(default_factory=list)
    ai_notes: str | None = None
    segments: list | None = None      # transcript, once known
    transcript_origin: str | None = None
    audio_path: str | None = None


class ProcessingWorker(QThread):
    stage_changed = pyqtSignal(str, int, int)   # label, current (1-based), total
    stage_progress = pyqtSignal(int)            # 0-100 within current stage
    stage_detail = pyqtSignal(int, str, bool)   # stage (1-based), one-line fact, skipped
    stage_eta = pyqtSignal(str)                 # e.g. "about 14m left" for the active stage
    warning = pyqtSignal(str)                   # something the user should see, not just the log
    log_message = pyqtSignal(str)
    completed = pyqtSignal(str)                 # output file path
    error = pyqtSignal(str)

    STAGES: ClassVar[list[str]] = [
        "Downloading / loading video",
        "Extracting audio",
        "Transcribing speech",
        "Identifying speakers",
        "Extracting screenshots",
        "Generating AI notes",
        "Writing document",
    ]

    def __init__(self, input_source: str, config: dict):
        super().__init__()
        self.input_source = input_source
        self.config = config
        self._cancelled = False
        self.temp_dir: str | None = None

    def cancel(self):
        self._cancelled = True

    def run(self):
        temp_dir = tempfile.mkdtemp(prefix=_TEMP_PREFIX)
        self.temp_dir = temp_dir
        try:
            self._run_pipeline(temp_dir)
        except PipelineCancelled:
            self._log("Processing cancelled.")
        except Exception as e:
            self._log(f"Pipeline error:\n{traceback.format_exc()}")
            if not self._cancelled:
                self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    # ── Pipeline ─────────────────────────────────────────────────────────

    def _run_pipeline(self, temp_dir: str):
        total = len(self.STAGES)
        output_dir = self.config.get("output_dir", os.path.expanduser("~/Desktop"))
        problem = check_output_dir(output_dir)
        if problem:
            raise RuntimeError(problem)
        self._preflight_api_key()

        run = _Run()
        self._stage(1, total)
        self._load_recording(run, temp_dir)
        self._maybe_reuse_transcript(run)

        self._stage(2, total)
        self._extract_audio(run, temp_dir)

        self._stage(3, total)
        self._transcribe(run)

        self._stage(4, total)
        self._identify_speakers(run)
        if run.transcript_origin != "Reused from last run":
            # Saving a reused transcript would relabel it with this video's
            # title, and the different-video warning would never fire again
            self._save_debug_transcript(run.segments, run.title)

        self._stage(5, total)
        frames = self._extract_screenshots(run, temp_dir)

        self._stage(6, total)
        notes = self._generate_notes(run, frames)

        self._stage(7, total)
        self._log("Writing document…")
        out_path = self._write_document(notes, frames, output_dir,
                                        safe_filename(run.title), self._source_info(run))
        self._progress(100)
        self._log(f"Saved: {out_path}")
        # The file name is in the completion line; the row says where it went
        self._detail(7, f"Saved to {os.path.basename(os.path.dirname(out_path))}")

        self.completed.emit(out_path)

    def _preflight_api_key(self):
        """Reject a bad key before the download, not after an hour of transcribing."""
        key = (self.config.get("anthropic_key") or "").strip()
        if not key:
            raise RuntimeError("Add your Claude API key under Settings, then try again.")
        note = check_api_key(key)
        if note:
            self._log(note)

    def _load_recording(self, run: _Run, temp_dir: str):
        """Stage 1: download the recording, or point at the local file."""
        run.is_url = self.input_source.startswith(("http://", "https://"))

        if run.is_url and is_teams_url(self.input_source):
            run.source_type = "Teams / SharePoint"
            self._log("Detected Teams/SharePoint URL — downloading recording…")
            teams = download_teams_recording(
                self.input_source, temp_dir,
                progress_cb=self._progress,
                log_cb=self._log,
                cancel_check=self._check_cancel,
            )
            run.video_path = teams["video_path"]
            run.title = teams["title"]
            run.description = teams["description"]
            run.segments = teams["transcript_segments"]
            self._fetch_meeting_context(run)

        elif run.is_url:
            run.source_type = _web_source_type(self.input_source)
            self._log(f"Downloading from {run.source_type}…")
            run.video_path, run.title, run.description, run.yt_chapters = download_youtube(
                self.input_source, temp_dir,
                progress_cb=self._progress,
                log_cb=self._log,
                cancel_check=self._check_cancel,
            )
            if run.yt_chapters:
                self._log(f"Found {len(run.yt_chapters)} chapters")
        else:
            run.source_type = "Local file"
            if not os.path.isfile(self.input_source):
                raise RuntimeError(f"The file {self.input_source} no longer exists.")
            run.video_path = self.input_source
            run.title = os.path.splitext(os.path.basename(run.video_path))[0]
            self._progress(100)

        if run.segments:
            run.transcript_origin = "From the Teams recording"
        self._log(f"Video: {run.title}")
        self._detail(1, run.source_type + (", transcript included" if run.segments else ""))

    def _fetch_meeting_context(self, run: _Run):
        """Attendees, AI recap and title from Microsoft Graph, when configured.

        Transcripts come from the recording itself (sharepoint_transcript.py),
        so Graph is only consulted for these.
        """
        ms_client_id = self.config.get("ms_client_id", "").strip()
        join_url = self.config.get("ms_join_url", "").strip()
        if ms_client_id and join_url:
            from pipeline.graph_client import fetch_meeting_context
            ctx = fetch_meeting_context(ms_client_id, join_url, log_cb=self._log)
            self._check_cancel()   # sign-in can take minutes; honour a cancel made meanwhile
            run.attendees = ctx["attendees"]
            run.ai_notes = ctx["ai_notes"]
            if ctx["title"]:
                run.title = ctx["title"]
        elif ms_client_id and not join_url:
            self._log("MS Client ID set but no Join URL provided — skipping Graph API")

    def _maybe_reuse_transcript(self, run: _Run):
        """Use the transcript saved by a previous run, if asked to.

        Skips audio extraction, Whisper and diarization. A transcript that came
        with the download (Teams VTT / Stream API) still takes priority.
        """
        if not self.config.get("reuse_transcript") or run.segments is not None:
            return
        saved_segments, meta = load_saved_transcript()
        if not saved_segments:
            self._log("No saved transcript found — transcribing normally")
            return
        run.segments = saved_segments
        run.transcript_origin = "Reused from last run"
        src = meta.get("video", "")
        self._log(f"Reusing saved transcript ({len(saved_segments)} segments"
                  + (f", from '{src}'" if src else "") + ")")
        if src and src != run.title:
            message = (f"The reused transcript is from a different recording "
                       f"(“{src}”). Untick “Reuse the last transcript” "
                       "and run again if that wasn't intended.")
            self._log(f"WARNING: {message}")
            self.warning.emit(message)

    def _extract_audio(self, run: _Run, temp_dir: str):
        """Stage 2: audio for Whisper — unless a transcript is already in hand."""
        if run.segments is not None:
            self._log(f"Transcript available ({len(run.segments)} segments) — "
                      "skipping audio extraction")
            self._progress(100)
            self._detail(2, "Not needed, transcript available", skipped=True)
            return
        if run.is_url:
            captions = find_transcript(temp_dir)
            if captions:
                run.segments = captions
                run.transcript_origin = "From the video's captions"
                self._log(f"Captions found: {len(captions)} segments — skipping audio extraction")
                self._progress(100)
                self._detail(2, "Not needed, captions available", skipped=True)
                return
            self._log("No transcript available — extracting audio…")
        else:
            self._log("Extracting audio track…")
        run.audio_path = extract_audio(run.video_path, temp_dir,
                                       progress_cb=self._progress,
                                       cancel_check=self._check_cancel)

    def _transcribe(self, run: _Run):
        """Stage 3: Whisper, unless a transcript is already in hand."""
        if run.segments is not None:
            self._log(f"Using pre-existing transcript ({len(run.segments)} segments) "
                      "— skipping Whisper")
            self._progress(100)
            self._detail(3, f"{run.transcript_origin}, {_lines(run.segments)}", skipped=True)
            return
        model = self.config.get("whisper_model", "medium")
        self._log(f"Transcribing with Whisper ({model})… (this may take a while)")
        run.segments = transcribe(run.audio_path, model_size=model,
                                  progress_cb=self._progress,
                                  cancel_check=self._check_cancel,
                                  log_cb=self._log,
                                  eta_cb=self.stage_eta.emit)
        if not run.segments:
            raise RuntimeError("No speech was found in this recording, so there is "
                               "nothing to write notes from.")
        self._log(f"Transcription: {len(run.segments)} segments")
        self._detail(3, _lines(run.segments))

    def _identify_speakers(self, run: _Run):
        """Stage 4: pyannote speaker labels, when there is a token and no labels yet."""
        hf_token = self.config.get("hf_token", "").strip() or None
        if run.audio_path is None:
            self._log("Skipping speaker diarization (transcript already has speaker labels)")
            self._progress(100)
            self._detail(4, "Not needed with an existing transcript", skipped=True)
        elif hf_token:
            self._log("Identifying speakers with pyannote…")
            run.segments = diarize(run.audio_path, run.segments, hf_token,
                                   progress_cb=self._progress, log_cb=self._log,
                                   cancel_check=self._check_cancel)
            speakers = {s.get("speaker") for s in run.segments} - {None, "Speaker"}
            self._detail(4, f"{len(speakers)} speakers" if speakers
                         else "No separate speakers found")
        else:
            self._log("No HuggingFace token — skipping speaker diarization")
            run.segments = diarize(run.audio_path, run.segments, None,
                                   progress_cb=self._progress, log_cb=self._log)
            self._detail(4, "Add a Hugging Face token to name speakers", skipped=True)

    def _extract_screenshots(self, run: _Run, temp_dir: str) -> list[dict]:
        """Stage 5."""
        self._log("Extracting and scoring candidate screenshots…")
        frames = extract_frames(
            run.video_path, temp_dir,
            progress_cb=self._progress,
            segments=run.segments,
            cancel_check=self._check_cancel,
            log_cb=self._log,
        )
        cropped_count = sum(1 for f in frames if f.get("cropped"))
        self._log(f"Selected {len(frames)} candidate screenshots "
                  f"({cropped_count} cropped to detected presentation screen)")
        self._save_debug_frames(frames)
        self._detail(5, f"{len(frames)} screenshots" if frames else "None found")
        return frames

    def _generate_notes(self, run: _Run, frames: list[dict]) -> dict:
        """Stage 6."""
        self._log("Sending to Claude for structured note generation…")
        notes = generate_notes(
            run.segments, frames, run.title,
            api_key=self.config["anthropic_key"],
            progress_cb=self._progress,
            description=run.description,
            yt_chapters=run.yt_chapters or None,
            attendees=run.attendees or None,
            ai_notes=run.ai_notes,
            cancel_check=self._check_cancel,
            log_cb=self._log,
        )
        chapters = notes.get("chapters", [])
        cited = sum(
            1 for ch in chapters for kp in ch.get("key_points", [])
            if kp.get("screenshot_idx") is not None
        )
        boxes = notes.get("screenshot_boxes") or {}
        self._log(f"Generated {len(chapters)} chapters ({cited} screenshots "
                  f"referenced, {len(boxes)} with content crops)")
        if notes.get("degraded"):
            self.warning.emit(f"{notes['degraded']}, so the document contains the "
                              "transcript instead of AI notes. Tick “Reuse the last "
                              "transcript” and run again to retry without transcribing.")
            self._detail(6, "Transcript only")
        else:
            self._detail(6, f"{len(chapters)} chapters")
        return notes

    def _source_info(self, run: _Run) -> dict:
        """Recording metadata shown at the top of the document.

        The summary prefers the Teams AI recap (when Graph is configured) over
        the video/meeting description.
        """
        if run.ai_notes:
            summary, label, is_description = run.ai_notes, "Teams recap", False
        else:
            summary, label, is_description = run.description, "Description", True
        return {
            "type": run.source_type,
            "url": self.input_source,
            "summary": (summary or "").strip(),
            "summary_label": label,
            "summary_is_description": is_description,
        }

    def _write_document(self, notes, frames, output_dir, safe_title, source_info):
        """Save the DOCX, falling back to the log dir if the output folder
        stopped being writable mid-run — never throw away a finished run."""
        try:
            return write_docx(notes, frames, output_dir, safe_title,
                              log_cb=self._log, source_info=source_info)
        except OSError as e:
            self._log(f"Could not save to {output_dir} ({e.strerror or e}) — "
                      f"saving to {DEBUG_DIR} instead")
            return write_docx(notes, frames, private_dir(DEBUG_DIR), safe_title,
                              log_cb=self._log, source_info=source_info)

    # ── Signals and checkpoints ──────────────────────────────────────────

    def _check_cancel(self):
        if self._cancelled:
            raise PipelineCancelled()

    def _stage(self, n: int, total: int):
        self._check_cancel()
        label = self.STAGES[n - 1]
        self.stage_changed.emit(label, n, total)
        self._log(f"[{n}/{total}] {label}")

    def _detail(self, n: int, text: str, skipped: bool = False):
        self.stage_detail.emit(n, text, skipped)

    def _progress(self, pct: int):
        self.stage_progress.emit(max(0, min(100, pct)))

    def _log(self, msg: str):
        self.log_message.emit(msg)
        append_log(msg)

    # ── Debug artifacts ──────────────────────────────────────────────────

    def _save_debug_transcript(self, segments: list[dict], title: str):
        """Persist the transcript so a later-stage failure doesn't cost the
        (potentially hours-long) transcription again on a retry."""
        try:
            path = os.path.join(private_dir(DEBUG_DIR), "last_transcript.json")
            data = {
                "video": title,
                "saved": datetime.now().isoformat(timespec="seconds"),
                "segments": segments,
            }
            write_private(path, json.dumps(data, ensure_ascii=False, indent=1))
            self._log(f"Transcript saved to {path}")
        except (OSError, TypeError, ValueError):
            pass

    def _save_debug_frames(self, frames: list[dict]):
        """Copy selected screenshots to the debug dir (replacing the last run's)."""
        try:
            dest = os.path.join(private_dir(DEBUG_DIR), "last_run_frames")
            shutil.rmtree(dest, ignore_errors=True)
            private_dir(dest)
            for i, frame in enumerate(frames):
                ts = int(frame["timestamp"])
                crop = "_cropped" if frame.get("cropped") else ""
                name = f"{i + 1:02d}_t{ts // 60:02d}m{ts % 60:02d}s{crop}.jpg"
                shutil.copy(frame["path"], os.path.join(dest, name))
        except OSError:
            pass


def _web_source_type(url: str) -> str:
    """Name the site a non-Teams URL came from, for the log and the document."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "Web video"
    if host == "youtu.be" or host == "youtube.com" or host.endswith(".youtube.com"):
        return "YouTube"
    if "kaltura" in host or "entry_id=" in url:
        return "Kaltura"
    if host == "vimeo.com" or host.endswith(".vimeo.com"):
        return "Vimeo"
    return "Web video"


def _lines(segments: list[dict]) -> str:
    return f"{len(segments):,} lines"


def append_log(msg: str) -> None:
    """Append to the persistent log, rotating it once it passes _LOG_MAX_BYTES."""
    try:
        private_dir(DEBUG_DIR)
        path = os.path.join(DEBUG_DIR, "autonotes.log")
        try:
            if os.path.getsize(path) > _LOG_MAX_BYTES:
                os.replace(path, path + ".1")
        except OSError:
            pass
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def log_path() -> str:
    return os.path.join(DEBUG_DIR, "autonotes.log")


def clear_saved_data() -> None:
    """Delete everything AutoNotes keeps between runs.

    The log, the saved transcript, the last run's screenshots and raw Claude
    responses, and the Microsoft sign-in. Settings and API keys are left alone.
    """
    shutil.rmtree(DEBUG_DIR, ignore_errors=True)
    try:
        from pipeline.graph_client import clear_token_cache
        clear_token_cache()
    except Exception:
        pass


def sweep_stale_temp_dirs(max_age: float = _STALE_TEMP_SECONDS) -> int:
    """Remove pipeline temp dirs left behind by a crash or force-quit.

    Each holds a full copy of a downloaded recording. Returns how many went.
    """
    root = tempfile.gettempdir()
    cutoff = time.time() - max_age
    removed = 0
    try:
        entries = os.listdir(root)
    except OSError:
        return 0
    for name in entries:
        if not name.startswith(_TEMP_PREFIX):
            continue
        path = os.path.join(root, name)
        try:
            if os.path.isdir(path) and os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def load_saved_transcript() -> tuple[list[dict], dict]:
    """Load the transcript saved by a previous run.

    Returns (segments, metadata); ([], {}) if absent or unreadable. Handles
    both the current {"video", "saved", "segments"} format and the bare
    segment list written by 1.4.4.
    """
    path = os.path.join(DEBUG_DIR, "last_transcript.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return [], {}
    if isinstance(data, list):
        return data, {}
    if not isinstance(data, dict):
        return [], {}
    segments = data.get("segments")
    if not isinstance(segments, list):
        return [], {}
    return segments, {k: v for k, v in data.items() if k != "segments"}

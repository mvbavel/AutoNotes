import os
import re
import shutil
import tempfile

from PyQt6.QtCore import QThread, pyqtSignal

from pipeline.downloader import download_youtube, find_transcript
from pipeline.transcriber import extract_audio, transcribe
from pipeline.diarizer import diarize
from pipeline.frame_extractor import extract_frames
from pipeline.note_generator import generate_notes
from output.docx_writer import write_docx


class ProcessingWorker(QThread):
    stage_changed = pyqtSignal(str, int, int)   # label, current (1-based), total
    stage_progress = pyqtSignal(int)            # 0-100 within current stage
    log_message = pyqtSignal(str)
    completed = pyqtSignal(str)                 # output file path
    error = pyqtSignal(str)

    STAGES = [
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

    def cancel(self):
        self._cancelled = True

    def run(self):
        temp_dir = tempfile.mkdtemp(prefix="autonotes_")
        try:
            self._run_pipeline(temp_dir)
        except Exception as e:
            if not self._cancelled:
                self.error.emit(str(e))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _run_pipeline(self, temp_dir: str):
        total = len(self.STAGES)

        # ── Stage 1: Download / load ──────────────────────────────────────
        self._stage(1, total)
        is_url = self.input_source.startswith(("http://", "https://"))
        description = ""
        yt_chapters = []
        if is_url:
            self._log("Downloading from YouTube…")
            video_path, title, description, yt_chapters = download_youtube(
                self.input_source, temp_dir,
                progress_cb=self._progress,
                log_cb=self._log,
            )
            if yt_chapters:
                self._log(f"Found {len(yt_chapters)} YouTube chapters")
        else:
            video_path = self.input_source
            title = os.path.splitext(os.path.basename(video_path))[0]
            self._progress(100)

        self._log(f"Video: {title}")
        safe_title = _safe_filename(title)

        # ── Stage 2: Audio extraction (skip if YouTube transcript found) ──
        self._stage(2, total)
        yt_segments = None
        if is_url:
            yt_segments = find_transcript(temp_dir)
            if yt_segments:
                self._log(f"YouTube transcript found: {len(yt_segments)} segments — skipping audio extraction")
                self._progress(100)
                audio_path = None
            else:
                self._log("No YouTube transcript available — extracting audio…")
                audio_path = extract_audio(video_path, temp_dir, progress_cb=self._progress)
        else:
            self._log("Extracting audio track…")
            audio_path = extract_audio(video_path, temp_dir, progress_cb=self._progress)

        # ── Stage 3: Transcription ────────────────────────────────────────
        self._stage(3, total)
        if yt_segments is not None:
            segments = yt_segments
            self._log(f"Using YouTube transcript ({len(segments)} segments) — skipping Whisper")
            self._progress(100)
        else:
            model = self.config.get("whisper_model", "medium")
            self._log(f"Transcribing with Whisper ({model})… (this may take a while)")
            segments = transcribe(audio_path, model_size=model, progress_cb=self._progress)
            self._log(f"Transcription: {len(segments)} segments")

        # ── Stage 4: Speaker diarization ──────────────────────────────────
        self._stage(4, total)
        hf_token = self.config.get("hf_token", "").strip() or None
        if yt_segments is not None:
            # No audio available for diarization when using YouTube transcript
            self._log("Skipping speaker diarization (YouTube transcript used)")
            self._progress(100)
        elif hf_token:
            self._log("Identifying speakers with pyannote…")
            segments = diarize(audio_path, segments, hf_token, progress_cb=self._progress)
        else:
            self._log("No HuggingFace token — skipping speaker diarization")
            segments = diarize(audio_path, segments, None, progress_cb=self._progress)

        # ── Stage 5: Frame extraction ─────────────────────────────────────
        self._stage(5, total)
        self._log("Extracting and scoring candidate screenshots…")
        frames = extract_frames(video_path, temp_dir, progress_cb=self._progress)
        self._log(f"Selected {len(frames)} candidate screenshots")

        # ── Stage 6: AI note generation ───────────────────────────────────
        self._stage(6, total)
        self._log("Sending to Claude for structured note generation…")
        notes = generate_notes(
            segments, frames, title,
            api_key=self.config["anthropic_key"],
            progress_cb=self._progress,
            description=description,
            yt_chapters=yt_chapters,
        )
        chapters = notes.get("chapters", [])
        self._log(f"Generated {len(chapters)} chapters")

        # ── Stage 7: Write DOCX ───────────────────────────────────────────
        self._stage(7, total)
        self._log("Writing document…")
        output_dir = self.config.get("output_dir", os.path.expanduser("~/Desktop"))
        out_path = write_docx(notes, frames, output_dir, safe_title)
        self._progress(100)
        self._log(f"Saved: {out_path}")

        self.completed.emit(out_path)

    def _stage(self, n: int, total: int):
        label = self.STAGES[n - 1]
        self.stage_changed.emit(label, n, total)
        self._log(f"[{n}/{total}] {label}")

    def _progress(self, pct: int):
        self.stage_progress.emit(max(0, min(100, pct)))

    def _log(self, msg: str):
        self.log_message.emit(msg)


def _safe_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "_", name)[:80]

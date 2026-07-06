import json
import os
import shutil
import tempfile
import traceback
from datetime import datetime

from PyQt6.QtCore import QThread, pyqtSignal

from pipeline._util import PipelineCancelled, safe_filename
from pipeline.downloader import download_youtube, find_transcript
from pipeline.teams_downloader import is_teams_url, download_teams_recording
from pipeline.transcriber import extract_audio, transcribe
from pipeline.diarizer import diarize
from pipeline.frame_extractor import extract_frames
from pipeline.note_generator import generate_notes
from output.docx_writer import write_docx

# Debug artifacts from the most recent run (log + selected frames),
# kept outside the temp dir so they survive pipeline cleanup
DEBUG_DIR = os.path.expanduser("~/Library/Logs/AutoNotes")


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
        except PipelineCancelled:
            self._log("Processing cancelled.")
        except Exception as e:
            self._log(f"Pipeline error:\n{traceback.format_exc()}")
            if not self._cancelled:
                self.error.emit(f"{type(e).__name__}: {e}")
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _run_pipeline(self, temp_dir: str):
        total = len(self.STAGES)

        # ── Stage 1: Download / load ──────────────────────────────────────
        self._stage(1, total)
        is_url = self.input_source.startswith(("http://", "https://"))
        description = ""
        yt_chapters = []
        attendees: list[str] = []
        ai_notes: str | None = None
        pre_segments = None   # transcript segments from download (VTT/SRT/Graph)

        if is_url and is_teams_url(self.input_source):
            self._log("Detected Teams/SharePoint URL — downloading recording…")
            teams = download_teams_recording(
                self.input_source, temp_dir,
                progress_cb=self._progress,
                log_cb=self._log,
                cancel_check=self._check_cancel,
            )
            video_path = teams["video_path"]
            title = teams["title"]
            description = teams["description"]
            pre_segments = teams["transcript_segments"]

            # Fetch Graph API metadata if client ID is configured
            ms_client_id = self.config.get("ms_client_id", "").strip()
            join_url = self.config.get("ms_join_url", "").strip()
            if ms_client_id and join_url:
                from pipeline.graph_client import fetch_meeting_context
                ctx = fetch_meeting_context(ms_client_id, join_url, log_cb=self._log)
                attendees = ctx["attendees"]
                ai_notes = ctx["ai_notes"]
                # Graph transcript takes priority over yt-dlp VTT
                if ctx["transcript_vtt"]:
                    from pipeline.vtt_parser import parse_vtt
                    tmp_vtt = os.path.join(temp_dir, "graph_transcript.vtt")
                    with open(tmp_vtt, "w", encoding="utf-8") as f:
                        f.write(ctx["transcript_vtt"])
                    parsed = parse_vtt(tmp_vtt)
                    if parsed:
                        pre_segments = parsed
                        self._log(f"Using Graph API transcript ({len(pre_segments)} segments)")
                if ctx["title"]:
                    title = ctx["title"]
            elif ms_client_id and not join_url:
                self._log("MS Client ID set but no Join URL provided — skipping Graph API")

        elif is_url:
            self._log("Downloading from YouTube…")
            video_path, title, description, yt_chapters = download_youtube(
                self.input_source, temp_dir,
                progress_cb=self._progress,
                log_cb=self._log,
                cancel_check=self._check_cancel,
            )
            if yt_chapters:
                self._log(f"Found {len(yt_chapters)} YouTube chapters")
        else:
            video_path = self.input_source
            title = os.path.splitext(os.path.basename(video_path))[0]
            self._progress(100)

        self._log(f"Video: {title}")
        safe_title = safe_filename(title)

        # Optional: reuse the transcript saved by a previous run (skips audio
        # extraction, Whisper, and diarization). A transcript that came with
        # the download (Teams VTT / Graph) still takes priority.
        if self.config.get("reuse_transcript") and pre_segments is None:
            saved_segments, meta = load_saved_transcript()
            if saved_segments:
                pre_segments = saved_segments
                src = meta.get("video", "")
                self._log(f"Reusing saved transcript ({len(saved_segments)} segments"
                          + (f", from '{src}'" if src else "") + ")")
                if src and src != title:
                    self._log(f"WARNING: saved transcript is from a different video "
                              f"('{src}') — uncheck 'Reuse last transcript' if unintended")
            else:
                self._log("No saved transcript found — transcribing normally")

        # ── Stage 2: Audio extraction (skip if transcript already available) ──
        self._stage(2, total)
        if pre_segments is not None:
            self._log(f"Transcript available ({len(pre_segments)} segments) — skipping audio extraction")
            self._progress(100)
            audio_path = None
        elif is_url:
            # Check for yt-dlp downloaded SRT (YouTube)
            srt_segs = find_transcript(temp_dir)
            if srt_segs:
                pre_segments = srt_segs
                self._log(f"YouTube transcript found: {len(pre_segments)} segments — skipping audio extraction")
                self._progress(100)
                audio_path = None
            else:
                self._log("No transcript available — extracting audio…")
                audio_path = extract_audio(video_path, temp_dir,
                                           progress_cb=self._progress,
                                           cancel_check=self._check_cancel)
        else:
            self._log("Extracting audio track…")
            audio_path = extract_audio(video_path, temp_dir,
                                       progress_cb=self._progress,
                                       cancel_check=self._check_cancel)

        # ── Stage 3: Transcription ────────────────────────────────────────
        self._stage(3, total)
        if pre_segments is not None:
            segments = pre_segments
            self._log(f"Using pre-existing transcript ({len(segments)} segments) — skipping Whisper")
            self._progress(100)
        else:
            model = self.config.get("whisper_model", "medium")
            self._log(f"Transcribing with Whisper ({model})… (this may take a while)")
            segments = transcribe(audio_path, model_size=model,
                                  progress_cb=self._progress,
                                  cancel_check=self._check_cancel,
                                  log_cb=self._log)
            self._log(f"Transcription: {len(segments)} segments")

        # ── Stage 4: Speaker diarization ──────────────────────────────────
        self._stage(4, total)
        hf_token = self.config.get("hf_token", "").strip() or None
        if pre_segments is not None:
            self._log("Skipping speaker diarization (transcript already has speaker labels)")
            self._progress(100)
        elif hf_token:
            self._log("Identifying speakers with pyannote…")
            segments = diarize(audio_path, segments, hf_token,
                               progress_cb=self._progress, log_cb=self._log)
        else:
            self._log("No HuggingFace token — skipping speaker diarization")
            segments = diarize(audio_path, segments, None,
                               progress_cb=self._progress, log_cb=self._log)

        self._save_debug_transcript(segments, title)

        # ── Stage 5: Frame extraction ─────────────────────────────────────
        self._stage(5, total)
        self._log("Extracting and scoring candidate screenshots…")
        frames = extract_frames(
            video_path, temp_dir,
            progress_cb=self._progress,
            segments=segments,
            cancel_check=self._check_cancel,
        )
        cropped_count = sum(1 for f in frames if f.get("cropped"))
        self._log(
            f"Selected {len(frames)} candidate screenshots "
            f"({cropped_count} cropped to detected presentation screen)"
        )
        self._save_debug_frames(frames)

        # ── Stage 6: AI note generation ───────────────────────────────────
        self._stage(6, total)
        self._log("Sending to Claude for structured note generation…")
        notes = generate_notes(
            segments, frames, title,
            api_key=self.config["anthropic_key"],
            progress_cb=self._progress,
            description=description,
            yt_chapters=yt_chapters or None,
            attendees=attendees or None,
            ai_notes=ai_notes,
            cancel_check=self._check_cancel,
            log_cb=self._log,
        )
        chapters = notes.get("chapters", [])
        cited = sum(
            1 for ch in chapters for kp in ch.get("key_points", [])
            if kp.get("screenshot_idx") is not None
        )
        self._log(f"Generated {len(chapters)} chapters ({cited} screenshots referenced in notes)")

        # ── Stage 7: Write DOCX ───────────────────────────────────────────
        self._stage(7, total)
        self._log("Writing document…")
        output_dir = self.config.get("output_dir", os.path.expanduser("~/Desktop"))
        out_path = write_docx(notes, frames, output_dir, safe_title, log_cb=self._log)
        self._progress(100)
        self._log(f"Saved: {out_path}")

        self.completed.emit(out_path)

    def _check_cancel(self):
        if self._cancelled:
            raise PipelineCancelled()

    def _stage(self, n: int, total: int):
        self._check_cancel()
        label = self.STAGES[n - 1]
        self.stage_changed.emit(label, n, total)
        self._log(f"[{n}/{total}] {label}")

    def _progress(self, pct: int):
        self.stage_progress.emit(max(0, min(100, pct)))

    def _log(self, msg: str):
        self.log_message.emit(msg)
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            with open(os.path.join(DEBUG_DIR, "autonotes.log"), "a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
        except OSError:
            pass

    def _save_debug_transcript(self, segments: list[dict], title: str):
        """Persist the transcript so a later-stage failure doesn't cost the
        (potentially hours-long) transcription again on a retry."""
        try:
            os.makedirs(DEBUG_DIR, exist_ok=True)
            path = os.path.join(DEBUG_DIR, "last_transcript.json")
            data = {
                "video": title,
                "saved": datetime.now().isoformat(timespec="seconds"),
                "segments": segments,
            }
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
            self._log(f"Transcript saved to {path}")
        except (OSError, TypeError):
            pass

    def _save_debug_frames(self, frames: list[dict]):
        """Copy selected screenshots to the debug dir (replacing the last run's)."""
        try:
            dest = os.path.join(DEBUG_DIR, "last_run_frames")
            shutil.rmtree(dest, ignore_errors=True)
            os.makedirs(dest, exist_ok=True)
            for i, frame in enumerate(frames):
                ts = int(frame["timestamp"])
                crop = "_cropped" if frame.get("cropped") else ""
                name = f"{i + 1:02d}_t{ts // 60:02d}m{ts % 60:02d}s{crop}.jpg"
                shutil.copy(frame["path"], os.path.join(dest, name))
        except OSError:
            pass


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
    segments = data.get("segments")
    if not isinstance(segments, list):
        return [], {}
    return segments, {k: v for k, v in data.items() if k != "segments"}

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the app

```bash
cd /Users/mvb/Code/AutoNotes
python3 main.py
```

PyQt6 is installed globally via `pip3`. All other dependencies are in `requirements.txt` and should be installed with `pip3 install -r requirements.txt`.

## Architecture

AutoNotes is a PyQt6 desktop app that converts a YouTube URL or local video file into a structured DOCX with AI-generated notes and screenshots.

### Pipeline (7 stages)

`pipeline/worker.py` — `ProcessingWorker(QThread)` orchestrates everything. It runs off the main thread and emits signals (`stage_changed`, `stage_progress`, `log_message`, `completed`, `error`) that the UI connects to. The pipeline runs in a `tempfile.mkdtemp` directory that is cleaned up on exit.

Stages in order:
1. **Download** (`pipeline/downloader.py`) — wraps `yt-dlp` for YouTube URLs; local files are used directly. Teams/SharePoint downloads (`pipeline/teams_downloader.py`) use browser cookies (`--cookies-from-browser chrome`, then `edge`) to authenticate. `ffmpeg` is assumed at `/opt/homebrew/bin`.
2. **Audio extraction** (`pipeline/transcriber.py`) — `ffmpeg` extracts a 16 kHz mono WAV.
3. **Transcription** (`pipeline/transcriber.py`) — `faster-whisper` runs on CPU with `int8` compute; returns `[{start, end, text}]`.
4. **Diarization** (`pipeline/diarizer.py`) — optional pyannote speaker ID keyed on HuggingFace token. Silently falls back to `"Speaker"` if token is absent or pyannote fails. Adds `speaker` field to each segment.
5. **Frame extraction** (`pipeline/frame_extractor.py`) — ffmpeg extracts one frame every 5 s in a single decode pass (1280 px wide), OpenCV detects a presentation-screen quadrilateral in each frame and crops to it when found (with a score bonus, since a shown screen is inherently relevant); near-upright quads (`_MAX_AXIS_TILT_DEG`) get a pixel-exact bounding-box crop — only genuinely skewed (filmed) screens are perspective-warped, so screencast content is never rotated by imprecise corner detection, near-duplicate frames are collapsed via a 256×144 grayscale diff sensitive enough to catch a single bullet appearing (one best frame per slide/scene), each survivor is scored for screen-content likeness — slides, demos, terminals, browsers, dashboards — via edge density + gradient rectilinearity + color simplicity + uniformity, plus a bonus near transcript visual-cue phrases ("as you can see", "let me show", …). The dedup signature is computed on the central content region only (`_SIG_REGION`), so Teams webcam galleries/captions at the frame edges don't defeat dedup, and the top 40 are picked greedily by score with a ≥25 s minimum gap (`MIN_GAP_SECONDS`) so one busy stretch can't crowd out the rest of the video; result sorted by timestamp. Debug copies of each run's selections land in `~/Library/Logs/AutoNotes/`.
6. **Note generation** (`pipeline/note_generator.py`) — sends transcript text + up to 30 JPEG screenshots (base64) to `claude-sonnet-5` (override with the `AUTONOTES_MODEL` env var) and asks for structured JSON (title, chapters, key points, screenshot references). Chunked (two-call) mode triggers on transcript size only (`_TOKEN_THRESHOLD`, transcript tokens — images excluded so chunking stays rare). `screenshot_idx` values are coerced to int and validated against the provided frame set (invalid ones dropped with a log line), raw model responses are saved to `~/Library/Logs/AutoNotes/last_claude_*.json`, and a single-chapter fallback is used if JSON parsing fails. Claude also returns a per-screenshot `content_box` (fractional crop enclosing only the shared display content — excluding webcam tiles, browser chrome, docks, letterbox bars) which is clamped/validated (`_MIN_BOX_AREA`) into `notes["screenshot_boxes"]`; the docx writer crops each embed to its box at full resolution, falling back to the uncropped frame on any invalid box, and Claude is instructed not to reference webcam-gallery-only frames at all.
7. **DOCX output** (`output/docx_writer.py`) — `python-docx` builds the document: title, a source block (recording type, hyperlinked URL/path, and the recording summary — Teams AI recap preferred over the video/meeting description), chapter headings, speaker bylines, bullet points with inline `**bold**` formatting, and embedded screenshots at 5.5 in width. Saved to the configured output directory (default: `~/Desktop`).

### UI (`ui/main_window.py`)

Split-panel layout: left = inputs (URL/file, API key, HuggingFace token, Whisper model size, output dir) + action bar; right = stage indicator list, overall progress bar, log pane, and an "Open Document" button that appears on completion.

After each transcription the segments are saved to `~/Library/Logs/AutoNotes/last_transcript.json` (with video title + timestamp). A "Reuse last transcript" checkbox (enabled only when that file exists) skips audio extraction, Whisper, and diarization on the next run; the worker warns in the log if the saved transcript came from a different video. A transcript bundled with the download (Teams VTT / Graph) still takes priority over the saved one.

Settings (whisper_model, output_dir, MS client ID/join URL) are persisted across launches via `QSettings("AutoNotes", "AutoNotes")`. Secrets (api_key, hf_token) are stored in the macOS Keychain via `keyring` (`ui/secure_store.py`), falling back to QSettings if keyring is unavailable; old plaintext values are migrated on first load.

Cancellation is cooperative: the worker raises `PipelineCancelled` (`pipeline/_util.py`) at check points threaded through each stage via `cancel_check` callbacks, and subprocess loops kill their child process on the way out. The UI never calls `QThread.terminate()`.

## Key constants / hardcoded paths

| Symbol | Location | Value |
|---|---|---|
| `FFMPEG` / `FFPROBE` | `transcriber.py`, `frame_extractor.py` | `/opt/homebrew/bin/ffmpeg` |
| `YTDLP_CMD` | `_paths.ytdlp_command()` | dev: system `yt-dlp`; frozen app: `[sys.executable, "--yt-dlp"]` re-exec running the bundled `yt_dlp` package (dispatch at the top of `main.py`) |
| `MODEL` | `note_generator.py` | `"claude-sonnet-5"` (env override: `AUTONOTES_MODEL`) |
| `MAX_FRAMES` / `MIN_FRAMES` | `frame_extractor.py` | `60` / `8` — budget is `duration / TARGET_SECONDS_PER_FRAME` (30 s) clamped between them, so short videos get 1 frame/30s and long ones degrade toward 1/min+ |
| `MAX_SCREENSHOTS` | `note_generator.py` | `60` (sent to Claude; chunked mode splits 30 per call) |
| `INTERVAL_SECONDS` | `frame_extractor.py` | `5` (frame sampling interval) |
| `MIN_GAP_SECONDS` | `frame_extractor.py` | `25` (min spacing between selected frames) |
| `EXTRACT_WIDTH` / `API_MAX_WIDTH` | `frame_extractor.py` | `1920` / `1000` (extraction cap, never upscales; full res kept for DOCX / downscaled copy sent to Claude) |

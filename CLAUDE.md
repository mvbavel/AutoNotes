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
1. **Download** (`pipeline/downloader.py`) — wraps `yt-dlp` for YouTube URLs; local files are used directly. `yt-dlp` is invoked as a subprocess using Chrome cookies (`--cookies-from-browser chrome`) to bypass age/auth gates. `ffmpeg` is assumed at `/opt/homebrew/bin`.
2. **Audio extraction** (`pipeline/transcriber.py`) — `ffmpeg` extracts a 16 kHz mono WAV.
3. **Transcription** (`pipeline/transcriber.py`) — `faster-whisper` runs on CPU with `int8` compute; returns `[{start, end, text}]`.
4. **Diarization** (`pipeline/diarizer.py`) — optional pyannote speaker ID keyed on HuggingFace token. Silently falls back to `"Speaker"` if token is absent or pyannote fails. Adds `speaker` field to each segment.
5. **Frame extraction** (`pipeline/frame_extractor.py`) — ffmpeg extracts one frame every 10 s, OpenCV scores each for "slide-likeness" (edge density + color simplicity + background uniformity), and the top 25 are kept sorted by timestamp.
6. **Note generation** (`pipeline/note_generator.py`) — sends transcript text + up to 20 JPEG screenshots (base64) to `claude-sonnet-4-6` and asks for structured JSON (title, chapters, key points, screenshot references). Falls back to a single-chapter dump if JSON parsing fails.
7. **DOCX output** (`output/docx_writer.py`) — `python-docx` builds the document: title, chapter headings, speaker bylines, bullet points with inline `**bold**` formatting, and embedded screenshots at 5.5 in width. Saved to the configured output directory (default: `~/Desktop`).

### UI (`ui/main_window.py`)

Split-panel layout: left = inputs (URL/file, API key, HuggingFace token, Whisper model size, output dir) + action bar; right = stage indicator list, overall progress bar, log pane, and an "Open Document" button that appears on completion.

Settings (api_key, hf_token, whisper_model, output_dir) are persisted across launches via `QSettings("AutoNotes", "AutoNotes")`.

## Key constants / hardcoded paths

| Symbol | Location | Value |
|---|---|---|
| `FFMPEG` / `FFPROBE` | `transcriber.py`, `frame_extractor.py` | `/opt/homebrew/bin/ffmpeg` |
| `YTDLP` | `downloader.py` | resolved at import time from a priority list |
| `MODEL` | `note_generator.py` | `"claude-sonnet-4-6"` |
| `MAX_FRAMES` | `frame_extractor.py` | `25` (scored candidates kept) |
| `MAX_SCREENSHOTS` | `note_generator.py` | `20` (sent to Claude) |
| `INTERVAL_SECONDS` | `frame_extractor.py` | `10` (frame sampling interval) |

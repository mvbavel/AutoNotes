---
name: run-autonotes
description: Build, launch, screenshot and smoke-test the AutoNotes PyQt6 desktop app. Use when asked to run, start, launch, screenshot, or verify AutoNotes, or to confirm a pipeline/UI change works in the real app rather than only in tests.
---

# Run AutoNotes

AutoNotes is a **PyQt6 desktop app** (macOS) that turns a YouTube/Teams/SharePoint
recording into a DOCX of AI notes plus screenshots. All paths below are relative
to the repo root; run every command from there.

Drive it with **`.claude/skills/run-autonotes/driver.py`**. It renders the real
`MainWindow` under Qt's `offscreen` platform, so it screenshots and inspects
widgets with no display and **no macOS Screen Recording grant** — plain
`screencapture` fails here (see Gotchas). It also runs the heavy pipeline stages
offline.

**The driver never calls Claude.** Note generation is the only paid stage and no
subcommand invokes it, so iterate freely.

## Prerequisites

`ffmpeg`/`ffprobe` must be at `/opt/homebrew/bin` (hardcoded in
`pipeline/_paths.py`), and the app shells out to the **system** `yt-dlp`:

Install only if missing — a bare `brew install` on an already-present formula
silently *upgrades* it, which is not yours to do:

```bash
brew list ffmpeg >/dev/null 2>&1 || brew install ffmpeg
brew list yt-dlp >/dev/null 2>&1 || brew install yt-dlp
```

`yt-dlp` does need to stay current: `requirements.txt` pins `>=2026.7.4` because
extractors rot, and Homebrew's copy drifts below that floor and silently breaks
the SharePoint extractor. Check before upgrading:

```bash
/opt/homebrew/bin/yt-dlp --version   # must be >= 2026.07.04
# if older:  brew upgrade yt-dlp
```

## Setup

A venv is **mandatory**, despite `CLAUDE.md` and `build.sh` saying deps are
installed globally — Homebrew Python refuses (PEP 668) and has no PyQt6:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Activate the venv for every command below. First install pulls torch and takes
a few minutes.

## Run (agent path)

```bash
source .venv/bin/activate
```

**Screenshot the UI + dump widget state** (JSON to stdout):

```bash
python3 .claude/skills/run-autonotes/driver.py gui --screenshot /tmp/gui.png
```

Reach the enabled "Ready" state — the button needs a URL *and* a key, and the
driver blanks real secrets by default:

```bash
python3 .claude/skills/run-autonotes/driver.py gui \
  --screenshot /tmp/ready.png \
  --url "https://www.youtube.com/watch?v=dQw4w9WgXcQ" \
  --api-key dummy
```

**Always open the PNG and look at it.** The driver fails on a <5 KB grab, but
only your eyes catch a rendered-but-wrong window. Other flags: `--size WxH`,
`--keep-secrets` (leaks real Keychain values into the image — avoid).

**Offline pipeline smoke** — synthesises an 8-slide video, runs the real
`extract_frames` (ffmpeg decode, OpenCV screen-detect, dedup, slide scoring)
and `write_docx`, then asserts frames were selected and images embedded. ~8 s,
no network, no API spend:

```bash
python3 .claude/skills/run-autonotes/driver.py pipeline --out /tmp/smoke
```

Expect `PASS  8 frames -> …/driver_smoke_notes.docx`. **Fewer than 8 frames means
dedup is over-collapsing** — that is the signal this smoke test exists to catch.

**Check a Teams/SharePoint URL resolves** (network, no API spend):

```bash
python3 .claude/skills/run-autonotes/driver.py probe "<sharepoint-stream-url>"
```

**Tests** (89 of them; ~1 s warm, ~19 s on the first run while torch imports):

```bash
python3 .claude/skills/run-autonotes/driver.py tests
# same as: python3 -m unittest discover tests -v
```

## Direct invocation

Most changes here touch `pipeline/` internals, not the UI. Import and call
directly — no Qt, no GUI:

```bash
source .venv/bin/activate && python3 -c "
from pipeline.teams_downloader import _fetch_info, _BROWSERS
print(_BROWSERS)
print(_fetch_info('<url>', log_cb=print))
"
```

## Run (human path)

Opens a real window; needs a logged-in GUI session and blocks until closed.
Useless headless — use the driver instead.

```bash
source .venv/bin/activate && python3 main.py
```

## Gotchas

- **`screencapture -x` fails**: `could not create image from display`. The
  terminal lacks Screen Recording permission. Don't chase the grant — the
  driver's in-process `QWidget.grab()` under `QT_QPA_PLATFORM=offscreen` needs
  no permission at all.
- **`ps` lies about the interpreter.** Running from `.venv` still shows
  `/opt/homebrew/.../Python.app/.../Python` because macOS venvs exec the base
  framework stub for GUI support. Trust `sys.prefix`, not `ps`.
- **Screenshots leak secrets by default.** `MainWindow._load_settings()` pulls
  the real API key from the login Keychain and the real Teams Join URL (with its
  meeting token) from QSettings. The driver blanks all four secret fields before
  grabbing; `--keep-secrets` disables that.
- **`process_btn` stays disabled** with no API key (`_on_input_changed` requires
  input *and* key). Blanking secrets therefore disables it — pass `--api-key
  dummy`.
- **Dev mode uses system `yt-dlp`, not the venv's.** `_paths.ytdlp_command()`
  resolves it from `/opt/homebrew/bin`, so `pip install -U yt-dlp` inside the
  venv changes nothing. Use `brew upgrade yt-dlp`.
- **SharePoint auth is Edge-only in practice.** `FedAuth`/`rtFa` are *persistent*
  cookies in Edge but *session* cookies in Chrome, and
  `--cookies-from-browser` reads the on-disk DB — so Chrome's copy is stale and
  always bounces to `login.microsoftonline.com`. `_BROWSERS` is `["edge",
  "chrome"]` for this reason. If `probe` fails, open the recording in Edge and
  let it play, then retry: SharePoint mints the session cookie only once you've
  opened that specific recording.
- **Editing the driver's synthetic slides:** they need a real TrueType font at
  real size (Arial at 96/60/40 pt). PIL's default bitmap font is ~11 px, which
  vanishes at the 256×144 dedup signature resolution — all 8 slides then look
  identical and collapse to 2. `--seconds-per-slide` must also stay above
  `MIN_GAP_SECONDS` (25).
- `qt.qpa.fonts: Populating font family aliases took 52 ms` on every offscreen
  launch is harmless noise.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `error: externally-managed-environment` from `pip3 install` | Use the venv (see Setup). Don't reach for `--break-system-packages`. |
| `ModuleNotFoundError: No module named 'PyQt6'` | The venv isn't active, or you're on bare `/opt/homebrew/bin/python3`, which has no PyQt6. |
| `could not create image from display` | Screen Recording permission. Use `driver.py gui --screenshot`. |
| `Session cookies are required for this URL … --cookies-from-browser will not work` | Open the recording in Edge and let it play, then retry. Also check `brew upgrade yt-dlp`. |
| `AttributeError: 'MainWindow' object has no attribute …` from the driver | UI attribute renamed; the driver reads `stage_labels`, `url_edit`, `process_btn`, `api_key_edit`, `model_combo`, `output_dir_edit`, `reuse_transcript_check`, `reuse_info_label`. |
| `pipeline` reports fewer than 8 frames | Real signal, not driver flake — dedup/scoring in `frame_extractor.py` is over-collapsing. |

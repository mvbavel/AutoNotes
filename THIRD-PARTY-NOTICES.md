# Third-Party Notices

AutoNotes bundles and builds upon the open-source components listed below.
Each remains the property of its respective copyright holders and is used
under the terms of its own license.

Versions reflect those bundled in the current release. This file is a
good-faith summary; the authoritative license text for each component ships
inside its own distribution (and inside the built application bundle).

## Copyleft components — these determine AutoNotes' own license

| Component | Version | License | Project |
|---|---|---|---|
| PyQt6 | 6.11.0 | GPL-3.0-only (or Riverbank commercial) | https://www.riverbankcomputing.com/software/pyqt/ |
| FFmpeg / FFprobe | 8.1.1 | GPL-3.0-or-later (built `--enable-gpl --enable-version3`) | https://ffmpeg.org/ |

Because AutoNotes is distributed as a single application bundle that
includes GPL-licensed PyQt6 and a GPL build of FFmpeg, the combined work is
distributed under the **GNU General Public License v3.0** — see `LICENSE`.

An LGPL build of FFmpeg (without `--enable-gpl`) and a commercial Qt/PyQt
license would permit distribution under other terms; that is not the
configuration shipped here.

## Weak-copyleft components

| Component | Version | License | Project |
|---|---|---|---|
| certifi | 2026.5.20 | MPL-2.0 | https://github.com/certifi/python-certifi |

certifi's Mozilla CA bundle is redistributed unmodified. MPL-2.0 is
file-level copyleft; source for the unmodified files is available from the
project above.

## Permissive components

| Component | Version | License | Purpose in AutoNotes |
|---|---|---|---|
| yt-dlp | 2026.7.4 | Unlicense (public domain) | Video/metadata/subtitle download (YouTube, Teams, SharePoint) |
| curl-cffi | 0.15.0 | MIT | TLS impersonation enabling high-resolution formats |
| faster-whisper | 1.2.1 | MIT | Speech-to-text transcription |
| CTranslate2 | 4.7.2 | MIT | Inference engine for faster-whisper |
| pyannote.audio | 4.0.4 | MIT (© 2020 CNRS) | Optional speaker diarization |
| PyTorch | 2.12.0 | BSD-3-Clause | Deep-learning runtime for pyannote |
| OpenCV (opencv-python-headless) | 4.13.0.92 | Apache-2.0 | Frame analysis, screen detection, cropping |
| NumPy | 2.4.6 | BSD-3-Clause (and others) | Array math for frame analysis |
| Pillow | 12.2.0 | MIT-CMU | Image encoding and cropping |
| python-docx | 1.2.0 | MIT | Word document generation |
| anthropic | 0.104.1 | MIT | Claude API client for note generation |
| MSAL for Python | 1.37.0 | MIT | Microsoft identity auth (Graph API) |
| requests | 2.34.2 | Apache-2.0 | HTTP client for Graph API |
| keyring | 25.7.0 | MIT | macOS Keychain storage for secrets |

## Build-time only (not distributed in the application)

| Component | Version | License | Purpose |
|---|---|---|---|
| PyInstaller | 6.20.0 | GPL-2.0-or-later with linking exception | Freezes the app into a macOS bundle |

PyInstaller's linking exception explicitly permits distributing applications
built with it under the application's own license terms.

## Models and services

Machine-learning models are downloaded at runtime and are **not** bundled
with AutoNotes; each carries its own license and terms:

- **Whisper** models (via faster-whisper / Systran conversions) — MIT.
- **pyannote/speaker-diarization-3.1** and its dependencies — gated models
  on Hugging Face requiring acceptance of their user conditions and a
  Hugging Face access token.
- **Claude** (Anthropic API) — a commercial service requiring your own API
  key; subject to Anthropic's terms, not an open-source component.

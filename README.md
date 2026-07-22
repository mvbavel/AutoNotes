Convert a mpeg file or youtube video URL into a word document with summarised notes screen-shots associated to the notes, and organised into chapters as a summary of the video

## Installation

Download `AutoNotes.dmg` from the
[latest release](https://github.com/mvbavel/AutoNotes/releases/latest), open
it, and drag AutoNotes onto the Applications shortcut.

Because the app is ad-hoc signed rather than notarised, the first launch must
be **right-click (or Control-click) → Open → Open**. Subsequent launches work
normally.

> Always install by dragging from the DMG. Copying the app with `cp -r`, or
> through tools that don't preserve symbolic links, breaks the bundled Qt
> framework layout and the app will crash at launch.

Everything AutoNotes needs — FFmpeg, yt-dlp, Python — is bundled. Nothing has
to be installed separately.

## Quick start

1. **Launch AutoNotes.**
2. **Choose a source** — paste a YouTube, Teams or SharePoint URL into the
   *Video Input* box, or click **Browse…** to pick a local file
   (`.mp4`, `.mpeg`, `.mpg`, `.mov`, `.avi`, `.mkv`).
3. **Paste your Claude API key** (see below). This is the only mandatory
   credential.
4. *(Optional)* add a Hugging Face token for speaker names, pick a Whisper
   model size, and set an output folder.
5. **Click Generate Notes** and watch the log pane on the right.
6. When it finishes, click **Open Document**.

The result is saved as `<video title>_notes.docx` in your output folder
(**`~/Desktop`** by default) and contains: the recording source and summary,
AI-generated chapters, bullet-point notes with key terms in bold, speaker
attribution, and screenshots cropped to the shared screen content.

Settings persist between launches, so steps 3–4 are usually one-time.

### What happens during a run

The seven stages shown in the progress panel are:

1. **Download / load video** — fetches the URL or opens the local file.
2. **Extract audio** — 16 kHz mono WAV via FFmpeg (skipped if a transcript
   already exists).
3. **Transcribe speech** — Whisper, running locally on your CPU.
4. **Identify speakers** — optional, needs a Hugging Face token.
5. **Extract screenshots** — samples frames, detects shared screens, discards
   duplicates and scores what's worth keeping.
6. **Generate AI notes** — sends the transcript and screenshots to Claude.
7. **Write document** — builds the `.docx`.

Transcription (stage 3) dominates the runtime — roughly as long as the
recording itself with the `medium` model, longer with `large-v3`. The log
shows an estimate up front and progress with a live ETA every 10%. YouTube
videos with subtitles and Teams recordings with transcripts skip stages 2–4
entirely and finish in minutes.

## Credentials

### Claude API key — required

Used to turn the transcript and screenshots into structured notes.

1. Sign in at [console.anthropic.com](https://console.anthropic.com).
2. Add billing credit — this is a paid API, separate from any Claude.ai
   subscription.
3. Create a key under **API Keys** and copy it (it starts with `sk-ant-`).
4. Paste it into **Claude API Key** in AutoNotes.

Cost depends on recording length and screenshot count, but is typically a
few tens of US cents per hour of video. Usage is visible in the Console.

### Hugging Face token — optional

Enables **speaker diarization**: labelling who spoke when, so the notes say
"Speaker A", "Speaker B" rather than a single generic "Speaker". Without it
everything still works — you just lose per-speaker attribution.

It is only used for locally transcribed audio. Teams recordings and
downloaded transcripts already carry real speaker names, so the token is
never needed for those.

A **read-only** token is sufficient; AutoNotes only downloads models and
never uploads anything.

1. Create an account at [huggingface.co](https://huggingface.co).
2. Under **Settings → Access Tokens**, create a token with the **Read** role.
   (For a fine-grained token, grant *Read access to contents of all public
   gated repos you can access*.)
3. Signed in with that same account, open
   [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
   and [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
   and **accept the user conditions** on each. These are free but gated
   models — the token alone is not enough.
4. Paste the token (it starts with `hf_`) into **HuggingFace Token**.

If the token is valid but the conditions haven't been accepted, the run
continues without speaker labels and the log explains why.

### How credentials are stored

Both are held in the **macOS Keychain** (service name `AutoNotes`), not in a
plain-text file. Any older plain-text values are migrated automatically on
first launch.

## Whisper model sizes

`tiny` · `base` · `small` · `medium` (default) · `large-v3`

Larger models are more accurate and considerably slower on CPU — `large-v3`
takes roughly 2–3× as long as `medium`. The first use of any size downloads
it (a one-time download of up to ~1.5 GB, cached in
`~/.cache/huggingface/hub/`); the log says so before it starts.

## Reuse last transcript

Transcription is by far the slowest stage, so after every successful
transcription AutoNotes saves the result to
`~/Library/Logs/AutoNotes/last_transcript.json`.

Tick **Reuse last transcript (skip transcription)** to load that saved
transcript instead of transcribing again. Stages 2–4 are skipped and the run
completes in minutes rather than hours.

This is useful when you want to re-generate a document without paying the
transcription cost again — for example after changing the output folder, or
to pick up an improved version of the app.

- The checkbox is **only enabled when a saved transcript exists**, and the
  line beneath it shows which recording it came from, how many segments it
  has, and when it was saved.
- Only the **most recent** transcript is kept; each successful run overwrites
  it.
- If the saved transcript came from a *different* recording than the one you
  are processing, the run continues but the log prints a clear warning — the
  notes would otherwise describe the wrong video.
- The box is unticked at every launch, so a stale transcript is never used by
  accident.
- A transcript that ships with the recording (a Teams or YouTube transcript)
  always takes priority over the saved one.

## Where files are stored

| Location | Contents |
|---|---|
| `~/Desktop` (configurable) | The generated `<title>_notes.docx` |
| `~/Library/Logs/AutoNotes/autonotes.log` | Running log of every run, including full error tracebacks. **Start here when something fails.** |
| `~/Library/Logs/AutoNotes/last_transcript.json` | Saved transcript used by *Reuse last transcript* |
| `~/Library/Logs/AutoNotes/last_run_frames/` | Copies of the screenshots selected in the last run, named by timestamp — handy for checking what the app chose |
| `~/Library/Logs/AutoNotes/last_claude_*.json` | Raw responses from Claude for the last run, for diagnosing note or screenshot problems |
| `~/.cache/huggingface/hub/` | Downloaded Whisper and diarization models (largest disk user; safe to delete, models re-download on demand) |
| macOS Keychain (service `AutoNotes`) | Claude API key and Hugging Face token |
| `~/Library/Preferences/com.autonotes.AutoNotes.plist` | Non-secret settings: model size, output folder, Teams client ID and join URL |
| `~/.autonotes_graph_tokens.json` | Microsoft Graph sign-in token cache, owner-readable only (only if Teams Graph integration is used) |

**Temporary working files** — the downloaded video, extracted audio, and all
sampled frames — go to a private folder under the system temporary directory
(`/var/folders/…/autonotes_*`) and are **deleted automatically when the run
ends**, including after an error or cancellation. These can be many gigabytes
while a run is in progress, so keep some free disk space available.

Nothing in `~/Library/Logs/AutoNotes/` is required for the app to work; you
can delete the whole folder at any time. Note that doing so removes the saved
transcript, disabling *Reuse last transcript* until the next run.

## Using a Microsoft Teams recording

There are two ways to process a Teams meeting recording. **Option A needs no
administrator involvement** and is what most people should use. Option B is
optional and adds richer metadata, but requires an Azure app registration and
(in most tenants) admin consent.

### Option A — paste the recording URL (no admin steps)

1. Sign in to Teams or SharePoint in **Google Chrome or Microsoft Edge**, and
   confirm you can play the recording in that browser.
2. Open the recording and copy its URL from the address bar. Any of these
   hosts are recognised automatically:
   `teams.microsoft.com`, `*.sharepoint.com`, `stream.microsoft.com`,
   `microsoftstream.com`.
3. Paste it into the **Video Input** field in AutoNotes and click
   **Generate Notes**.

AutoNotes reads your existing browser session cookies (Chrome first, then
Edge) to download the recording — it never asks for your password and stores
no Microsoft credentials. macOS may prompt for permission to read the
browser's cookie store the first time.

If the recording has a Teams transcript attached, AutoNotes uses it directly
(including real speaker names) and skips local transcription entirely, which
makes the whole run dramatically faster.

**Requirements:** you must have permission to view the recording, and be
signed in with that account in Chrome or Edge. If the download fails, the
usual causes are being signed into the wrong profile, the link being
sign-in-restricted, or the recording having been moved or deleted.

### Option B — connect Microsoft Graph (optional, needs admin consent)

Adding a Graph connection lets AutoNotes also pull:

- the attendee list,
- the Teams **AI meeting recap**, used as the recording summary in the
  document, and
- the official meeting transcript (preferred over the downloaded subtitles).

Everything else works without this. Skip it if you only need notes and
screenshots.

#### Administrator steps (once per organisation)

These are performed in the **Microsoft Entra admin center** (formerly Azure
Active Directory) by someone with the Application Administrator or Global
Administrator role.

1. **Register an application** — *Entra admin center → App registrations →
   New registration*.
   - Name: e.g. `AutoNotes`
   - Supported account types: *Accounts in this organizational directory only*
     is sufficient.
   - Redirect URI: select the **Mobile and desktop applications** platform and
     add `http://localhost`. AutoNotes signs in through the system browser
     using MSAL, which needs the loopback redirect.
2. **Enable public client flows** — *Authentication → Advanced settings →
   Allow public client flows → **Yes***. AutoNotes is a desktop app and holds
   no client secret; sign-in fails without this.
3. **Add delegated API permissions** — *API permissions → Add a permission →
   Microsoft Graph → Delegated permissions*:

   | Permission | Purpose | Admin consent |
   |---|---|---|
   | `User.Read` | Sign-in and basic profile | Not required |
   | `Calendars.Read` | Find the meeting and its attendees | Not required |
   | `OnlineMeetings.Read` | Look up the meeting by join URL | **Required** |
   | `OnlineMeetingTranscript.Read.All` | Read the meeting transcript | **Required** |

4. **Grant admin consent** — click *Grant admin consent for &lt;tenant&gt;*.
   Without this, sign-in will succeed but transcript and meeting lookups
   return nothing.
5. **Share the Application (client) ID** with users — it is on the app's
   *Overview* page. It is not a secret.

> These permissions are **delegated**, meaning AutoNotes only ever sees data
> the signed-in user can already access — it cannot read meetings they are not
> entitled to. No application (app-only) permissions are used, so a Teams
> application access policy is not required.
>
> Consent requirements can differ between tenants and Microsoft changes them
> from time to time; treat the table above as a starting point and verify in
> your own tenant.

#### User steps (once per person)

1. In AutoNotes, paste the Application (client) ID into **MS Client ID**.
2. Paste the meeting's **Join URL** — the
   `https://teams.microsoft.com/l/meetup-join/…` link from the calendar
   invite — into **Join URL**. This is how the meeting is identified; without
   it, the Graph connection is skipped.
3. Run as normal. On first use a browser window opens for Microsoft sign-in.
   The resulting token is cached in `~/.autonotes_graph_tokens.json` (owner
   read/write only) and refreshed automatically thereafter.

If Graph lookup fails for any reason, AutoNotes logs why and falls back to
Option A behaviour rather than failing the run.

## License

AutoNotes is free software, licensed under the **GNU General Public License
v3.0** — see [LICENSE](LICENSE).

The application bundle includes GPL-licensed components (PyQt6 and a GPL
build of FFmpeg), which is why the combined work is distributed under the
GPL. Attribution and license details for every bundled open-source
component are in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

Note that AutoNotes also relies on services that are **not** open source and
require your own credentials: the Anthropic Claude API (note generation)
and, optionally, Microsoft Graph (Teams metadata) plus a Hugging Face token
for gated speaker-diarization models.

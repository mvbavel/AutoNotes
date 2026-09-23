---
name: check-ytdlp
description: Check whether the yt-dlp AutoNotes actually runs is current, and diagnose download failures caused by extractor rot. Use when a YouTube or SharePoint download fails with HTTP 403, "Requested format is not available", nsig/signature extraction errors or a "Sign in to confirm" prompt; before cutting a release; or when asked whether yt-dlp is stale, which yt-dlp is being used, or how to upgrade it.
---

# Checking AutoNotes' yt-dlp

Extractor rot is the single most common cause of a broken AutoNotes download.
YouTube changes, yt-dlp ships the fix days later, and anything older than that
fails — usually as a *transport* error that sends you debugging the network
instead of the version.

## Which yt-dlp actually runs

**Not `/opt/homebrew/bin/yt-dlp`.** That binary is not on the app's path at all.
`pipeline/_paths.ytdlp_command()` re-execs `main.py --yt-dlp` in **both** dev and
frozen mode, so what runs is always the importable `yt_dlp` *package* — never a
standalone binary. (It must: a bare binary can't inherit the TLS trust `main.py`
installs, which breaks every download on a TLS-inspecting network.)

Two installs therefore matter, for two different purposes:

| Install | Runs when | Upgrade with |
|---|---|---|
| `.venv/bin/python`'s `yt_dlp` | you run the app in dev | `source .venv/bin/activate && pip install -r requirements.txt` |
| `/opt/homebrew/bin/python3`'s `yt_dlp` | `build.sh` / `AutoNotes.spec` bundles it into the `.app` | `/opt/homebrew/bin/python3 -m pip install --break-system-packages --upgrade yt-dlp` |

`brew upgrade yt-dlp` fixes **neither**. The Homebrew formula's binary is not
what `collect_all('yt_dlp')` bundles and not what either interpreter imports.

## Checking

`pipeline/ytdlp_health.py` is the source of truth — don't reimplement its
version parsing or thresholds, call it:

```bash
.venv/bin/python -c "
from pipeline.ytdlp_health import installed_version, age_days, staleness_warning
print(f'{installed_version()} — {age_days()} days old')
print(staleness_warning() or 'within the freshness threshold')
"
```

Before a release, check the **build** interpreter too, since that's the copy
users get and cannot upgrade themselves:

```bash
/opt/homebrew/bin/python3 -c "from yt_dlp.version import __version__; print(__version__)"
```

## Reading the result

Age, not the `requirements.txt` floor, is the signal. A floor only records what
was known when it was written — on 2026-09-16 the floor was `>=2026.7.4` and the
installed `2026.07.04` satisfied it while every download 403'd.

`STALE_AFTER_DAYS = 45` (yt-dlp releases roughly weekly; the version that broke
was 74 days old). Past that, upgrade before doing anything else — don't start
diagnosing a failure on a stale version.

An unparseable version returns `None`, not a warning. That's deliberate: an
unrecognised version isn't evidence of staleness, and a permanent false alarm
would train the warning out of usefulness. Don't "fix" it to warn.

## Diagnosing a failure

Feed the captured output to `diagnose()` rather than pattern-matching by hand —
it already knows the four signatures that mean "aged out" and returns a remedy
tailored to frozen vs dev:

```bash
.venv/bin/python -c "
import sys
from pipeline.ytdlp_health import diagnose
print(diagnose(sys.stdin.read().splitlines()) or 'not a recognised staleness symptom')
" < /path/to/captured-output.txt
```

It returns `None` for anything unrecognised (a full disk, a private video) on
purpose — keep reporting the real error instead of blaming the version for
everything.

## Proving a fix

A metadata fetch is **not** sufficient. When `2026.07.04` broke, extraction
succeeded and the media CDN URLs 403'd — only pulling real bytes reproduces it.
Use the same dispatch and format string the app uses:

```bash
.venv/bin/python main.py --yt-dlp --no-playlist \
  --format 'bestvideo+bestaudio/best' --merge-output-format mp4/mkv \
  --ffmpeg-location /opt/homebrew/bin -o '/tmp/smoke.%(ext)s' \
  'https://www.youtube.com/watch?v=jNQXAC9IVRw'
```

A file under ~100 KB is a stub or an error page, not video. This mirrors
`.github/workflows/monthly-rebuild.yml`, which runs the same check on a schedule
so rot is caught before a user hits it — if that workflow is red, start here.

Unit tests for the logic: `python3 -m unittest tests.test_ytdlp_health tests.test_ytdlp_dispatch`

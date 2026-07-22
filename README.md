Convert a mpeg file or youtube video URL into a word document with summarised notes screen-shots associated to the notes, and organised into chapters as a summary of the video

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

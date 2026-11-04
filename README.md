# Showreel for Hermes Agent

**Turn any Hermes session into a demo video under 90 seconds.**

Showreel records what a session actually did: your ask, the terminal commands and their output,
file edits, screenshots and token usage. It cuts the key beats into an MP4 with burned-in captions,
plus an `.srt` file and a poster PNG. The video is rendered on your machine with Pillow and your
own `ffmpeg`. Secrets are scrubbed before anything is stored or drawn. Nothing is uploaded.

```
/showreel cut 45 9:16
```

```
Showreel cut: 45.0s 9:16 · 10 beats (title → ask → terminal → tool → terminal → diff → fix → result → stats → outro)
narration: captions only · 1 secret(s) redacted · rendered in 14.7s
video: ~/.hermes/plugin-data/showreel/renders/showreel-build-a-tiny-snake-game-…-9x16-….mp4
```

## Why a plugin and not a prompt

* **Live, structured capture.** Hooks (`pre_llm_call`, `post_tool_call`, `post_api_request`,
  `post_llm_call`, session start and end) record real tool I/O, exit codes, timings and diffs as
  they happen. A prompt can only remember what fits in context.
* **A deterministic media pipeline.** The same session and settings always give the same
  storyboard. The pipeline scores beats (a failing test followed by the fix counts more than
  `ls`), fits them to a hard time budget (never over 89 s), animates terminal typing and diffs,
  and encodes H.264.
* **Redaction you can audit.** Every string is scrubbed twice: once when it is recorded, and again
  right before it is drawn. The scrub covers API keys, tokens, JWTs, private keys, URL
  credentials, `key=value` secrets, high-entropy blobs, email addresses and your home path.

## Install

```
hermes plugins install showreel        # once it is in the catalog
# or from a checkout:
hermes plugins install https://github.com/Duffey234/hermes-showreel
hermes plugins enable showreel
```

You need `ffmpeg` on PATH (`apt install ffmpeg`, `brew install ffmpeg` or `winget install ffmpeg`).
Showreel does not bundle or download it. `hermes showreel status` tells you what it found.

## Use

| Where | Command |
|---|---|
| In a session | `/showreel cut [seconds] [16:9\|9:16\|1:1] [title…]` renders the current session |
| | `/showreel list`, `/showreel status`, `/showreel demo`, `/showreel purge [id]` |
| Ask the agent | "make a 30 second vertical demo video of what we just did" (it calls the `showreel_render` tool) |
| | `/showreel plan [seconds]` previews the beats and the exact narration text, renders nothing |
| Terminal | `hermes showreel list \| render [id] \| plan [id] \| demo \| status \| purge` |

Each render writes `<name>.mp4`, `<name>.srt`, `<name>.png` (poster) and `<name>.json` (the
storyboard). They go to `<HERMES_HOME>/plugin-data/showreel/renders/` unless you set
`output_dir`. On messaging platforms the reply carries a `MEDIA:` tag, so the video arrives as an
attachment.

Try it with no model at all: `hermes showreel demo --length 30 --aspect 9:16` renders a built-in
sample session.

## In the Desktop app

Showreel ships a Desktop half in the same package (`desktop/plugin.js`, with its backend in
`dashboard/plugin_api.py`). Like every Desktop plugin it starts off: turn it on in
**Capabilities → Plugins → Showreel**. Then you get:

* **A Showreel page** in the sidebar (and in ⌘K): your recorded sessions, and for the one you
  pick a **moment editor**. Every beat is listed with its time; untick a beat to drop it, or edit
  its caption, then choose the length, aspect and narration and press **Render**. The plan shows
  the estimated length and, when a voice is on, how many characters it would speak.
* **An inline player** for the new video and every earlier render.
* **A REC chip** in the status bar: a pulsing dot while Showreel is recording the current
  session, a plain "Showreel" when capture is on and idle. Click it to open the page.
* **A player card in chat**: when the agent calls `showreel_render`, Desktop shows the video
  with its length, narration and redaction count, plus an **Edit moments** button.

The CLI and the `/showreel` command remain the core and do everything the page does; the Desktop
half is a front end over the same code. Tested in the browser-hosted Desktop (`hermes webapp`) on
Hermes 0.22.7.

## Narration (optional, pluggable)

| `narration` | What happens | Cost |
|---|---|---|
| `off` (default) | Captions only | free |
| `auto` | A free offline voice if one is installed, otherwise captions only. Never a paid service | free |
| `local` | Free offline voice: `piper` (set `piper_model`) or `espeak-ng`/`espeak` on PATH | free |
| `elevenlabs` | Only when you choose it: your own key, one request per narrated beat to `api.elevenlabs.io` | your ElevenLabs plan |
| `hermes` | Hermes's own `text_to_speech` tool, with whatever TTS you configured | depends on your TTS |

Set the key through Hermes like any other provider key: use the masked field on the plugin's
Settings page in the Desktop, or `ELEVENLABS_API_KEY` in your profile's `.env`. Showreel reads it
with `agent.secret_scope.get_secret` at the moment of the request and only sends it to
`https://api.elevenlabs.io`. It never logs, prints or stores the key. If ElevenLabs fails,
Showreel falls back to the local voice, then to captions only. In cron and other unattended runs
ElevenLabs is skipped unless you turn on `paid_narration_unattended`.

**Planned before any audio exists.** Showreel estimates how long each line takes to say (words at
the voice's speaking rate, plus pauses) before it synthesizes anything. Lines that would overrun
their beat are cut to their first clause; beats with speech borrow time from beats that have
spare; and if the video would still run past the length you asked for, the least important middle
beats go silent (their caption stays). The title is folded into "You asked: …", and the ask and
the result are always spoken. Silent beats are never synthesized, so a paid voice is never billed
for audio that gets thrown away. To see exactly what would be sent, and how many characters,
without generating anything:

```
hermes showreel plan --length 45 --narration elevenlabs     # no audio, no video, no network
```

## Settings

Set these under `plugins.entries.showreel.settings`. Every one also appears on the Desktop
Settings page.

`capture` (true), `default_length` (45), `default_aspect` (16:9), `resolution` (1080p|720p),
`theme` (midnight|daylight),
`narration` (off), `elevenlabs_voice_id`, `elevenlabs_model`, `paid_narration_unattended`
(false), `local_voice` (en-us), `piper_model`, `redact_emails` (true), `keep_sessions` (30),
`output_dir`, `ffmpeg_path`.

## What it does on your machine (disclosure)

* **Reads:** hook payloads from Hermes (your prompts, tool arguments and results, the replies,
  usage counts). Image files that a tool result points at (screenshots, generated images) are
  read only when a frame is drawn.
* **Writes:** `<HERMES_HOME>/plugin-data/showreel/data.db` (the scrubbed timeline; the newest 30
  sessions are kept) and the render files listed above.
* **Runs:** `ffmpeg` (and `ffprobe` when present) to encode. With `narration: local` it also runs
  `espeak-ng`/`espeak` or `piper`. All of these are subprocesses with fixed argument lists and no
  shell.
* **Network:** none by default. Only when you explicitly set `narration: elevenlabs` (setting,
  `--narration elevenlabs`, or the tool's `narration` argument) does it make HTTPS POST requests to
  `api.elevenlabs.io`, carrying the caption text of each beat. Having the key set is not enough.
* **Desktop half:** when you enable it, its routes under `/api/plugins/showreel/` read your
  recordings and the renders folder, render on request, and return a render (by bare file name,
  renders folder only) as a data URL for the player. No other files are served.
* **No telemetry**, no background processes and no self-updates. Showreel never overrides a core
  tool and never bypasses approvals: it only observes hooks and does not block or alter tool
  calls.
* **Privacy controls:** `capture: false` stops recording. `/showreel purge` deletes recordings
  (videos are kept).

## Develop

```
python -m pytest -c tests/pytest.ini tests     # from a Hermes venv: unit, render and real-discovery tests
hermes plugins validate . --install-deps
hermes plugins doctor . --ci
```

Run pytest with the Python that has Hermes installed. Under any other Python, one test is skipped
on purpose: `tests/test_hermes_load.py::test_real_discovery_and_core_hooks` ("Hermes not
importable"). It loads the plugin through Hermes's real discovery in a scratch `HERMES_HOME` and
drives a core `read_file` call so `post_tool_call` fires, so it needs Hermes itself. Two more tests
skip only when no `espeak-ng`/`espeak` is on PATH (`tests/test_tts.py` local voice,
`tests/test_render.py` narrated render), and the render tests skip without `ffmpeg`.

MIT licensed.

# yumnb — Yum NoteBook

> Turn any URL, YouTube video, screenshot, or chunk of text into a tidy
> learning packet: **AI summary + dual-host talk-show MP3 + slide deck**,
> with optional webhook notification and direct IM delivery via OpenClaw / Hermes.

```
URL / YouTube / image / text
            │
            ▼
       ingest  ──►  source/   raw HTML / VTT transcript / image copy
            │
            ▼
        AI summary   summary.md
            │
            ├──► tts          talkshow.mp3   (edge-tts dual voice + jingle)
            ├──► ppt          deck.pptx      (bullets / tables / flow / images)
            ▼
        publish      links.json  + optional webhook
```

## Why

LLMs are great at digesting one thing at a time but never put the result
where you can find it again. yumnb produces a **filesystem-first** artifact
per source — Markdown summary, an MP3 you can listen to in the car, and a
PPTX you can drop into a meeting — all under one folder. It's the same
workflow whether your AI is a hosted API, a local model, or an interactive
agent CLI.

If you like the idea of NotebookLM but want a more local-first, file-based
workflow, yumnb is a good fit. It is not trying to copy NotebookLM exactly;
it is a polite alternative for people who prefer to keep their notebooks,
source material, and generated artifacts on their own machine by default.

## Features

- **YouTube ingest** with proper subtitle handling — tries each language
  one-by-one (zh-Hans → zh → en …), manual subs first, then auto-generated,
  then falls back to `youtube-transcript-api`; parses VTT properly (strips
  inline timing tags, dedupes repeated lines).
- **Web ingest** with stdlib-only HTML stripping (BeautifulSoup used if
  installed). Falls back gracefully on 403/JS pages — you can plug in your
  own browser-fetch script via the `--fetcher` flag.
- **Image / text ingest** for screenshots, notes, snippets.
- **Pluggable AI** — OpenAI / Azure OpenAI / Anthropic Claude / Google
  Gemini / Ollama / **any CLI agent** (Copilot CLI, Claude Code, Aider, …),
  or `none` to drive the steps yourself from your own agent.
- **Dual-host MP3** via Microsoft `edge-tts` (no API key) with a procedurally
  generated intro/outro chime — voices are configurable per persona.
- **Real PPT** via `python-pptx` — title, bullets, table, horizontal flow,
  image, two-column, summary. WEBP/AVIF/HEIC auto-converted via Pillow.
- **Local-first notebooks.** Sources, summaries, scripts, slides, and links
  live as normal files under your chosen output directory, so your notebooks
  stay on your machine unless you explicitly enable upload / notify / deliver.
- **No hardcoded paths, tenants, webhooks, or org info.** Configure
  everything via `config.yaml` or environment variables.

## Install

```bash
git clone https://github.com/<you>/yumnb
cd yumnb
./scripts/bootstrap.sh

# then
cp config.example.yaml config.yaml
$EDITOR config.yaml
```

Manual alternative:

```bash
python -m venv .venv && . .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Install whichever AI SDK matches your provider (only one is needed):
pip install openai            # for provider: openai
pip install anthropic         # for provider: anthropic
pip install google-generativeai   # for provider: gemini
pip install ollama            # for provider: ollama
# (provider: cli / none need no extra SDK)

cp config.example.yaml config.yaml
$EDITOR config.yaml
```

> **Windows note**: yumnb works on Windows too. Use forward slashes or
> backslashes — both fine. ffmpeg is bundled via `imageio-ffmpeg`, so you
> do not need a system install.

## Quick start

### Fully-automatic

```bash
python -m yumnb auto "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
# → notes/20260524-1530-never-gonna-give-you-up/
#     ├── source/   (transcript.vtt, thumb.jpg, raw.txt, …)
#     ├── summary.md
#     ├── talkshow.txt
#     ├── talkshow.mp3
#     ├── deck.json
#     ├── deck.pptx
#     └── links.json
```

### Agent-driven (bring your own LLM)

Set `ai.provider: none` in `config.yaml`. Then in your favorite agent CLI
(GitHub Copilot CLI, Claude Code, Aider, Cursor, …) run:

```bash
python -m yumnb ingest "<URL>"
# read source/raw.txt, write summary.md and deck.json yourself
python -m yumnb tts notes/<slug>/talkshow.txt --output notes/<slug>/talkshow.mp3
python -m yumnb ppt notes/<slug>/deck.json --output notes/<slug>/deck.pptx
python -m yumnb publish notes/<slug>
```

The `SKILL.md` in this repo is a drop-in skill descriptor for agent CLIs
that load skill folders (e.g., Copilot CLI's `~/.copilot/skills/`).

## yumnb vs NotebookLM

A short version:

- **NotebookLM** is great if you want a polished hosted notebook experience.
- **yumnb** is better if you want a **local-first, file-based workflow** you can script, inspect, version, and extend.

In practice, yumnb may be a good fit when you want to:
- keep source material, notebooks, summaries, slides, and audio as normal local files
- plug the workflow into your own agent / CLI / automation stack
- choose your own AI backend instead of being tied to one hosted product
- control when anything gets uploaded or delivered

So the positioning is not “NotebookLM, but better at everything.”
It is more like: **a quieter, more local, more hackable alternative for people who prefer owning the workflow**.

## Smoke tests

After bootstrap:

```bash
. .venv/bin/activate
pytest tests/test_smoke.py
```

These tests intentionally avoid external AI/network assumptions for the core smoke path.

## AI providers

| `provider` | Required pkg            | Required env / config        |
| ---------- | ----------------------- | ---------------------------- |
| `openai`   | `openai`                | `OPENAI_API_KEY` (or `ai.openai.api_key`) |
| `anthropic`| `anthropic`             | `ANTHROPIC_API_KEY`          |
| `gemini`   | `google-generativeai`   | `GEMINI_API_KEY`             |
| `ollama`   | `ollama`                | local Ollama on `ai.ollama.host` |
| `cli`      | none                    | `ai.cli.command` (e.g. `["claude", "-p"]`) |
| `none`     | none                    | you bring your own agent to write summary.md / deck.json |

**Azure OpenAI / LM Studio / vLLM / any OpenAI-compatible endpoint** —
use `provider: openai` and set `ai.openai.base_url`.

**CLI provider** — yumnb pipes the prompt to stdin of the configured
command and reads the answer from stdout. Useful when you have a local
agent that already knows how to call tools / browse / search.

If `auto` mode fails because the configured AI backend is not ready, yumnb now prints a human-readable fix list instead of a low-level stack trace.

## TTS voices

`edge-tts` exposes hundreds of Microsoft Online voices for free. List them:

```bash
edge-tts --list-voices
```

yumnb ships **default male + female voice pairs for English, Chinese,
Japanese, Spanish, French and German** under `tts.language_voices`. The
active language (`config.language` or `--language`) selects which pair
is used. Set `tts.voices` to override entirely:

```yaml
language: en   # default — male Andrew + female Ava
# language: zh # Chinese duo 云飞 + 小晓
# language: ja, es, fr, de also ship with built-in pairs

tts:
  voices:                # explicit override; wins over language_voices
    云飞:
      voice: zh-CN-YunyangNeural
      rate: "+10%"
    小晓:
      voice: zh-CN-XiaoxiaoNeural
      rate: "+10%"
```

The talk-show script just uses `[HostA]` / `[HostB]` (or `[云飞]` / `[小晓]`)
line prefixes — yumnb splits, renders each segment, prepends/appends a
procedurally generated chime, and concatenates with ffmpeg (re-encoded to
avoid DTS gaps).

## Cloud upload

`upload.provider: rclone` pushes the generated `talkshow.mp3`,
`deck.pptx`, and `summary.md` to any cloud destination supported by
[rclone](https://rclone.org) — OneDrive, Google Drive, S3 / R2 / MinIO,
Dropbox, Box, pCloud, WebDAV, and ~50 more. yumnb calls
`rclone copyto` + `rclone link`, and the shareable URLs replace the
local `file://` URIs in `links.json` and the webhook notification — so
users get clickable mp3 + ppt links in one shot.

```yaml
upload:
  provider: rclone          # none | rclone
  rclone:
    remote: "onedrive:yumnb"   # or gdrive:yumnb, s3:bucket/yumnb, dropbox:yumnb, …
    rclone_bin: rclone
    share: true                # generate shareable links via `rclone link`
    per_note_subfolder: true   # upload under <remote>/<note-folder>/
```

Set it up once with `rclone config`; the binary itself ships standalone
and works on Windows / macOS / Linux.

## Notification webhook

`notify.webhook_url` posts a small JSON payload after `publish`. Built-in
payload styles: `slack`, `discord`, `teams_workflow`, `generic`.

## Direct delivery (OpenClaw / Hermes)

If you want `yumnb` to do more than generate files, configure `deliver` in
`config.yaml`. This uses the local `openclaw message send` bridge, so the
same delivery path can target Telegram, Discord, Slack, Microsoft Teams,
and other channels OpenClaw/Hermes supports.

Example:

```yaml
deliver:
  provider: openclaw   # alias: hermes
  openclaw:
    channel: discord
    target: "123456789012345678"
    send_text: true
    send_files: true
  files: [talkshow.mp3, deck.pptx]
```

Switching surfaces is just a config change, e.g.:
- `channel: telegram`
- `channel: discord`
- `channel: slack`
- `channel: msteams`

So yumnb's skill code is not Telegram-specific.

## Privacy

yumnb runs entirely under the `output_dir` you configure. The only network
calls are:

- Whatever your AI provider does (skip with `provider: none`).
- `yt-dlp` to YouTube (skip by not feeding it YouTube URLs).
- `requests` to whatever URL you ingest.
- `edge-tts` to Microsoft's online voice endpoint (skip with `tts.enabled: false`).
- Your configured `notify.webhook_url`, if set.
- Your configured OpenClaw / Hermes delivery bridge, if `deliver.provider` is enabled.

No telemetry, no usage reporting. Read `scripts/*.py` — it's <1500 lines.

## License

MIT. See [LICENSE](LICENSE).

## Acknowledgements

Inspired by the workflow of "ingest → summarize → narrate → deck" that I
built on top of agent CLIs for personal study notes. This is the version
that contains nothing you wouldn't want on the public internet.

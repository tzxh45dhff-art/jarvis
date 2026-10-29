# JARVIS

An AI assistant that actually operates your Mac. You say what you want, in a terminal,
from a menu-bar app, or by voice, and it does it: reads and writes files, drives
apps through the accessibility API, browses the web, makes Word / Excel / PowerPoint /
PDF files, edits media, renders 3D scenes in Blender, manages your calendar and
messages, and remembers what you like.

```bash
jarvis                                   # interactive shell
jarvis "organise my Downloads folder"    # one request, then exit
jarvis "make a 5-slide deck on solar panels for school students"
jarvis "who is the CEO of Nvidia?"
jarvis cost                              # what the models cost today
```

It is built to be **safe** (nothing is deleted, only moved to the Trash; anything that
sends is shown to you first), **fast** (answers in 1–3 s; simple things never touch a
model), and **cheap** (requests are routed to the smallest model that does the job, and
every rupee is logged).

> Status: a personal project, used daily on Apple Silicon Macs. It is macOS-only by
> design: it calls EventKit, Contacts, Accessibility, AppleScript and Quartz directly.

## Contents

- [What it can do](#what-it-can-do) · [How it works](#how-it-works) · [Requirements](#requirements)
- [Quick start](#quick-start) · [Configuration](#configuration) · [Menu-bar app](#menu-bar-app)
- [Safety](#safety) · [Privacy](#privacy-what-leaves-your-mac) · [Memory](#memory) · [Cost](#cost)
- [Tests](#tests) · [Project layout](#project-layout) · [Troubleshooting](#troubleshooting)

## What it can do

About 150 tools, each declaring its own risk level. By area:

| Area | Examples |
|---|---|
| **Files & shell** | read, write, edit, search, move, copy, find anything on disk, run shell commands and Python, background jobs. Deletions go to the Trash. |
| **Web** | one-call grounded answers with sources (`web_answer`), search, fetch pages, news, papers, YouTube info and transcripts, downloads, HTTP APIs, stock/crypto/currency/weather/time-zone lookups. |
| **Documents** | designed PDFs and **Word (.docx)** from Markdown (cover, contents, tables, charts, Mermaid diagrams, photos, page numbers); PowerPoint decks; Excel workbooks with formulas and charts; convert between formats; read any of them back. |
| **Computer use** | click, type and scroll; read an app's controls by name (`ui_elements`) and press them (`press_element`, `set_field`, `choose_menu`, `file_dialog`); browser tools that act on the page itself; OCR-grounded clicking for apps with no accessibility tree. |
| **Media** | trim / join / speed / crop / compress video, transcribe audio, edit and combine images, draw charts. |
| **3D** | headless Blender with a [figure kit](docs/blender-figure-kit.md): clothed, posed, studio-lit characters from a short spec. |
| **Mac integrations** | Music, Calendar, Reminders, Notes, Contacts, Messages / WhatsApp / Mail (sending is always confirmed), clipboard, notifications, Shortcuts, brightness, dark mode, Wi-Fi, windows, timers. |
| **Memory & personalisation** | remembers facts and tastes you tell it or imply, saved routines ("when I say *study mode*…"), important dates, a personality setting. Local, inspectable, and can be switched off. |
| **App coaching** | playbooks for Word, Excel, Keynote, Figma, Xcode, DaVinci Resolve, GarageBand, iMovie, Illustrator and LeetCode: it can show you where to click, with on-screen highlights. |

## How it works

```
  bin/jarvis ─► jarvis.cli ─► daemon.client ──unix socket──► daemon.server  (one resident engine)
  menu-bar app (Swift) ───────────────────────────────────────────┘             │
                                                                           core.agent
                                       fast path · router · plan · act · verify │
                              ┌──────────────────────┬─────────────────────────┼───────────────┐
                         core.llm backends      tools.registry (150 tools)   memory.store    safety.policy
                  Azure OpenAI · Gemini · Claude ·   safety-gated, each      SQLite +         journal + Trash
                  on-device MLX                      with a risk category    embeddings
```

- **A resident engine.** Startup is slow (loading the memory model, OCR, warming the
  prompt cache), so one engine process stays running and every `jarvis …` command talks
  to it over a Unix socket. Exactly one engine can exist (an exclusive `flock`).
- **The agent loop** (`jarvis/core/agent.py`): observe → reason → act → verify. A request
  first tries the *fast path* (exact answers the Mac gives itself, e.g. the time or "pause"),
  then a saved routine, then the **router** picks a model. Multi-step jobs plan first, run
  up to 40 steps, notice when they go in circles and stop, and check their own result
  before saying "done".
- **Model routing** ([docs/model-routing-and-cost.md](docs/model-routing-and-cost.md)):
  simple questions and plain web lookups can run on a small model, acting and multi-step
  work on the main one, Blender on a stronger one, and a request hands over to the main
  model the moment a step fails.
- **Backends** are interchangeable (`stream()` is the whole contract): Azure OpenAI
  (default when configured), Gemini, Claude, or an on-device Gemma-4 through MLX
  ([docs/local-engine.md](docs/local-engine.md)).
- **Grounded, not guessed.** Anything about *this Mac* or *this person* is looked up, not
  recalled; independent lookups run in the same step; content read from web pages, files
  and screenshots is treated as data, never as instructions.

## Requirements

- A Mac with Apple Silicon, macOS 13 or later.
- Python 3.12+ (developed on 3.13).
- One model source: an **Azure OpenAI** resource, a **Gemini** key (free tier works), a
  **Claude** key, or enough memory (~16 GB free) for the on-device model.
- Optional: Google Chrome (to lay out PDFs and Word documents), Blender 5 in
  `/Applications` (3D; developed on 5.2), `ffmpeg` (video/audio), Xcode command-line
  tools (to build the menu-bar app).

## Quick start

```bash
git clone https://github.com/tzxh45dhff-art/jarvis.git && cd jarvis
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # add -r requirements-local.txt for the on-device model
./install.sh                                     # links `jarvis` into ~/.local/bin
cp .env.example .env                             # then edit it: pick a model source
jarvis doctor                                    # checks packages, models, macOS permissions
jarvis "what's my battery level?"
```

**Choosing a model source** (details in `.env.example`):

| | Setup |
|---|---|
| **Azure OpenAI** | `az login`, then set `JARVIS_AZURE_ENDPOINT` and `JARVIS_AZURE_DEPLOYMENT`. Keyless: no API key is stored. Your account needs the *Cognitive Services OpenAI User* role. |
| **Gemini** | `GEMINI_API_KEY=…` (free key from Google AI Studio). `jarvis --cloud`. |
| **Claude** | `ANTHROPIC_API_KEY=…`. `jarvis --claude`. |
| **On-device** | `pip install -r requirements-local.txt`; `jarvis --local`. No network, no key. |

With nothing set, JARVIS uses the first one that is configured, in the order above.

**macOS permissions.** `jarvis doctor` reports what is missing. Screen Recording (for
reading the screen) and Accessibility (for clicking, typing and reading app controls) go
to the app you launch `jarvis` from: macOS attributes permission to the *launching*
application. Calendar, Reminders, Contacts and Automation ask the first time they are
used; reading Messages needs Full Disk Access.

## Configuration

Settings come from, strongest first: the real environment, `<repo>/.env`, `~/.jarvis/.env`,
then `~/.jarvis/azure.json` (Azure only) and built-in defaults. `.env.example` lists every
variable with its meaning; the ones you will actually touch:

| Variable | What it does |
|---|---|
| `JARVIS_BACKEND` | `azure` \| `gemini` \| `claude` \| `local` (default: first configured) |
| `JARVIS_AZURE_ENDPOINT`, `JARVIS_AZURE_DEPLOYMENT` | your Azure OpenAI resource and main model |
| `JARVIS_AZURE_FAST_/WEB_/SEARCH_/BACKGROUND_/BLENDER_DEPLOYMENT` | optional cheaper (or stronger) models for specific kinds of request |
| `JARVIS_AZURE_CACHE_RETENTION` | `24h` keeps the prompt cache warm between requests |
| `JARVIS_VOICE_ENABLED` | `1` turns on voice (costs 1–2 GB of memory) |
| `JARVIS_WORKSPACE` | the folder relative paths resolve against |
| `JARVIS_LEARNING=0` | stop learning facts about you |

State lives in `~/.jarvis` (memory, journal, backups, logs, the cost ledger, the engine's
socket); nothing is written inside the repo.

## Menu-bar app

`companion/` is a small Swift app: hold **⌃⌥** to talk (speech-to-text happens in the
app), and a notch-style HUD shows what JARVIS is doing, highlights the controls it is
about to use, and can offer a suggestion at the right moment. It talks to the same engine.

```bash
cd companion && ./build.sh          # builds, runs its self-test, signs, installs, launches
jarvis companion start|stop|status
```

Sign it with an Apple Development identity (`JARVIS_SIGN_IDENTITY`, or the first one in
your keychain). Ad-hoc signing works, but macOS then forgets the Accessibility grant on
every rebuild. The app's bundle identifier is in `companion/Info.plist`: change it to your
own reverse-DNS name if you publish a build.

## Safety

The design goal is that JARVIS **cannot destroy anything**.

1. **Refused outright**: no confirmation enables these: recursive deletion of `/` or `$HOME`,
   fork bombs, `mkfs`, raw writes to `/dev/disk*`, `csrutil disable`, writes inside
   `/System`, `/usr/bin`, `/Applications`.
2. **Confirmed**: deletions, `sudo`, installs, `git push`, uploads, quitting apps, running a
   Shortcut, and **sending anything**: a message or email cannot be recalled, so it is
   shown in full first. Approve once, or a category for the session. Ambiguous recipients
   are refused, not guessed.
3. **Confirmed in context**: routine clicking and typing is not gated, but is when the
   focused app is one where a stray Return sends something (Messages, Mail, WhatsApp,
   Slack, a terminal).
4. **Reversible**: everything else. Deletions go to the **Trash**, never `unlink`;
   overwrites snapshot the original first; `/undo` reverses the last change.

Credentials are never stored in memory (key-, card- and password-shaped content is refused
before it reaches the database), and API keys are kept out of the environment of the shell
commands JARVIS runs. Prompt-injection hardening: tool output is data, wire markers in it
are neutralised, and the system prompt tells the model not to follow instructions found
inside files, pages or screenshots.

## Privacy: what leaves your Mac

| Backend | Sent off the machine |
|---|---|
| **On-device** | nothing |
| **Azure OpenAI / Gemini / Claude** | your request, the conversation so far, tool results (file contents, page text) and, for screen questions, screenshots. Memory, the journal and the cost ledger stay local. |

Web tools make ordinary web requests. Memory and the learned profile live in SQLite under
`~/.jarvis/data`; `/memory` shows them, `/forget` removes one, `JARVIS_LEARNING=0` stops
learning.

## Memory

Working (the live conversation), semantic (facts about you), preference, project, and
episodic (what happened and when). Stored in SQLite with 384-d embeddings next to each row;
retrieval fuses dense similarity with BM25 (reciprocal-rank fusion) because dense-only was
too noisy at this scale. Memories are retrieved only when relevant, and near-duplicates
update the existing row.

```
/memory               what it remembers          /forget <thing>    remove one
/undo                 reverse the last change    /history /status /tools /stop /quit
```

## Cost

With a metered backend, every request is written to `~/.jarvis/data/usage.jsonl` and
`jarvis cost [days]` summarises it by model and by kind of call, with the prompt-cache hit
rate. The biggest lever turned out to be the **prompt cache**, not the model: the fixed
prompt is ~14k tokens and a cache miss costs ~12× a hit. See
[docs/model-routing-and-cost.md](docs/model-routing-and-cost.md).

## Tests

```bash
.venv/bin/python tests/test_protocol.py          # each file is a standalone script: PASS/FAIL lines,
                                                 # non-zero exit on failure
for t in tests/test_*.py; do JARVIS_LIVE=0 .venv/bin/python -W ignore $t | tail -1; done
```

About 30 suites and 900 checks. Test runs use throwaway state (`tests/_isolate.py`), never
your real memory or ledger. By what they need:

- **Run anywhere** (offline; model calls are stubbed): `protocol safety memory prompt_equivalence
  robustness tools_audit toolsearch documents files_plus research online_plus speed_paths
  stall_and_video fast_actions workspace autonomy engine_audit azure_backend claude_backend
  gemini_backend guide media_edit`. Some need Chrome (documents) or Blender (`media_edit`).
- **Drive your real Mac** (click, type, open apps, audio): `apps ax input_permission
  integrations system_controls system_plus voice voice_mood proactive`. Run them while you
  are watching.
- **Need a running engine or a live model** (cost a few rupees): `stop_live integration
  assistant profile accuracy honesty_web`.

## Project layout

```
jarvis/
  cli.py, config.py        command line / all tunables, .env loading
  core/    agent.py        the loop: fast path → router → plan → act → verify
           router.py       which model for which request
           azure.py, cloud.py, anthropic.py, llm.py   the backends (+ ledger.py: cost log)
           prompts.py      system prompts      protocol.py   on-device tool-call codec
  tools/   registry.py     tool registry, risk gating     (one module per area:
           fs, shell, web, research, documents_pro, docx_writer, presentations, files_plus,
           media_edit, ax, control, browser, screen, pim, comms, macos, ...)
  blender/ figure_kit.py   characters and studio lighting for blender_script
  memory/  store.py        SQLite + hybrid retrieval      profile.py   learned preferences
  safety/  policy.py       risk classification            journal.py   mutation log, Trash, undo
  daemon/  server.py       the resident engine            client.py    socket client
  voice/                   speech in/out, wake word, voice-activity detection
  playbooks/*.md           per-app guidance loaded into the request when relevant
companion/                 menu-bar app (Swift Package)
docs/                      model routing and cost · Blender figure kit · on-device engine
tests/                     standalone suites
```

## Troubleshooting

- **`jarvis doctor`** checks packages, models and permissions. `jarvis logs` shows the
  engine log; `jarvis restart` restarts the engine (do this after changing code or `.env`).
- *"Calendar access was denied"*: the engine inherited the permissions of whatever started it.
  `jarvis stop`, then run `jarvis` from the terminal app you granted.
- *Azure answers 401/403*: `az login`, and check the *Cognitive Services OpenAI User* role.
- *Costs higher than expected*: `jarvis cost 7`; a low cache-hit rate means something keeps
  changing the prompt or tool list ([why](docs/model-routing-and-cost.md#the-prompt-cache-is-the-bill)).
- *`transcribe_media` says it needs `requirements-local.txt`*: transcription uses the
  on-device Whisper model, which is an optional install.

## License

[MIT](LICENSE). You are free to use, change and share it. Note that it drives your Mac,
so read what a tool does before you approve it, and use it at your own risk.

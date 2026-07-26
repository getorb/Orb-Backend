<h1 align="center">Orb Backend</h1>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.11+-3776AB?logo=python&logoColor=white" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/FastAPI-WebSocket%20+%20REST-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/Platform-Windows-0078D4" alt="Windows">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="MIT">
  <img src="https://img.shields.io/badge/iOS%20app-live-black?logo=apple&logoColor=white" alt="iOS app live">
</p>

<p align="center"><b>The brain behind <a href="https://orb.nathanlangley.dev">Orb</a> —
the AI assistant that talks first, on hardware you own.</b></p>

<p align="center">
  <a href="https://apps.apple.com/us/app/orb-ai/id6776376035"><b>Get the app</b></a> ·
  <a href="https://nathanlangley.dev/Orb-Wiki/self-hosting.html"><b>Setup walkthrough</b></a> ·
  <a href="API.md"><b>API reference</b></a> ·
  <a href="SELF_HOSTING.md"><b>Every setting</b></a>
</p>

The iOS app is the face; this server is everything behind it. The app works out of
the box on-device — this server is the optional half that gives it hands, memory,
and a machine of its own. Orb doesn't wait to be asked: the backend watches over
your projects, keeps a running model of what's going on, plans ahead, does approved
work on its own, and reaches your phone only when it's genuinely worth your
attention.

## What it does

| Capability | What that means |
|---|---|
| **Voice server** | A WebSocket voice loop: speech in (local Whisper), a real conversation out (neural TTS). No wake word on the phone — you just talk. |
| **Agent spine** | One thin loop where the model itself decides when to speak and when to act, with ~60 built-in tools: weather, mail, files, stocks, screen, sessions, missions, reminders, notifications. |
| **Multi-brain router** | Claude (through your own logged-in Claude Code CLI), Grok, Codex, or local models behind a single consistent voice. Swap mid-conversation; the persona never changes. |
| **The mind** | A process that runs *between* conversations: maintains a situation model of your life and projects, notices what changed, makes predictions, proposes real work, supervises what you approve. |
| **Sessions** | Delegate long-running jobs (builds, research, training runs) to tracked background agents you can watch, message, and stop from your phone. |
| **Push done right** | APNs notifications with deduplication, hard pacing, full-message payloads, and delivery receipts that can never be silently swallowed. |

## Run

Windows-first today (macOS backend planned). Python 3.11+.

```powershell
git clone https://github.com/getorb/Orb-Backend
cd Orb-Backend
python -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python scripts\setup.py     # guided: token, identity, boot check
venv\Scripts\python supervisor.py        # runs the server with crash recovery
```

Then give it an address your phone can reach (`tailscale serve --bg 8340`) and pair
the app: **menu (☰) → Your PC**. The full walkthrough with every gotcha spelled out:
**[Self-Hosting guide](https://nathanlangley.dev/Orb-Wiki/self-hosting.html)**.

Prefer to delegate? Install [Claude Code](https://docs.anthropic.com/en/docs/claude-code),
run `claude` in the repo root, and say **"Set up the Orb backend for me"** — the
repo's CLAUDE.md teaches it the whole flow.

## Bring your own everything

- **Model access** — the default conversational brain is Claude through your own
  logged-in [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI
  (`claude -p`). No API key is shipped, asked for, or stored. Grok/Codex CLIs and
  any OpenAI-compatible free tier can slot into the router chain
  (`ORB_FREE_BACKENDS`).
- **Push** — your own APNs key from your Apple Developer account (a `.p8` file —
  never commit it; the `.gitignore` already refuses). Background push to a *closed*
  app only works for an iOS build under your own team + bundle id; with the public
  App Store app you get in-app WebSocket notifications instead. Details in
  [SELF_HOSTING.md](SELF_HOSTING.md).
- **Mail** — your own Gmail app password; the built-in mail tools are read-only.

Every variable is documented in [.env.example](.env.example) and
[SELF_HOSTING.md](SELF_HOSTING.md).

## Architecture

| File | Role |
|---|---|
| `server_win.py` | The server: WS voice loop, HTTP API, brain dispatch, the notification choke point (dedup / pacing / receipts) |
| `agent.py` | The thin agent loop — the model decides talk-vs-tool, the harness executes |
| `brain.py` | Brain router: Claude CLI, Grok/Codex, free backends, local floor, cooldowns |
| `tool_registry.py` + `jtools/` | Every tool, one file each, registered with `@tool(...)` |
| `mcp_http.py` + `orb_mcp.py` | The tool surface as MCP: resident loopback `/mcp` for the Claude brain; stdio for Codex/Grok |
| `proactive_engine.py` | Zero-AI collection ticks, twice-daily synthesis, the scheduler (reminders survive restarts) |
| `mind.py` | Between-conversation agency: situation model, budgeted wakes, approval-gated proposals |
| `stt.py` / `personas.py` | Ears (faster-whisper) and voice (neural TTS + persona definitions) |
| `supervisor.py` | Keeps the backend alive; enables self-restart after code changes |

## The iOS app

**Live on the App Store (free):**
**https://apps.apple.com/us/app/orb-ai/id6776376035**

The app itself is closed-source. This backend is the open half of the pair: the app
talks to it over a WebSocket + a small HTTP API documented in [API.md](API.md) —
nothing stops you from building your own client.

## Design stances

- **Local-first.** Your data is collected on your machine and stays there.
- **Bring your own keys.** The backend never ships credentials.
- **Structural safety.** Autonomous surfaces can't reach destructive tools — those
  tools are absent from those surfaces, not merely discouraged. Anything that acts
  on the world requires your explicit approval, and every autonomous step is logged
  where you can read it.
- **Silence is a feature.** The proactive layer treats "nothing worth saying" as a
  correct, first-class outcome.

## Coming

- Fully offline local-model brain tier (Ollama / Qwen) as a first-class default.
- macOS backend.
- QR pairing from inside the app.

## License

MIT — see [LICENSE](LICENSE). The license covers this backend only; the Orb iOS app
and the Orb name/branding are not part of it.

Built by [ninjahawk](https://github.com/ninjahawk).

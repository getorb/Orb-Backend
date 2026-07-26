# Orb Backend — assistant guide

You are working in the Orb Backend: the self-hosted server half of Orb, a proactive
voice AI presence. The iOS app (closed-source) connects to this server over one
WebSocket (`/ws/voice`) plus a small HTTP API (`API.md`). This file is written for
Claude Code so you can answer architecture questions and run the guided setup
immediately after clone.

## Setup assistant

When the user says something like **"set up the Orb backend for me"**, guide them
through these steps interactively (or run `python scripts/setup.py`, which automates
the same flow — prefer running it and narrating):

1. `python -m venv venv` + `venv\Scripts\pip install -r requirements.txt`.
2. Copy `.env.example` → `.env`; generate `ORB_TOKEN`
   (`python -c "import secrets; print(secrets.token_hex(32))"`), ask their name and
   city (`USER_NAME`, `ORB_DEFAULT_LOCATION`).
3. Push (optional, needs paid Apple Developer): send them to
   https://developer.apple.com/account/resources/authkeys/list — key with APNs
   enabled, `.p8` into the repo root (gitignored), fill `APNS_KEY_ID` /
   `APNS_TEAM_ID` / `APNS_BUNDLE_ID`.
4. Boot: `venv\Scripts\python supervisor.py`, verify
   `http://localhost:8340/api/health`.
5. Reachability: recommend `tailscale funnel 8340` (public) or `tailscale serve 8340`
   (tailnet-only); with a public URL set `ORB_HTTP_AUTH=1`.
6. Pairing: tell them to enter the URL + `ORB_TOKEN` in the Orb app
   (Settings → Your Server).

Never write secrets anywhere but `.env`. Never commit `.env` or `*.p8` — the
`.gitignore` blocks both; do not fight it.

## System overview

Phone (iOS app) ⇄ WebSocket + HTTP ⇄ `server_win.py` (FastAPI, :8340)
→ agent loop with ~60 tools → brains: Claude via the user's logged-in `claude -p`
CLI (default), Grok/Codex CLIs, OpenAI-compatible free tiers, local Ollama.
Pushes go phone-ward through APNs (`apns.py`) with the user's own key.

- `server_win.py` — the server: WS voice loop, HTTP API, brain dispatch, push
  choke-point `_push_notification` (dedup/pacing/receipts).
- `agent.py` — the thin agent loop (model decides talk-vs-tool; harness executes).
- `brain.py` — brain router: Claude CLI, free backends, local floor, cooldowns.
- `tool_registry.py` + `otools/` — every tool, one file each, registered with
  `@tool(...)`; `import_all_tools()` loads them.
- `mcp_http.py` + `orb_mcp.py` — the tool surface as MCP: resident loopback
  `/mcp` for the Claude-family brain; stdio server for Codex/Grok.
- `proactive_engine.py` — zero-AI collection ticks + twice-daily synthesis at fixed
  clock hours + the scheduler (user reminders survive restarts; items missed while
  the backend was off fire late, marked late, within `ORB_SCHED_GRACE_H`).
- `mind.py` — between-conversation agency: situation model, budgeted wakes,
  proposals that require explicit approval. Read `AGENCY.md`-style docs before
  changing behavior here; budgets and pacing are load-bearing.
- `stt.py` (faster-whisper) / Edge-TTS in `personas.py` — ears and voice.
- `supervisor.py` — keeps the backend alive; enables `POST /api/system/restart`.

## Key design decisions

- **Zero API keys.** Claude is reached only via the user's logged-in CLI. Do not
  introduce `ANTHROPIC_API_KEY` usage.
- **Structural safety.** The synthesis/proactive layer emits a fixed JSON plan — it
  cannot call arbitrary tools; destructive capabilities simply don't exist on
  autonomous surfaces. Keep it that way.
- **Notifications are paced.** Everything funnels through `_push_notification`:
  topic dedup + cooldowns for proactive pushes; direct replies and action receipts
  bypass dedup (`dedupe=False`) because an answer to the user's own question is
  never a duplicate.
- **The token boundary.** `ORB_TOKEN` gates the WS always, and HTTP when
  `ORB_HTTP_AUTH=1`. Loopback is not a trust boundary once a tunnel forwards to
  a local port.

## Run / verify

- Run: `venv\Scripts\python supervisor.py`; health at `/api/health`.
- After editing server code: restart (`POST /api/system/restart` when supervised),
  then re-check health. Logs: `backend.log` / `backend_err.log` (append mode).
- Quick import sanity: `python -c "import server_win"` catches most wiring mistakes.

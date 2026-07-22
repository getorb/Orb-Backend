# Orb Backend — Self-Hosting (the manual path)

Everything `scripts/setup.py` automates, written out. Windows-first; paths below use
PowerShell conventions.

## 1. Install

```powershell
git clone https://github.com/getorb/Orb-Backend
cd Orb-Backend
python -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env
```

Python 3.11+ required. First server start downloads the Whisper model (~150MB for
`base.en`) — one-time.

## 2. Configure `.env`

Open `.env` and set at minimum:

```
ORB_TOKEN=<generate one: python -c "import secrets; print(secrets.token_hex(32))">
USER_NAME=<what the assistant calls you>
ORB_DEFAULT_LOCATION=<your city, for no-location weather asks>
```

`ORB_TOKEN` is the shared secret between your backend and your phone. Treat it like
a password; rotate it by changing the value and re-entering it in the app.

### The brain

The default conversational model is **Claude via your own logged-in
[Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI** (`claude -p`) —
zero API keys. Install it, log in once, and the backend finds it on PATH. Optional:

- `codex` / `grok` CLIs, if you have them — they slot in as switchable brains.
- Any OpenAI-compatible endpoint via `ORB_FREE_BACKENDS` (JSON list of
  `{name, base_url, model, api_key_env}`).
- A local Ollama floor via `OLLAMA_URL` / `OLLAMA_MODEL` (full local-first tier is on
  the roadmap).

### Push notifications (optional, needs a paid Apple Developer account)

1. [developer.apple.com → Certificates, Identifiers & Profiles → Keys](https://developer.apple.com/account/resources/authkeys/list)
2. Create a key with **Apple Push Notifications service (APNs)** enabled, download the
   `.p8` (Apple only lets you download it once), note the **Key ID** and your
   **Team ID**.
3. Put the `.p8` in the repo root (gitignored) and set:

```
APNS_KEY_PATH=AuthKey_XXXXXXXXXX.p8
APNS_KEY_ID=XXXXXXXXXX
APNS_TEAM_ID=YYYYYYYYYY
APNS_BUNDLE_ID=<the app's bundle id>
APNS_ENV=auto
```

`APNS_ENV=auto` tries production and sandbox — dev builds get sandbox tokens,
TestFlight/App Store builds get production tokens, and a key restricted to one
environment is the classic "pushes work in Xcode but not TestFlight" failure.

**Important — which app your key can push to.** An APNs key is tied to *your* Apple
Developer team, and Apple only delivers a push when the signing key's team owns the
target app's bundle id. The public App Store / TestFlight Orb app is signed under the
project owner's team, so a key from your own account **cannot** push to it. Self-hosters
using the store app still get in-app notifications over the live WebSocket
(`ORB_WS_NOTIFICATIONS=1`, on by default) while the app is open — background push
while the app is *closed* requires building the iOS app yourself under your own team +
bundle id (or a future shared push relay, which is not provided).

### Mail (optional, read-only Gmail) — free, your own account

Email is where the highest-value proactivity lives (a reply you missed, a deadline, a
commitment). Orb reads yours through **your own Google account** — the developer runs
no server and pays Google nothing; you authorize your own inbox and the credential
stays on your machine. It costs $0: Gmail's API/IMAP access is free at any volume a
person generates.

Today's path is a **Gmail app password** (read-only IMAP). Step by step:

1. Turn on 2-Step Verification: [myaccount.google.com/security](https://myaccount.google.com/security)
   → **2-Step Verification** (required before app passwords exist).
2. Create an app password: [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
   → name it "Orb" → Google shows a 16-character code **once**.
3. Put it in `.env`:

   ```
   GMAIL_ADDRESS=you@gmail.com
   GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx
   ```

Orb's mail tools are **read-only** by design — it can check, search, and read, but
never send. Nothing about your mail leaves your machine except to the AI model *you*
configured to answer a request. Leave these unset and every mail feature degrades to a
clear "not configured" line.

> Honest note on the tradeoff: an app password is a bit clunky (it needs 2-Step on) and
> it's the pragmatic zero-cost option a solo project can ship today. A cleaner
> **Google OAuth (Gmail API)** flow — the same approach other self-hosted assistants use,
> also free, tokens stored locally — is the planned upgrade. Neither one routes your
> mail through any developer server; both are your own account, your own machine.

### Search (optional)

Image and web search use a self-hosted [SearXNG](https://github.com/searxng/searxng)
metasearch instance when one is running; without it they fall back to plain scrapers
(same results, just less reliable — nothing breaks). The repo ships a ready config at
`searxng/settings.yml` (it only adds the JSON API + moderate safe-search on top of the
SearXNG defaults). Start it with Docker:

```powershell
docker run -d --name jarvis-searxng -p 8888:8080 `
  -v "${PWD}/searxng:/etc/searxng" --restart unless-stopped searxng/searxng
```

The backend looks for it at `http://localhost:8888`. Generate your own `secret_key`
in `searxng/settings.yml` first (`python -c "import secrets; print(secrets.token_hex(16))"`).

## 3. Run

```powershell
venv\Scripts\python supervisor.py     # recommended: crash recovery + /api/system/restart
# or bare:
venv\Scripts\python server_win.py
```

Health check: `http://localhost:8340/api/health`. Logs land in `backend.log` /
`backend_err.log` (append mode, one separator line per launch).

## 4. Reach it from your phone (Tailscale, step by step)

Your phone needs a route to your PC. [Tailscale](https://tailscale.com) is a free
personal VPN that gives your devices a private encrypted network — it's the
recommended transport, and it means your backend does **not** have to be exposed to
the public internet.

**One-time setup:**

1. Make a free Tailscale account: [login.tailscale.com/start](https://login.tailscale.com/start).
2. Install Tailscale on **this PC**: [tailscale.com/download/windows](https://tailscale.com/download/windows).
   Sign in — the PC joins your private network (your "tailnet").
3. Install the Tailscale app on **your iPhone** (App Store) and sign in with the same
   account. Now the phone and PC can see each other privately, from anywhere.

**Expose the backend to your own devices (recommended — private):**

```powershell
tailscale serve 8340        # tailnet-only: only YOUR signed-in devices can reach it
tailscale serve status      # prints the https://<your-machine>.<tailnet>.ts.net URL
```

`serve` keeps the backend **private to your tailnet** — the URL is not reachable from
the public internet, so a leaked URL is not a leaked backend. Your phone reaches it
over the encrypted tailnet from anywhere (cellular included), because the phone is one
of your tailnet devices. **This is the right default.**

Then in the Orb app: **Settings → Your Server** → paste the printed
`https://…ts.net` URL and your `ORB_TOKEN`.

**Public exposure (`funnel`) — only if you know you need it:**

```powershell
tailscale funnel 8340       # PUBLIC: anyone on the internet who has the URL can reach it
```

Funnel puts the backend on the open internet. **If you use it, you MUST set
`ORB_HTTP_AUTH=1` in `.env`** so every HTTP request also requires your token — otherwise
your notifications, sessions, and settings endpoints answer to anyone with the URL.
Prefer `serve` unless you have a specific reason a non-tailnet device must connect.

> The port above is `8341` if you changed `ORB_PORT`; otherwise it's `8340`. The
> WebSocket (voice) always requires `ORB_TOKEN`; `ORB_HTTP_AUTH=1` extends that
> requirement to the HTTP API and is mandatory for any public (`funnel`) URL.

## What it watches on the machine (and how to turn it off)

Being a *presence* is the product, so out of the box the backend observes its own
host: the active window title, CPU/disk/GPU pressure, clipboard changes, input
idleness, file activity in the project folders you list (`ORB_PROJECT_DIRS`), and
the screen only when you explicitly ask it to look. All of it stays on your machine
(see the privacy stance in the README) — collection is local; nothing is uploaded
anywhere by the backend itself.

Dials: `ORB_MIND=0` turns off the between-conversation agency; leaving
`ORB_PROJECT_DIRS` unset means no project watching; direct system alerts pace
themselves (30-min cooldowns + an `ORB_ALERT_BOOT_GRACE_S` quiet window after every
boot, default 600s). If you want a piece of this gone entirely, it's one small,
readable module per watcher — delete or comment the `start()` call and it's gone.

## 5. Environment reference

Required:

| Variable | Meaning |
|---|---|
| `ORB_TOKEN` | Shared secret between app and backend. Auto-generated on first boot if missing. |

Common:

| Variable | Default | Meaning |
|---|---|---|
| `USER_NAME` | — | What the assistant calls you |
| `ORB_PERSONA` | `orb` | `orb`, `jarvis`, or `ultron` (voice + wake word + character) |
| `TTS_VOICE` | persona's | Edge-TTS voice override |
| `ORB_DEFAULT_LOCATION` | `New York` | Weather fallback when no location is spoken |
| `ORB_HTTP_AUTH` | `0` | `1` = require the token on HTTP `/api/*` too |
| `ORB_SPINE_BRAIN` | `haiku` | Default brain: haiku/sonnet/opus/grok/codex/local |
| `ORB_PROJECT_DIRS` | — | JSON `[[label, path], ...]` — dirs the proactive layer watches |

Voice input:

| Variable | Default | Meaning |
|---|---|---|
| `WHISPER_MODEL` | `base.en` | faster-whisper model |
| `WHISPER_DEVICE` | `cpu` | `cpu` or `cuda` |
| `WHISPER_COMPUTE` | `int8` | compute type |

Push: `APNS_KEY_PATH`, `APNS_KEY_ID`, `APNS_TEAM_ID`, `APNS_BUNDLE_ID`,
`APNS_ENV` (auto), `ORB_WS_NOTIFICATIONS` (1 = mirror pushes over the live WS when
APNs didn't deliver), `ORB_NOTIFY_TOPIC_COOLDOWN_H` (8 — duplicate-content window).

Mail: `GMAIL_ADDRESS`, `GMAIL_APP_PASSWORD`. Vision: `GEMINI_API_KEY` (free tier at
aistudio.google.com; falls back to `claude -p` without it).

Proactive layer: `ORB_SYNTHESIS_HOURS` (`6,18` — fixed clock hours for the
twice-daily synthesis), `ORB_SYNTHESIS_MODEL` (`sonnet`), `ORB_MAX_EXTRA_REVIEWS`
(2/day), `ORB_SCHED_GRACE_H` (18 — reminders missed while the backend was off still
fire, marked late, within this window), `ORB_SESSION_CHECKBACK_MIN` (20).

Delegated-session progress pushes: `ORB_SESSION_PROGRESS` (`1`; `0` = off — live
"still on it" pushes for long-running delegated sessions), `ORB_SESSION_PROGRESS_AFTER_S`
(`90` — only start pushing once a session has run this long),
`ORB_SESSION_PROGRESS_EVERY_S` (`240` — minimum gap between progress pushes).

The mind (between-conversation agency): `ORB_MIND` (1; `0` = off),
`ORB_MIND_MAX_WAKES` (per day), `ORB_MIND_MAX_EXPRESS` (self-initiated
notifications per day), `ORB_MIND_EXPRESS_GAP_MIN` (pacing floor),
`ORB_MIND_MAX_STEPS` / `ORB_MIND_MAX_STEPS_DEEP` (agent-loop budgets).

Brain plumbing: `CLAUDE_MODEL`, `ORB_MCP_BRAIN` (1 — Claude-family turns run as one
`claude -p` with native MCP tools + resumed sessions), `ORB_MCP_TURN_TIMEOUT` (90s
before a turn hands off to the background finisher), `ORB_MCP_LATE_CAP` (600s),
`ORB_FREE_BACKENDS`, `ORB_FREE_TIMEOUT`, `ORB_BACKEND_COOLDOWN`,
`OLLAMA_URL`, `OLLAMA_MODEL`, `ORB_EMBED_MODEL`, `ORB_AGENT_SPINE` (1),
`ORB_PARALLEL_TOOLS` (0), `ORB_ACK` (0), `ORB_MEMORY` (0),
`ORB_SEMANTIC_ROUTER` / `ORB_SEMANTIC_THRESHOLD` (legacy router),
`ORB_MOBILE_NO_WAKE`.

Defaults live in source; when in doubt, grep the variable name — every read is a
single `os.getenv` line with a comment.

## Troubleshooting

- **App says "not reachable"** — is the server listening (`/api/health` locally)? Is
  the funnel/serve URL current (`tailscale funnel status`)? Token mismatch closes the
  WS immediately: re-paste `ORB_TOKEN` in the app.
- **Pushes arrive in dev builds but not TestFlight** — your APNs key or `APNS_ENV` is
  environment-restricted; use a key enabled for both, keep `APNS_ENV=auto`.
- **No pushes at all** — check `backend.log` for `[apns]` lines: `configured but
  broadcast returned 0` means the app never registered a token (open the app once);
  `credentials not set` means the `.env` APNs block is incomplete.
- **First voice turn is slow** — Whisper downloads its model on first run; watch
  `backend_err.log`.
- **Port 8340 busy** — another instance is up; the supervisor restarts crashed
  backends automatically, so don't run two supervisors.
- **401 on HTTP API** — you set `ORB_HTTP_AUTH=1`; send `X-Jarvis-Token: <token>`
  (the app versions that predate the header need it off).

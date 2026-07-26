# Orb Backend â€” API (abridged)

The app-facing surface: one WebSocket for conversation, a small HTTP API for panels
and control. Source of truth is `server_win.py` â€” every route is defined there with a
docstring; this file is the map, not the spec.

Auth: the WebSocket requires `?token=<ORB_TOKEN>`. HTTP routes require
`X-Orb-Token` (legacy `X-Orb-Token` or `?token=` also accepted) when `ORB_HTTP_AUTH=1`; health/persona/card/screen
stay open.

## WebSocket â€” `/ws/voice`

Client â†’ server messages:

| type | Meaning |
|---|---|
| `hello` | Client intro: `pushToken`, `connectors` (calendar/reminders/â€¦), optional phone tool schemas |
| `audio` | 16kHz Int16 PCM chunk (base64) â€” backend runs local Whisper |
| `transcript` | Typed text turn (`typed: true` skips the wake-word gate) |
| `mute` | `{on: bool}` â€” backend drops audio frames pre-Whisper |
| `tool_result` | Phone answering a `tool_call` the backend relayed |
| `context` | Phone context snapshot (calendar/reminders) |

Server â†’ client:

| type | Meaning |
|---|---|
| `ready` | Hello fully processed â€” mic can arm |
| `ack` | Optional instant acknowledgment before a slow answer (`ORB_ACK=1`) |
| `audio` | The reply: TTS mp3 (base64) + `text` |
| `card` / `chat` / `game` | Open a HUD panel |
| `tool_call` | Backend asking the phone to run an on-device tool (calendar, remindersâ€¦) |
| `notification` | In-app copy of a push (skipped when APNs already delivered) |
| `session_event` | Live feed line from a tracked background session |
| `cc_reply` | Reply from a messaged background session |
| `screen` | Screenshot payload |

## HTTP

Health & identity: `GET /api/health`, `GET /api/persona`, `GET /api/settings`,
`POST /api/settings`.

Brains: `GET /api/brains` (tiers + live availability + gray-out reasons),
`POST /api/brain` (switch).

Notifications: `POST /api/notify` (send one; optional `show: {image?, link?}` payload
for kind `show`), `GET /api/notifications/recent`
(history: `body`, `full_body`, `opened`, `session`), `POST /api/notification/feedback`
(tap tracking).

Inbox: `POST /api/inbox/image` (`{name, jpeg_b64, note?}` — phone→PC image drop;
saved to `~/Desktop/Orb_Inbox/`, never overwrites, note lands beside it as `.txt`).

Machines: `GET /api/machines`, `POST /api/machines/register` (heartbeat every ~60s;
a row reads offline after 180s).

Proactive: `GET /api/proactive/status` (brief + upcoming), `POST /api/proactive/review`
(run now; `{"synthesis": true}` for the full run), `POST /api/proactive/schedule`
(`{time, title, body, kind}` â€” `HH:MM` or ISO), `POST /api/proactive/cancel`,
`GET /api/proactive/scheduled`, `POST /api/proactive/context` (phone snapshot),
`POST /api/proactive/proposal/respond` (approve/reject a proposed build).

The mind: `GET /api/mind/status`, `POST /api/mind/wake` (loopback only).

Sessions: `GET /api/sessions`, `GET /api/sessions/{name}/activity` (scrollback),
`GET /api/sessions/{name}/events`, `POST /api/sessions/start`
(`{task, engine, machine, name?, working_dir?}`), `POST /api/sessions/{name}/message`
(202 immediately; reply arrives as `cc_reply`), `POST /api/sessions/{name}/stop`,
`POST /api/resume-session`.

Cards & screen: `GET /api/card/{preset}` (clock|system|weather|news),
`GET /api/card/payload/{token}`, `GET /api/screen/{token}`.

System: `POST /api/system/restart` (loopback only; 409 when unsupervised),
`POST /api/internal/mcp_relay/{token}` (loopback only â€” phone-tool relay for CLI
brains).

`/mcp` (streamable-HTTP, loopback only) exposes the full tool registry to the resident
Claude-family brain. It is not part of the app contract.

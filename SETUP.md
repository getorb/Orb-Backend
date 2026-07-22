# Orb Backend — Guided Setup (Claude Code)

The fastest path from clone to a phone that answers: about five minutes, plus however
long Apple's developer portal takes you.

## Prerequisites

- Windows 10/11, Python 3.11+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code):
  `npm install -g @anthropic-ai/claude-code`, then log in once with `claude`
- For push notifications: an Apple Developer Program membership ($99/yr) to create an
  APNs key. Skip it and everything still works except background pushes while the app is
  closed — you still get in-app notifications while it's open. A BYO key can only push to
  an iOS build signed under *your own* team + bundle id, not the public App Store app; see
  the push note in [SELF_HOSTING.md](SELF_HOSTING.md).

## The guided flow

```
git clone https://github.com/getorb/Orb-Backend
cd Orb-Backend
claude
```

Then say: **"Set up the Orb backend for me."**

Claude Code reads this repo's CLAUDE.md and walks you through:

1. **Dependencies** — creates the venv, installs requirements, verifies imports.
2. **Your token** — generates a strong `ORB_TOKEN` (the shared secret between the
   app and your backend) and writes your `.env`.
3. **Push (optional)** — shows a QR code straight to the Apple Developer APNs Keys
   page; you create the key on your phone, drop the `.p8` next to the repo (it is
   gitignored), and paste the Key ID + Team ID.
4. **Boot check** — starts the server and verifies `/api/health` answers.
5. **Pairing** — prints your backend URL + token as a QR code to enter in the Orb app
   (Settings → Your Server).

Everything above is also a plain script — `python scripts/setup.py` — if you'd rather
not involve an AI in your infrastructure. Same steps, same QR codes.

## Reaching it from anywhere

The app needs a route to your PC. The recommended path is
[Tailscale](https://tailscale.com) — a free personal VPN, so your backend never has to
touch the public internet:

```
tailscale up
tailscale serve 8340         # tailnet-only: only YOUR devices reach it (recommended)
tailscale serve status       # prints your https://<machine>.<tailnet>.ts.net URL
```

Point the app at the printed URL (Settings → Your Server) with your `ORB_TOKEN`. Your
phone reaches it from anywhere as long as it's signed into the same Tailscale account.

Only use `tailscale funnel 8340` if you specifically need a **public** URL — and if you
do, set `ORB_HTTP_AUTH=1` in `.env` so the HTTP API also requires your token (the
WebSocket always does). A public funnel without that flag exposes your data endpoints
to anyone with the URL.

## If something doesn't work

[SELF_HOSTING.md](SELF_HOSTING.md#troubleshooting) has the failure catalog: APNs
environment mismatches, token mismatches, Whisper's first-run model download, port
conflicts, and how to read the supervisor's logs.

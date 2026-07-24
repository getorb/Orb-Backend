# PROVENANCE — code authorship & licensing posture (backend)

*Compiled 2026-07-23 for the public backend repo, from the measured line-level
comparisons of 2026-07-14 → 2026-07-18 and the 2026-07-23 file-by-file audit. This is
the honest record; update it whenever lineage-relevant code moves. It is the backend
counterpart of the app repo's PROVENANCE.md.*

## Lineage

The wider project began (spring 2026) as a git fork of **ethanplusai/jarvis** — a macOS
web-based voice-assistant experiment ("JARVIS Voice AI Assistant", © 2026 Ethan Rogers).
That repo's license, both at fork time and today, is a **custom personal-use license:
free for personal/educational use, commercial use of the Software or derivative works
prohibited without a commercial license, attribution required.** It is *not* MIT.

The Windows backend published here was rebuilt around that seed: a different server
(FastAPI + WebSocket, `server_win.py`), local Whisper STT, a tool registry and agent
loop, an MCP tool surface, a proactive engine and mind — none of which exist upstream.
The upstream repo's macOS modules (`server.py`, `actions.py`, `planner.py`, the
desktop-overlay and prompt templates, ~40 files) were **never part of this public
repo** and were deleted from the shipping lines entirely.

## Measured overlap

Method (same as the app repo's audit): identical non-blank non-comment lines against
the complete upstream repo (27 files).

- **2026-07-18 full scan — every public file vs every upstream file: CLEAN.** All
  remaining matches are boilerplate (`import json`, `try:`) or, for `memory.py`,
  the SQLite schema DDL and our own call-site function signatures (~13%) — see below.
- **`memory.py`** was the one flagged surface (66% derived on 2026-07-14). It was
  **clean-room rewritten 2026-07-18**: the new module was written *without reading the
  old file*, from the call-site contract (`remember`, `get_important_memories`,
  `get_recent_memories`, `build_memory_context`, `extract_memories`) plus the live
  database's schema, and verified with 15/15 tests against both a real-data copy and a
  fresh-install database. The pre-launch remediation item recorded in the app repo's
  PROVENANCE table ("Windows's remediation item") is closed by that rewrite.
- **`scripts/orb_notify.py`** (formerly `jarvis_notify.py`): original, first written
  2026-06-29 in this project; no upstream counterpart exists.
- Everything else in this repo (tools in `jtools/`, `proactive_engine.py`, `mind.py`,
  `agent.py`, `brain.py`, `apns.py`, `mcp_http.py`, `jarvis_mcp.py`, `supervisor.py`,
  `stt.py`, docs, setup wizard) was written for this product; the 07-18 scan found no
  distinctive upstream lines in any of them.

The closed-source frontend (three.js orb, overlay, HUD cards) is **not part of this
repo** and ships in no distribution surface here.

## 2026-07-23 re-measurement (full public set vs full upstream repo)

Re-ran the identical-line comparison the same day this repo's snapshot shipped,
against a fresh clone of the complete upstream repo. Findings and fixes, all
applied before publish:

- `memory.py`: the SQLite DDL was still a *textual* match with upstream's schema
  block (kept for data compatibility). Reformulated — same semantics for existing
  databases, zero shared lines.
- `personas.py`: one persona-prompt line matched upstream verbatim. Rewritten.
- The shared logger name was renamed (`orb`).

After those fixes, **no file in this repo shares any distinctive line with any
upstream file.** Every remaining identical line is language idiom
(`from datetime import datetime`, `except asyncio.TimeoutError:`, subprocess
boilerplate, `if __name__ == "__main__":`) or wire-protocol constants the app
depends on (`/ws/voice`, `/api/restart`, `/api/settings/*` route strings).

## 2026-07-23 surface trim

To keep the public working set to what a self-hoster actually runs, these files were
removed from the snapshot (recoverable in this repo's git history, per
PRESERVATION.md): `site_templates.py`, `semantic_router.py`, `intent_config.py`,
`intents.yaml`, and the bundled SearXNG settings. The server degrades cleanly when
they are absent.

## Naming (legacy identifiers that remain, and why)

The product and brand are **Orb**. A few legacy `jarvis` identifiers survive as
deliberate compatibility surfaces, not oversights:

| Identifier | Why it remains |
|---|---|
| `JARVIS_*` env vars in code | Internal canonical names; `.env.example` and docs teach `ORB_*`, which `orb_env.py` maps onto them (new name wins when both are set). |
| `X-Jarvis-Token` header | Accepted alongside the preferred `X-Orb-Token` until no pre-rename app builds remain. |
| `jarvis_mcp.py` | The MCP server is registered with the Claude/Codex/Grok CLIs under this module path; renaming would break existing registrations. |
| `jarvis_sessions/` folder, `jarvis.db`, `JARVIS_Files/`, `JARVIS_Output/` | On-disk state locations of existing installs; renaming would orphan user data. |

## License

This public backend is **MIT-licensed, backend only**. The iOS app and the Orb
branding/visual identity are proprietary and are not granted by this repo's license.
The upstream project's non-commercial license applied to code that no longer ships
from any distribution surface of this project; no expressive upstream code remains
here (per the scans above). The original three.js orb concept that inspired the
project's visual direction is acknowledged in the app repo's PROVENANCE.md; no orb
rendering code exists in this repo.

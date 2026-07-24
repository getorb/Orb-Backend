"""Training job watcher — monitors ~/Desktop/jarvis_sessions/*.json for job
progress and completions.

Polls every 10 seconds. For any session that is NOT a CC session (type != "cc"):
  - ANY change (status or message — e.g. a training step advancing) gets a
    live session_event broadcast, so the sessions view stays live even for
    external processes (a training script in a different venv/process can't
    call log_activity directly — this poll loop is its only path to "live").
  - A transition INTO "done" or "failed" additionally fires a push notification.
    This bypasses the AI reasoning pipeline entirely — when a job finishes or
    breaks, you always want to know, no screening required.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Callable

log = logging.getLogger("orb.training_watcher")

_SESSIONS_DIR = Path.home() / "Desktop" / "jarvis_sessions"
_POLL_INTERVAL = 10  # seconds — cheap (glob + JSON parse), worth being snappy

# session_name -> last known (status, message) so we only act on real changes
_known: dict[str, tuple[str, str]] = {}

_push_fn: Callable | None = None
_broadcast_fn: Callable | None = None


def wire(push_fn: Callable, broadcast_fn: Callable | None = None) -> None:
    global _push_fn, _broadcast_fn
    _push_fn = push_fn
    _broadcast_fn = broadcast_fn


async def run_forever() -> None:
    log.info("[training_watcher] started — polling every %ds", _POLL_INTERVAL)
    # Seed known states from existing files so a restart doesn't re-fire old completions.
    _seed_known()
    while True:
        try:
            await _poll()
        except Exception as e:
            log.warning(f"[training_watcher] poll error: {e}")
        await asyncio.sleep(_POLL_INTERVAL)


def _seed_known() -> None:
    """Read current statuses at startup so we don't push stale completions."""
    for path in _SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if data.get("type") == "cc":
            continue
        name = data.get("session", path.stem)
        _known[name] = (data.get("status", ""), data.get("message", ""))


async def _poll() -> None:
    if not _SESSIONS_DIR.exists():
        return

    for path in _SESSIONS_DIR.glob("*.json"):
        # Skip inbox files
        if "_inbox" in path.stem:
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue

        # CC sessions are handled by the inbox relay, not us
        if data.get("type") == "cc":
            continue

        name = data.get("session", path.stem)
        status = data.get("status", "")
        message = data.get("message", "")
        prev = _known.get(name)
        _known[name] = (status, message)

        if prev == (status, message):
            continue  # genuinely nothing changed

        # Any change at all -> live push (cheap, no LLM, this is what makes the
        # sessions view "live" for external processes like a training script).
        if _broadcast_fn:
            try:
                await _broadcast_fn(name, "lifecycle", message or status, {})
            except Exception:
                pass

        # Terminal transition -> also a real notification.
        prev_status = prev[0] if prev else None
        if prev_status != status and status in ("done", "failed"):
            await _notify(name, status, data)


async def _notify(name: str, status: str, data: dict) -> None:
    if not _push_fn:
        return

    message = data.get("message", "")
    progress = data.get("progress")
    desc = data.get("description", "")

    if status == "done":
        emoji = "✓"
        kind = "done"
        title = f"Job finished: {name}"
    else:
        emoji = "✗"
        kind = "alert"
        title = f"Job failed: {name}"

    body_parts = []
    if desc:
        body_parts.append(desc)
    if message:
        body_parts.append(message)
    if progress is not None and status != "done":
        body_parts.append(f"Progress: {progress}%")

    body = " — ".join(body_parts) if body_parts else f"{name} {status}"

    log.info(f"[training_watcher] {emoji} {title}: {body[:80]}")
    await _push_fn(title, body, kind)

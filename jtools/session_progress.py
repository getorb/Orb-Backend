"""Keep the user in the loop while delegated sessions work (his 07-03 ask:
"I should be in the loop and see live what's going on").

The real live view is the app's Sessions panel (iOS, queued — backend already
streams `session_event` per tool call). This watcher covers the gap NOW, and
forever covers the app-closed case, with honest pushes instead of silence:

  • a delegated session still running after START_AFTER gets one
    "Still on it" push;
  • further pushes only when its status message CHANGES, floored at EVERY_S
    apart per session — progress you can feel, never a heartbeat spam;
  • a session whose process died while its record still says "running" gets
    one "Session looks dead" alert — crashes were invisible before
    (completion pushes only fire on clean ends via sessions_tool.wire_notify).

Zero AI, zero new state files: reads the same Desktop/jarvis_sessions/*.json
records everything else uses; pacing state is in-memory (a restart just means
one fresh "Still on it" for a long-runner — acceptable). Kill switch:
JARVIS_SESSION_PROGRESS=0.
"""
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger("orb")

_SESSIONS_DIR = Path.home() / "Desktop" / "jarvis_sessions"
ENABLED = os.getenv("JARVIS_SESSION_PROGRESS", "1") == "1"
START_AFTER = float(os.getenv("JARVIS_SESSION_PROGRESS_AFTER_S", "90"))
EVERY_S = float(os.getenv("JARVIS_SESSION_PROGRESS_EVERY_S", "240"))
TICK_S = 45.0
# Delegated work only — terminal/file_activity/supervisor rows are ambient.
_WATCH_TYPES = {"cc", "grok-build", "codex"}

_push_fn = None                 # _push_notification, wired by server_win
_state: dict[str, dict] = {}    # name -> pacing state (in-memory)


def wire(push_fn) -> None:
    global _push_fn
    _push_fn = push_fn


def _pid_alive(pid) -> bool | None:
    """True/False when checkable, None when unknown (no pid recorded)."""
    if not pid:
        return None
    try:
        import psutil
        return psutil.pid_exists(int(pid))
    except Exception:
        return None


def _write_record(f: Path, d: dict) -> bool:
    """Atomic-replace persist of a session record. A silently-swallowed write
    failure here is exactly how the 07-03 zombie alerted twice (17:55 + 17:59,
    once per boot): the close never landed, the next boot re-scanned an open
    record. Failures are LOUD now, and the caller retries next tick."""
    try:
        tmp = f.with_name(f.name + ".tmp")
        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
        os.replace(tmp, f)
        return True
    except Exception as e:
        log.warning(f"[session-progress] couldn't persist record {f.name}: {e}")
        return False


async def _tick() -> None:
    now = time.time()
    seen: set[str] = set()
    for f in _SESSIONS_DIR.glob("*.json"):
        if f.name.endswith("_inbox.json"):
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        name = str(d.get("session") or f.stem)
        seen.add(name)
        if d.get("type") not in _WATCH_TYPES or d.get("status") != "running":
            _state.pop(name, None)
            continue
        st = _state.setdefault(name, {"first_seen": now, "last_push": 0.0,
                                      "last_hash": "", "dead_pushed": False})
        msg = str(d.get("message") or d.get("description") or "")[:200]

        # Crash visibility: the process is gone but the record never closed.
        if _pid_alive(d.get("pid")) is False:
            already_alerted = bool(d.get("dead_alerted_at"))
            closed_msg = (msg if "(marked dead" in msg
                          else f"{msg} (marked dead by session watcher)")
            if not st["dead_pushed"] and not already_alerted:
                st["dead_pushed"] = True
                # Persist the close BEFORE pushing (and stamp dead_alerted_at
                # so the dedup survives restarts) — the alert-then-close order
                # let the same zombie push once per boot on 07-03.
                d["status"] = "ended"
                d["dead_alerted_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                d["message"] = closed_msg
                _write_record(f, d)
                # dedupe=True (was False): the per-boot in-memory guard resets on
                # restart, so on heavy-restart days one dead session (e.g.
                # youtube-edit-pipeline, 7 pushes on 2026-07-14) re-alerted every
                # boot. The persistent topic cooldown on `sess-dead-<name>` makes
                # "process gone" fire at most once per session, restart-proof.
                await _push_fn(
                    "Session looks dead",
                    f"{name} — process gone, no finish reported. "
                    f"Last: {(msg or '(none)')[:90]}",
                    "alert", topic=f"sess-dead-{name}", dedupe=True,
                    session=name)
            else:
                # Alert already sent (this boot or an earlier one) but the
                # record is still open — close it quietly, never re-push.
                st["dead_pushed"] = True
                d["status"] = "ended"
                d.setdefault("dead_alerted_at", time.strftime("%Y-%m-%dT%H:%M:%S"))
                d["message"] = closed_msg
                _write_record(f, d)
            continue

        age = now - st["first_seen"]
        h = hashlib.sha1(msg.encode("utf-8", "replace")).hexdigest()[:10]
        due_first = st["last_push"] == 0.0 and age >= START_AFTER
        due_change = (st["last_push"] > 0.0 and h != st["last_hash"]
                      and now - st["last_push"] >= EVERY_S)
        if due_first or due_change:
            st["last_push"], st["last_hash"] = now, h
            mins = int(age // 60)
            body = f"{name} — {msg or 'no status reported yet'}"
            if mins:
                body += f" ({mins} min in)"
            await _push_fn("Still on it", body, "info",
                           topic=f"sess-prog-{name}", dedupe=False,
                           session=name)
    # forget sessions whose files vanished
    for gone in set(_state) - seen:
        _state.pop(gone, None)


async def run_forever() -> None:
    if not ENABLED:
        log.info("[session-progress] disabled (JARVIS_SESSION_PROGRESS=0)")
        return
    if _push_fn is None:
        log.warning("[session-progress] no push fn wired — not running")
        return
    log.info(f"[session-progress] watching {_SESSIONS_DIR} "
             f"(first push after {START_AFTER:.0f}s, then on-change, "
             f">={EVERY_S:.0f}s apart; crash alerts on dead PIDs)")
    while True:
        try:
            await _tick()
        except Exception as e:
            log.warning(f"[session-progress] tick failed: {e}")
        await asyncio.sleep(TICK_S)

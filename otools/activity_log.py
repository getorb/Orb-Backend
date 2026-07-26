"""Session activity log — a persistent, zero-LLM record of everything happening
across all sessions: the live orb conversation AND any tracked background
session (CC sessions, training jobs).

Every entry is a plain structured fact — a tool call started, a tool
returned, something was said, a session's status changed. Nothing in this
module ever calls a model; it only ever records strings that were already
produced for other reasons. This is what lets the app show real history for
a session (scroll back after reconnecting) instead of only whatever a live
WS broadcast happened to catch.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta
from pathlib import Path

_DATA_DIR = Path(__file__).parent.parent / "data"
_LOG_FILE = _DATA_DIR / "session_activity.jsonl"
_MAX_LINES = 5000   # hard cap so the file never grows unbounded
_KEEP_DAYS = 7
_lock = threading.Lock()

# Injected by server_win.py's lifespan: async fn(session, kind, text, extra) -> None.
# Lets every log_activity() call ALSO push a live WS session_event, so any
# same-process caller (tool calls, session lifecycle) is live by construction —
# no separate wiring needed per call site. External processes (a training
# script in a different venv) can't reach this directly; training_watcher.py's
# poll loop is the live-push path for those instead.
_broadcast_fn = None


def wire_broadcast(fn) -> None:
    global _broadcast_fn
    _broadcast_fn = fn


def log_activity(session: str, kind: str, text: str, **extra) -> None:
    """Append one activity entry AND (if wired) push it live over WS.
    Fire-and-forget — never raises, never blocks the caller.

    kind: "tool_start" | "tool_result" | "message" | "lifecycle"
    """
    # Conversational messages carry more meaning per character than a status
    # blurb — give them more room so a real exchange doesn't get chopped
    # mid-thought (this cost real context recovering one earlier tonight).
    # 2026-07-20: bumped 600->4000 for messages so the durable copy the app
    # backfills from (/api/chat/history) matches the full reply sent live over
    # the WS — a backgrounded turn was recovering truncated otherwise.
    cap = 4000 if kind == "message" else 300
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "session": session,
        "kind": kind,
        "text": (text or "")[:cap],
        **{k: v for k, v in extra.items() if v is not None},
    }
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _lock, _LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    if _broadcast_fn:
        try:
            import asyncio
            loop = asyncio.get_running_loop()
            loop.create_task(_broadcast_fn(session, kind, entry["text"], extra))
        except RuntimeError:
            pass  # no running event loop (e.g. a sync script) — persisted, just not live
        except Exception:
            pass


def get_activity(session: str = "", limit: int = 100) -> list[dict]:
    """Most recent activity entries, oldest-first (scrollback order). Filter
    by session name if given; empty session = every session, merged."""
    if not _LOG_FILE.exists():
        return []
    try:
        lines = _LOG_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out: list[dict] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if session and e.get("session") != session:
            continue
        out.append(e)
        if len(out) >= limit:
            break
    out.reverse()
    return out


def trim(keep_days: int = _KEEP_DAYS, max_lines: int = _MAX_LINES) -> None:
    """Trim to the last `keep_days` AND cap total lines. Cheap; call
    occasionally (e.g. from an existing 60s scheduler tick), not per-write."""
    if not _LOG_FILE.exists():
        return
    try:
        lines = _LOG_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    kept = []
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) >= cutoff:
                kept.append(line)
        except Exception:
            continue
    kept = kept[-max_lines:]
    try:
        with _lock:
            _LOG_FILE.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except Exception:
        pass

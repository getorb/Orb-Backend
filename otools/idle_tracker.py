"""Idle tracker — tracks keyboard/mouse activity via pynput.

Runs two daemon listeners (keyboard + mouse) that update a shared timestamp on
any input event. Zero CPU at idle — pynput uses OS hooks, not polling.

Usage anywhere in the codebase:
    from otools.idle_tracker import idle_seconds, is_idle, start_tracking

Call start_tracking() once at startup (server_win.py lifespan). After that,
idle_seconds() is always accurate with no async overhead.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path

log = logging.getLogger("orb.idle")

_DATA_DIR = Path(__file__).parent.parent / "data"
_ACTIVITY_FILE = _DATA_DIR / "last_activity.json"

# Shared state — updated by pynput listener threads, read by anyone.
_last_event: float = time.time()
_lock = threading.Lock()
_started = False


def _touch() -> None:
    global _last_event
    with _lock:
        _last_event = time.time()


def idle_seconds() -> float:
    """Seconds since the last keyboard or mouse event."""
    with _lock:
        return time.time() - _last_event


def is_idle(threshold_seconds: float = 300.0) -> bool:
    return idle_seconds() >= threshold_seconds


def last_seen_iso() -> str:
    with _lock:
        t = _last_event
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


def _flush_loop() -> None:
    """Write last_activity.json every 60 s so other processes can read idle state."""
    while True:
        try:
            _DATA_DIR.mkdir(parents=True, exist_ok=True)
            _ACTIVITY_FILE.write_text(
                json.dumps({
                    "last_seen": last_seen_iso(),
                    "idle_seconds": round(idle_seconds()),
                }),
                encoding="utf-8",
            )
        except Exception:
            pass
        time.sleep(60)


def start_tracking() -> None:
    """Start pynput keyboard + mouse listeners and the flush thread.
    Safe to call multiple times — only starts once."""
    global _started
    if _started:
        return
    _started = True

    try:
        from pynput import keyboard, mouse

        def _on_key(*_):
            _touch()

        def _on_mouse(*_):
            _touch()

        keyboard.Listener(on_press=_on_key, daemon=True).start()
        mouse.Listener(
            on_move=_on_mouse, on_click=_on_mouse, on_scroll=_on_mouse,
            daemon=True,
        ).start()
        log.info("[idle] pynput listeners started")
    except ImportError:
        log.warning("[idle] pynput not installed — idle tracking disabled. Run: pip install pynput")
    except Exception as e:
        log.warning(f"[idle] could not start listeners: {e}")

    threading.Thread(target=_flush_loop, daemon=True, name="idle-flush").start()

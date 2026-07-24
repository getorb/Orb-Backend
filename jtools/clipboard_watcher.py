"""Clipboard watcher — detects clipboard changes and makes them available
to JARVIS for conversation context.

Uses ctypes (zero deps on Windows) to poll every 3 seconds in a daemon thread.
Content is capped at 1000 chars to avoid bloating the context.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("orb.clipboard")

_DATA_DIR     = Path(__file__).parent.parent / "data"
_CLIP_LOG     = _DATA_DIR / "clipboard_log.jsonl"
_MAX_CONTENT  = 1000   # chars stored per entry
_POLL_INTERVAL = 3     # seconds

_last_content: str = ""
_last_hash:    str = ""
_started:      bool = False
_lock = threading.Lock()


def _read_clipboard() -> str:
    """Read current clipboard text via ctypes — no external deps."""
    try:
        user32   = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not user32.OpenClipboard(0):
            return ""
        try:
            CF_UNICODETEXT = 13
            handle = user32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                return ""
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                return ""
            try:
                text = ctypes.wstring_at(ptr)
            finally:
                kernel32.GlobalUnlock(handle)
            return (text or "").strip()
        finally:
            user32.CloseClipboard()
    except Exception:
        return ""


def _log_clip(content: str) -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts":      datetime.now().isoformat(timespec="seconds"),
        "preview": content[:_MAX_CONTENT],
        "length":  len(content),
    }
    with _CLIP_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def _watch_loop() -> None:
    global _last_content, _last_hash
    while True:
        try:
            text = _read_clipboard()
            h = hashlib.md5(text.encode("utf-8", errors="replace")).hexdigest()
            with _lock:
                if h != _last_hash and text:
                    _last_content = text
                    _last_hash    = h
                    _log_clip(text)
                    log.debug(f"[clipboard] changed ({len(text)} chars): {text[:60]!r}")
        except Exception:
            pass
        time.sleep(_POLL_INTERVAL)


def start_watching() -> None:
    global _started
    if _started:
        return
    _started = True
    threading.Thread(target=_watch_loop, daemon=True, name="clipboard-watcher").start()
    log.info("[clipboard] watcher started")


def get_last() -> str:
    with _lock:
        return _last_content


def get_recent(n: int = 5) -> list[dict]:
    if not _CLIP_LOG.exists():
        return []
    lines = _CLIP_LOG.read_text(encoding="utf-8").splitlines()
    out = []
    for line in reversed(lines):
        try:
            out.append(json.loads(line))
            if len(out) >= n:
                break
        except Exception:
            pass
    return out

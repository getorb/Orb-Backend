"""
Tiny persistent memory for JARVIS — conversation history survives restarts so
JARVIS feels like a presence that remembers, not a fresh script every launch.

Stored as JSON Lines (one role/content pair per line) in data/conversation.jsonl.
Each launch loads the last N exchanges into the in-memory conversation so the
new session has continuity with the previous one.
"""

import json
import logging
from pathlib import Path

log = logging.getLogger("jarvis")

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
CONV_FILE = DATA_DIR / "conversation.jsonl"

# How many of the most-recent turns to load on startup. A turn is one
# user message + one assistant message, so 10 means up to 20 messages.
LOAD_LAST_N_TURNS = 10


def load_recent() -> list[dict]:
    """Return the last N turns of conversation history (newest at end)."""
    if not CONV_FILE.exists():
        return []
    try:
        lines = CONV_FILE.read_text(encoding="utf-8").splitlines()
    except Exception as e:
        log.warning(f"[memory] couldn't read history: {e}")
        return []

    msgs: list[dict] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if obj.get("role") in ("user", "assistant") and isinstance(obj.get("content"), str):
                msgs.append({"role": obj["role"], "content": obj["content"]})
        except Exception:
            continue

    # Keep only the last N turns (≈ 2*N messages). Trim from the front.
    if len(msgs) > LOAD_LAST_N_TURNS * 2:
        msgs = msgs[-LOAD_LAST_N_TURNS * 2:]
    return msgs


def append(role: str, content: str):
    """Append one message to the persistent log."""
    if role not in ("user", "assistant"):
        return
    if not content:
        return
    try:
        with CONV_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"role": role, "content": content}, ensure_ascii=False) + "\n")
    except Exception as e:
        log.warning(f"[memory] append failed: {e}")


def clear():
    """Wipe history (e.g., user says 'Jarvis, forget everything')."""
    try:
        if CONV_FILE.exists():
            CONV_FILE.unlink()
    except Exception as e:
        log.warning(f"[memory] clear failed: {e}")

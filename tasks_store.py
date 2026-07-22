"""
Persistent task / working-memory for JARVIS — a weighted to-do list so JARVIS
actually remembers what it's been asked to do, instead of giving context-free
fluff ("operating at optimal parameters"). This is the user's "weighted
priority list."

Stored as JSON Lines in data/tasks.jsonl. Each task:
  {id, text, status: open|done|dropped, priority: 1(high)|2(normal)|3(low),
   created, done_at}

The matcher that turns speech into add/list/complete lives in server_win
(deterministic, before the LLM); this module is just the store + formatting,
so it's importable and unit-testable headless.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

TASKS_FILE = Path(__file__).parent / "data" / "tasks.jsonl"

_PRIORITY_WORD = {1: "high", 2: "normal", 3: "low"}


def _load() -> list[dict]:
    if not TASKS_FILE.exists():
        return []
    out: list[dict] = []
    try:
        for line in TASKS_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
                if isinstance(o, dict) and o.get("text"):
                    out.append(o)
            except Exception:
                continue
    except Exception:
        return []
    return out


def _save(tasks: list[dict]) -> None:
    TASKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    body = "\n".join(json.dumps(t, ensure_ascii=False) for t in tasks)
    TASKS_FILE.write_text(body + ("\n" if body else ""), encoding="utf-8")


def add(text: str, priority: int = 2) -> dict:
    text = text.strip()
    tasks = _load()
    tid = max((t.get("id", 0) for t in tasks), default=0) + 1
    t = {"id": tid, "text": text, "status": "open",
         "priority": int(priority), "created": time.time()}
    tasks.append(t)
    _save(tasks)
    return t


def list_open() -> list[dict]:
    """Open tasks, highest priority first, then oldest first."""
    return sorted(
        [t for t in _load() if t.get("status") == "open"],
        key=lambda t: (t.get("priority", 2), t.get("created", 0)),
    )


def complete(query: str) -> dict | None:
    """Mark the first open task matching `query` (substring, either way) done."""
    q = query.lower().strip()
    tasks = _load()
    for t in tasks:
        if t.get("status") == "open":
            tl = t["text"].lower()
            if q and (q in tl or tl in q):
                t["status"] = "done"
                t["done_at"] = time.time()
                _save(tasks)
                return t
    return None


def clear_open() -> int:
    tasks = _load()
    n = 0
    for t in tasks:
        if t.get("status") == "open":
            t["status"] = "dropped"
            n += 1
    _save(tasks)
    return n


def spoken_list(addr: str = "sir") -> str:
    """A natural spoken rendering of the open list, priority-ordered."""
    op = list_open()
    if not op:
        return f"Your list is clear, {addr}."
    items = [t["text"] for t in op]
    if len(items) == 1:
        return f"One thing on your list, {addr}: {items[0]}."
    head = "; ".join(items[:-1])
    return f"{len(items)} things on your list, {addr}: {head}; and {items[-1]}."


def summary_for_prompt() -> str:
    """Compact open-task line injected into the LLM system prompt so JARVIS is
    aware of what it's tracking. Empty string if nothing open."""
    op = list_open()
    if not op:
        return ""
    return "Open tasks on the user's list: " + "; ".join(t["text"] for t in op[:8])

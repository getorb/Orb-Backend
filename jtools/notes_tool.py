"""Notes tool — persistent JSONL-backed notes/reminders."""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from tool_registry import tool

log = logging.getLogger("orb.tools.notes")

NOTES_PATH = Path(__file__).parent.parent / "data" / "notes.jsonl"
NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)


def _load() -> list[dict]:
    if not NOTES_PATH.exists():
        return []
    notes: list[dict] = []
    with NOTES_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                notes.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return notes


def _save_all(notes: list[dict]) -> None:
    with NOTES_PATH.open("w", encoding="utf-8") as f:
        for n in notes:
            f.write(json.dumps(n) + "\n")


def recent_notes(limit: int = 8) -> list[dict]:
    """Newest-last slice of the most recent notes (for the proactive synthesis)."""
    return _load()[-max(1, limit):]


@tool(
    name="add_note",
    description=(
        "Save a note or reminder for the user. Use when they say 'remember "
        "that...', 'note that...', 'remind me that...', 'jot down...', 'save "
        "this idea'. The note is persisted across sessions."
    ),
    parameters={
        "text": {
            "type": "string",
            "description": "The note content as the user expressed it.",
        },
        "tag": {
            "type": "string",
            "description": "Optional one-word category (e.g. 'idea', 'todo', 'reminder').",
        },
    },
    required=["text"],
)
async def add_note(text: str, tag: str = "") -> str:
    text = text.strip()
    if not text:
        return "There's nothing to note."
    note = {
        "id": int(time.time() * 1000),
        "ts": datetime.now().isoformat(timespec="seconds"),
        "text": text,
        "tag": (tag or "").strip().lower()[:20],
    }
    with NOTES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(note) + "\n")
    return f"Noted. {len(_load())} notes on file."


@tool(
    name="list_notes",
    description=(
        "Read back the user's saved notes. Use when they ask 'what notes do I "
        "have', 'what was that thing I asked you to remember', 'read my notes', "
        "or 'what's on my list'."
    ),
    parameters={
        "tag": {
            "type": "string",
            "description": "Optional filter — only return notes with this tag.",
        },
        "limit": {
            "type": "integer",
            "description": "How many notes to return, newest first (default 5, max 20).",
        },
    },
    required=[],
)
async def list_notes(tag: str = "", limit: int = 5) -> str:
    notes = _load()
    if tag:
        tag_l = tag.strip().lower()
        notes = [n for n in notes if n.get("tag") == tag_l]
    if not notes:
        return f"No notes{(' tagged ' + tag) if tag else ''}." if tag else "You haven't saved any notes yet."
    limit = max(1, min(int(limit or 5), 20))
    recent = notes[-limit:][::-1]
    lines = [f"{n['text']}" for n in recent]
    label = f"Your last {len(lines)} notes" if not tag else f"Your last {len(lines)} {tag} notes"
    return f"{label}: " + " | ".join(lines) + "."


@tool(
    name="search_notes",
    description=(
        "Find notes containing a specific word or phrase. Use when the user "
        "asks 'find my note about X', 'did I write down anything about Y'."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "Text to search for. Case-insensitive substring match.",
        },
    },
    required=["query"],
)
async def search_notes(query: str) -> str:
    q = query.strip().lower()
    if not q:
        return "What should I search for?"
    notes = _load()
    matches = [n for n in notes if q in n.get("text", "").lower()]
    if not matches:
        return f"Nothing in your notes matches '{query}'."
    return f"Found {len(matches)}: " + " | ".join(n["text"] for n in matches[-5:][::-1]) + "."

"""Self-modification preferences — Orb reads and writes its own behavior settings.

When the user says "don't notify me about X" or "always tell me about Y" or
"change your personality to be more concise", Orb calls set_preference and
it takes effect immediately on the next turn (injected into system prompt).

Preferences are stored in data/orb_prefs.json.
"""

import json
from datetime import datetime
from pathlib import Path

from tool_registry import tool

_PREFS_PATH = Path(__file__).parent.parent / "data" / "orb_prefs.json"
_PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)


def load_preferences() -> dict:
    if not _PREFS_PATH.exists():
        return {}
    try:
        return json.loads(_PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_preferences(prefs: dict) -> None:
    _PREFS_PATH.write_text(json.dumps(prefs, indent=2, ensure_ascii=False), encoding="utf-8")


def preferences_system_block() -> str:
    """Return a system-prompt block summarizing active preferences. Called each turn."""
    prefs = load_preferences()
    if not prefs:
        return ""
    lines = ["USER PREFERENCES (self-modified, respect these):"]
    for key, entry in prefs.items():
        val = entry["value"] if isinstance(entry, dict) else entry
        lines.append(f"  - {key}: {val}")
    return "\n".join(lines)


@tool(
    name="set_preference",
    description=(
        "Save a behavioral preference or rule for yourself. Call this when the user tells "
        "you to change how you behave — 'don't notify me about emails', 'be more concise', "
        "'always tell me when the GPU is hot', 'stop reminding me about X'. "
        "The preference is saved permanently and takes effect immediately."
    ),
    parameters={
        "key": {
            "type": "string",
            "description": "Short identifier for this preference (e.g. 'email_notifications', 'response_style')",
        },
        "value": {
            "type": "string",
            "description": "The rule or setting (e.g. 'disabled', 'only urgent', 'very brief responses')",
        },
        "reason": {
            "type": "string",
            "description": "Why this preference was set (what the user said)",
        },
    },
    required=["key", "value"],
)
async def set_preference(key: str, value: str, reason: str = "") -> str:
    prefs = load_preferences()
    prefs[key] = {
        "value": value,
        "reason": reason,
        "set_at": datetime.now().isoformat(timespec="seconds"),
    }
    _save_preferences(prefs)
    return f"Got it — I've updated '{key}' to '{value}'. This takes effect immediately and persists."


@tool(
    name="list_preferences",
    description="Show all current behavioral preferences and self-modifications.",
    parameters={},
    required=[],
)
async def list_preferences() -> str:
    prefs = load_preferences()
    if not prefs:
        return "No preferences set yet."
    lines = []
    for key, entry in prefs.items():
        if isinstance(entry, dict):
            lines.append(f"{key}: {entry['value']}  (set: {entry.get('set_at', '?')}  reason: {entry.get('reason', '')})")
        else:
            lines.append(f"{key}: {entry}")
    return "\n".join(lines)


@tool(
    name="delete_preference",
    description="Remove a saved preference. Use when the user says 'forget that rule' or 'undo that setting'.",
    parameters={
        "key": {"type": "string", "description": "The preference key to remove."},
    },
    required=["key"],
)
async def delete_preference(key: str) -> str:
    prefs = load_preferences()
    if key not in prefs:
        return f"No preference named '{key}' found."
    del prefs[key]
    _save_preferences(prefs)
    return f"Preference '{key}' removed."

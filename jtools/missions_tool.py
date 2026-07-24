"""Missions tool — the user's current week-scale goals.

Distinct from notes (facts/ideas to remember) and tasks (short-lived to-dos):
a mission is what ninjahawk is actually driving at right now ("submit the app",
"land a job") — few, slow-moving, deadline-aware. The proactive synthesis reads
these as its interpretive frame, so every other data source gets weighed
against what he's actually trying to get done, and get_conversation_context()
injects them into every live conversation.
"""

import json
import logging
import time
from datetime import date
from pathlib import Path

from tool_registry import tool

log = logging.getLogger("orb.tools.missions")

MISSIONS_PATH = Path(__file__).parent.parent / "data" / "missions.json"
MISSIONS_PATH.parent.mkdir(parents=True, exist_ok=True)

# Surfacing mode (2026-07-20 — stop nagging the user's todo list):
#   SILENT       — background context only. Injected into conversations as the
#                  interpretive frame, but a stale SILENT mission NEVER generates
#                  a proactive nudge, note, or push. This is the DEFAULT.
#   ACTIVE_DRIVE — the mind/synthesis MAY surface progress/nudges on this one.
# Missions default to SILENT when the field is absent, so migration is automatic
# and the safe direction (silence) is the fallback.
SURFACE_SILENT = "silent"
SURFACE_ACTIVE_DRIVE = "active-drive"


def mission_surface(m: dict) -> str:
    s = str(m.get("surface", SURFACE_SILENT)).strip().lower()
    return SURFACE_ACTIVE_DRIVE if s == SURFACE_ACTIVE_DRIVE else SURFACE_SILENT


def _load() -> list[dict]:
    if not MISSIONS_PATH.exists():
        return []
    try:
        data = json.loads(MISSIONS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(missions: list[dict]) -> None:
    MISSIONS_PATH.write_text(
        json.dumps(missions, indent=2, ensure_ascii=False), encoding="utf-8")


def active_missions() -> list[dict]:
    """Non-done missions, soonest deadline first (no-deadline ones last).
    Includes BOTH silent and active-drive — used for conversation context, where
    all missions are legitimate background frame."""
    acts = [m for m in _load() if not m.get("done")]
    acts.sort(key=lambda m: (m.get("deadline") or "").strip() or "9999-12-31")
    return acts


def driving_missions() -> list[dict]:
    """Only ACTIVE-DRIVE missions — the ONLY ones the proactive layers may
    surface (nudge/note/push) about. Silent missions never reach here."""
    return [m for m in active_missions() if mission_surface(m) == SURFACE_ACTIVE_DRIVE]


def _days_left(deadline: str) -> int | None:
    try:
        return (date.fromisoformat(deadline[:10]) - date.today()).days
    except Exception:
        return None


def render_line(m: dict, show_surface: bool = False) -> str:
    """One human line for prompts/blocks: text, deadline countdown, the why.
    show_surface=True prefixes the surfacing mode so the proactive blocks can
    make the silent/active-drive distinction visible to the model."""
    bits = []
    if show_surface:
        bits.append("[ACTIVE-DRIVE]" if mission_surface(m) == SURFACE_ACTIVE_DRIVE else "[silent]")
    bits.append(m.get("text", "").strip())
    dl = (m.get("deadline") or "").strip()
    if dl:
        days = _days_left(dl)
        if days is None:
            bits.append(f"(deadline: {dl})")
        elif days < 0:
            bits.append(f"(deadline {dl} — {-days}d OVERDUE)")
        elif days == 0:
            bits.append("(deadline: TODAY)")
        else:
            bits.append(f"(deadline {dl} — {days}d left)")
    why = (m.get("why") or "").strip()
    if why:
        bits.append(f"— {why[:160]}")
    return " ".join(b for b in bits if b)


@tool(
    name="add_mission",
    description=(
        "Log a MISSION — a current week-scale goal the user is driving toward "
        "(e.g. 'submit the app by Friday', 'land a summer job'). Use when they "
        "declare a goal, a deadline, or something they're 'working toward' — "
        "NOT for small reminders (that's add_note). Missions are injected into "
        "every conversation and every proactive review as context."
    ),
    parameters={
        "text": {
            "type": "string",
            "description": "The goal, stated plainly.",
        },
        "deadline": {
            "type": "string",
            "description": "Optional deadline as YYYY-MM-DD (preferred) or free text.",
        },
        "why": {
            "type": "string",
            "description": "Optional stakes/context — why it matters, known blockers.",
        },
    },
    required=["text"],
)
async def add_mission(text: str, deadline: str = "", why: str = "") -> str:
    text = text.strip()
    if not text:
        return "There's no mission to log."
    missions = _load()
    for m in missions:
        if not m.get("done") and m.get("text", "").strip().lower() == text.lower():
            return f"That mission's already on the board: {render_line(m)}"
    missions.append({
        "id": str(int(time.time() * 1000)),
        "text": text,
        "deadline": deadline.strip(),
        "why": why.strip(),
        "added": date.today().isoformat(),
        "done": False,
    })
    _save(missions)
    n = len([m for m in missions if not m.get("done")])
    return f"Mission logged. {n} active."


@tool(
    name="complete_mission",
    description=(
        "Mark a mission accomplished (or abandoned). Use when the user says "
        "they finished, shipped, landed, or dropped one of their goals. Match "
        "by a few words from the mission text."
    ),
    parameters={
        "query": {
            "type": "string",
            "description": "A few words identifying which mission (substring match).",
        },
    },
    required=["query"],
)
async def complete_mission(query: str) -> str:
    q = query.strip().lower()
    if not q:
        return "Which mission?"
    missions = _load()
    matches = [m for m in missions if not m.get("done") and q in m.get("text", "").lower()]
    if not matches:
        acts = active_missions()
        listing = "; ".join(m.get("text", "") for m in acts) or "none on the board"
        return f"No active mission matches '{query}'. Currently: {listing}."
    if len(matches) > 1:
        return ("That matches more than one: "
                + "; ".join(m.get("text", "") for m in matches) + ". Which one?")
    matches[0]["done"] = True
    matches[0]["done_at"] = date.today().isoformat()
    _save(missions)
    n = len([m for m in missions if not m.get("done")])
    return f"Mission accomplished: {matches[0]['text']}. {n} still active."


@tool(
    name="list_missions",
    description=(
        "Read back the user's active missions (week-scale goals with deadlines). "
        "Use when they ask 'what am I working toward', 'what are my missions', "
        "'what's on the board', or want a goals status check."
    ),
    parameters={},
    required=[],
)
async def list_missions() -> str:
    acts = active_missions()
    if not acts:
        return "No missions on the board. Declare one and I'll hold you to it."
    lines = "; ".join(render_line(m, show_surface=True) for m in acts)
    return f"{len(acts)} active mission{'s' if len(acts) != 1 else ''}: {lines}."


def set_surface(query: str, mode: str) -> str:
    """Set a mission's surfacing mode. mode: 'silent' | 'active-drive'.
    Programmatic entry point (also used by migration)."""
    q = (query or "").strip().lower()
    target = SURFACE_ACTIVE_DRIVE if str(mode).strip().lower() in (
        SURFACE_ACTIVE_DRIVE, "active", "drive", "active_drive") else SURFACE_SILENT
    missions = _load()
    matches = [m for m in missions if not m.get("done") and q in m.get("text", "").lower()]
    if not matches:
        return f"No active mission matches '{query}'."
    if len(matches) > 1:
        return ("That matches more than one: "
                + "; ".join(m.get("text", "") for m in matches) + ". Be more specific.")
    matches[0]["surface"] = target
    _save(missions)
    return f"Mission set to {target}: {matches[0]['text']}"


@tool(
    name="set_mission_mode",
    description=(
        "Set whether a mission is SILENT (background context only — never generates "
        "a proactive nudge/note/push, the default) or ACTIVE-DRIVE (JARVIS may "
        "proactively surface progress and nudges on it). Use 'active-drive' only "
        "when the user explicitly wants JARVIS driving a goal; use 'silent' to stop "
        "it nagging about one. Match by a few words from the mission text."
    ),
    parameters={
        "query": {"type": "string", "description": "A few words identifying the mission."},
        "mode": {"type": "string", "description": "'silent' or 'active-drive'."},
    },
    required=["query", "mode"],
)
async def set_mission_mode(query: str, mode: str) -> str:
    return set_surface(query, mode)

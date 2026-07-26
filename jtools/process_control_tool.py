"""Process control tools — stop, pause, or restart any session from Orb."""

import json
import os
import signal
import sys
from pathlib import Path
from datetime import datetime

from tool_registry import tool

_SESSIONS_DIR = Path.home() / "Desktop" / "orb_sessions"

STOP_WORDS = ("stop", "cancel", "abort", "kill", "terminate")
PAUSE_WORDS = ("pause", "hold", "wait", "suspend")
RESUME_WORDS = ("resume", "continue", "unpause", "go")


def _read_session(name: str) -> dict | None:
    path = _SESSIONS_DIR / f"{name}.json"
    # Fuzzy match if exact name not found
    if not path.exists():
        matches = [f for f in _SESSIONS_DIR.glob("*.json")
                   if "_inbox" not in f.name and name.lower() in f.stem.lower()]
        if matches:
            path = matches[0]
        else:
            return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def _write_inbox(name: str, message: str) -> None:
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    inbox = _SESSIONS_DIR / f"{name}_inbox.json"
    inbox.write_text(json.dumps({
        "id": os.urandom(4).hex(),
        "from": "orb",
        "message": message,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))


@tool(
    name="stop_session",
    description=(
        "Stop a running background session or process — training jobs, Claude Code sessions, "
        "build tasks, etc. Sends a stop signal via inbox (graceful — the process stops at its "
        "next checkpoint) and optionally a SIGTERM (immediate). Use when the user says "
        "'stop myproject', 'kill the training job', 'cancel the build', etc."
    ),
    parameters={
        "session_name": {
            "type": "string",
            "description": "Name of the session to stop (from check_sessions).",
        },
        "force": {
            "type": "boolean",
            "description": "Send SIGTERM immediately in addition to the inbox stop message. Default false (graceful only).",
        },
    },
    required=["session_name"],
)
async def stop_session(session_name: str, force: bool = False) -> str:
    from jtools.activity_log import log_activity
    data = _read_session(session_name)
    if not data:
        return f"No session '{session_name}' found. Use check_sessions to list running sessions."

    actual_name = data.get("session", session_name)
    _write_inbox(actual_name, "stop")
    log_activity(actual_name, "lifecycle", f"Stop requested (force={force})")

    pid = data.get("pid")
    killed = False
    if force and pid:
        try:
            if sys.platform == "win32":
                os.kill(int(pid), signal.SIGTERM)
            else:
                os.kill(int(pid), signal.SIGTERM)
            killed = True
        except (ProcessLookupError, PermissionError, OSError):
            killed = False

    if force and killed:
        return f"Stop signal + SIGTERM sent to '{actual_name}' (PID {pid}). It should stop immediately."
    return (f"Stop message sent to '{actual_name}'. "
            f"It will stop at its next checkpoint (when it calls check_inbox). "
            f"{'PID ' + str(pid) + ' not reachable for SIGTERM.' if force and pid and not killed else ''}")


@tool(
    name="pause_session",
    description=(
        "Pause a running background session at its next checkpoint. The session will hold "
        "until it gets a resume message. Use for 'pause the training', 'hold myproject', etc."
    ),
    parameters={
        "session_name": {"type": "string", "description": "Name of the session to pause."},
    },
    required=["session_name"],
)
async def pause_session(session_name: str) -> str:
    from jtools.activity_log import log_activity
    data = _read_session(session_name)
    if not data:
        return f"No session '{session_name}' found."
    actual_name = data.get("session", session_name)
    _write_inbox(actual_name, "pause")
    log_activity(actual_name, "lifecycle", "Pause requested")
    return f"Pause message sent to '{actual_name}'. It will hold at its next checkpoint."


@tool(
    name="resume_session",
    description=(
        "Resume a paused session, or send a custom instruction to a running one. "
        "Use for 'resume myproject', 'tell the training job to continue', 'unpause the build'."
    ),
    parameters={
        "session_name": {"type": "string", "description": "Name of the session."},
        "message": {"type": "string", "description": "Instruction to send (default: 'resume')."},
    },
    required=["session_name"],
)
async def resume_session(session_name: str, message: str = "resume") -> str:
    from jtools.activity_log import log_activity
    data = _read_session(session_name)
    if not data:
        return f"No session '{session_name}' found."
    actual_name = data.get("session", session_name)
    _write_inbox(actual_name, message)
    log_activity(actual_name, "lifecycle", f"Resume/instruction: {message}")
    return f"Message sent to '{actual_name}': {message}"

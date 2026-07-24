"""File operations — reads scoped to Desktop, writes scoped to Desktop\\JARVIS_Files."""

import logging
from pathlib import Path

from tool_registry import tool

log = logging.getLogger("orb.tools.files")

DESKTOP = Path.home() / "Desktop"
WRITE_SANDBOX = DESKTOP / "JARVIS_Files"
WRITE_SANDBOX.mkdir(parents=True, exist_ok=True)

MAX_READ_BYTES = 50_000
MAX_WRITE_BYTES = 200_000


def _resolve_read(name: str) -> Path:
    """Resolve a path for reading — must stay within Desktop (no traversal above it)."""
    if not name or not name.strip():
        raise ValueError("empty filename")
    p = Path(name.strip())
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("path must be relative and cannot traverse above Desktop")
    target = (DESKTOP / p).resolve()
    if DESKTOP.resolve() not in target.parents and target != DESKTOP.resolve():
        raise ValueError("path escapes Desktop")
    return target


def _resolve_write(name: str) -> Path:
    """Resolve a path for writing — stays within JARVIS_Files."""
    if not name or not name.strip():
        raise ValueError("empty filename")
    p = Path(name.strip())
    if p.is_absolute() or ".." in p.parts:
        raise ValueError("path must be relative and stay inside JARVIS_Files")
    target = (WRITE_SANDBOX / p).resolve()
    if WRITE_SANDBOX.resolve() not in target.parents and target != WRITE_SANDBOX.resolve():
        raise ValueError("path escapes JARVIS_Files")
    return target


@tool(
    name="read_file",
    description=(
        "Read a text file from the user's Desktop (or any subfolder on it, including "
        "JARVIS_Files and JARVIS_Output). Use when the user asks 'read me X', "
        "'what's in X', 'what did Claude Code build', 'show me the poem', etc. "
        "Pass just a filename to search the Desktop root first."
    ),
    parameters={
        "name": {
            "type": "string",
            "description": (
                "Filename or relative path on the Desktop "
                "(e.g. 'note.txt', 'JARVIS_Output/poem.txt', 'JARVIS_Files/ideas.md')."
            ),
        },
    },
    required=["name"],
)
async def read_file(name: str) -> str:
    try:
        p = _resolve_read(name)
    except ValueError as e:
        return f"Can't access that file — {e}."
    if not p.exists():
        # Try common subfolders automatically before giving up.
        for subdir in ("JARVIS_Output", "JARVIS_Files"):
            alt = _resolve_read(subdir + "/" + name) if ".." not in name else None
            if alt and alt.exists():
                p = alt
                break
        else:
            return f"No file called '{name}' found on the Desktop."
    if not p.is_file():
        return f"'{name}' is a folder, not a file."
    size = p.stat().st_size
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.error(f"[read_file] {e}")
        return f"Couldn't read '{name}': {e}."
    if size > MAX_READ_BYTES:
        text = text[:MAX_READ_BYTES] + f"\n\n[truncated — file is {size:,} bytes]"
    return text or f"'{name}' is empty."


@tool(
    name="write_file",
    description=(
        "Write text to a file in the JARVIS_Files folder on the Desktop. Overwrites "
        "if it exists. Use when the user says 'save this as X', 'write a note', "
        "'create a file named Y'."
    ),
    parameters={
        "name": {
            "type": "string",
            "description": "Filename (e.g. 'idea.txt', 'subfolder/log.md'). Path stays inside JARVIS_Files.",
        },
        "content": {
            "type": "string",
            "description": "Text to write. Replaces existing file content.",
        },
    },
    required=["name", "content"],
)
async def write_file(name: str, content: str) -> str:
    if len(content) > MAX_WRITE_BYTES:
        return f"That's too large — limit is {MAX_WRITE_BYTES:,} characters."
    try:
        p = _resolve_write(name)
    except ValueError as e:
        return f"Can't write there — {e}."
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return f"Saved {len(content):,} characters to {name}."
    except Exception as e:
        log.error(f"[write_file] {e}")
        return f"Couldn't write '{name}': {e}."


@tool(
    name="list_files",
    description=(
        "List files on the user's Desktop (or a specific subfolder on it). "
        "Use when the user asks 'what's on my desktop', 'what files do I have', "
        "'what did Claude Code build', 'list JARVIS_Output', etc."
    ),
    parameters={
        "subfolder": {
            "type": "string",
            "description": (
                "Subfolder to list, relative to Desktop (e.g. 'JARVIS_Output', "
                "'JARVIS_Files'). Leave empty to list the Desktop root."
            ),
        },
    },
    required=[],
)
async def list_files(subfolder: str = "") -> str:
    try:
        target = _resolve_read(subfolder) if subfolder else DESKTOP
    except ValueError as e:
        return f"Can't list that — {e}."
    if not target.exists() or not target.is_dir():
        return f"No folder called '{subfolder}' on the Desktop."
    items = sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    if not items:
        return "That folder is empty."
    listing = []
    for item in items[:40]:
        if item.is_dir():
            listing.append(f"{item.name}/")
        else:
            listing.append(f"{item.name} ({item.stat().st_size:,} bytes)")
    extra = f" and {len(items) - 40} more" if len(items) > 40 else ""
    return ", ".join(listing) + extra + "."

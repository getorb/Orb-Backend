"""Orb's eyes and hands into its own system — direct, in-process file access
plus self-knowledge and self-correction, so Orb can SEE its backend / logs /
project and CHANGE its own state without spawning a slow Claude Code agent.

Background (2026-07-06): Orb kept failing to read its own backend logs and the
"10 errors", and kept *promising* actions it never took, because its only file
reach outside the Desktop sandbox was run_claude_code — a heavyweight `claude -p`
spawn that hits the 120s cap and returns "still running". (Orb blamed a "local
Qwen router"; the real cause is the wrong tool for the job — the backend's
claude -p reaches real Claude fine, there are no router vars in .env.) These
tools remove the choke point: pure-Python reads/writes, millisecond-fast, and
they work identically from the resident brain process or the grok/codex stdio
process because they only touch files.

Safety boundary (honors the never-destructive-on-autonomous-surfaces rule):
  • READS are broad — the project tree, ~/.orb_brain, the Desktop, and Orb's own
    Claude Code session logs — but NEVER hand back a credential file (.env/.p8/…).
  • WRITES are scoped to Orb's OWN state — the data/ dir, ~/.orb_brain, and
    Desktop/JARVIS_Files — and refuse source code (*.py) and engine-owned files.
    So an autonomous turn can edit its knowledge, notes and situation model, but
    cannot rewrite the running backend's code out from under itself. Source-level
    self-modification stays behind run_claude_code + the restart tool.
"""

import logging
import os
import time
from pathlib import Path

from tool_registry import tool

log = logging.getLogger("jarvis.tools.introspect")

PROJECT = Path(__file__).resolve().parent.parent          # Desktop/Jarvis-AI
DATA = PROJECT / "data"
DESKTOP = Path.home() / "Desktop"
ORB_BRAIN = Path.home() / ".orb_brain"
CC_LOGS = Path.home() / ".claude" / "projects"

_READ_ROOTS = [PROJECT, DESKTOP, ORB_BRAIN, CC_LOGS]
_WRITE_ROOTS = [DATA, ORB_BRAIN, DESKTOP / "JARVIS_Files"]

# Engine-owned files that have their own proper tools — never let a raw write
# clobber them (external edits to active_plan.json don't stick anyway; the rest
# would corrupt live state).
_ENGINE_OWNED = {
    "active_plan.json", "mind_state.json", "brain_state.json",
    "notify_topics.json", "notification_log.jsonl", "seed_facts.json",
}

MAX_READ = 40_000
MAX_WRITE = 200_000
SITUATION_CAP = 6000
ABOUT_CAP = 60_000


def _is_secret(p: Path) -> bool:
    n = p.name.lower()
    return (n == ".env" or n.startswith(".env.")
            or p.suffix.lower() in {".p8", ".p12", ".key", ".pem", ".pfx", ".cer", ".crt"})


def _within(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _resolve_read(path: str) -> Path:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("empty path")
    p = Path(raw)
    if not p.is_absolute():
        # Bare/relative name: prefer the project root, then the Desktop.
        cand = PROJECT / p
        if not cand.exists():
            alt = DESKTOP / p
            if alt.exists():
                cand = alt
        p = cand
    p = p.resolve()
    if not any(_within(p, r) for r in _READ_ROOTS):
        raise ValueError("outside your readable area (project, Desktop, ~/.orb_brain, CC logs)")
    if _is_secret(p):
        raise ValueError("that file holds credentials — off limits")
    return p


def _resolve_write(path: str) -> Path:
    raw = (path or "").strip()
    if not raw:
        raise ValueError("empty path")
    p = Path(raw)
    if not p.is_absolute():
        p = DATA / p                      # relative writes land in the data dir
    p = p.resolve()
    if not any(_within(p, r) for r in _WRITE_ROOTS):
        raise ValueError("writes are limited to your own state: the data folder, "
                         "~/.orb_brain, or Desktop/JARVIS_Files")
    if _is_secret(p):
        raise ValueError("can't write credential files")
    if p.suffix.lower() == ".py":
        raise ValueError("can't edit source code here — use run_claude_code for that")
    if p.name in _ENGINE_OWNED:
        raise ValueError(f"{p.name} is engine-managed — use the proper tool "
                         "(scheduler / switch_brain / mind), not a raw write")
    return p


def _atomic_write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


@tool(
    name="read_system_file",
    description=(
        "Read any file in your own world directly and instantly — backend code, "
        "logs, config, data files, project files, your ~/.orb_brain, your own Claude "
        "Code session logs. Use this to actually SEE your system instead of guessing. "
        "Pass a bare name (resolved against the backend project first) or a full path. "
        "Set tail_lines to read just the end of a big log. Credential files are off limits."
    ),
    parameters={
        "path": {"type": "string", "description": "Filename or path (e.g. 'server_win.py', "
                 "'data/situation.md', 'backend_err.log', or an absolute path under ~/.orb_brain)."},
        "tail_lines": {"type": "integer", "description": "If >0, return only the last N lines (for logs)."},
    },
    required=["path"],
)
async def read_system_file(path: str, tail_lines: int = 0) -> str:
    try:
        p = _resolve_read(path)
    except ValueError as e:
        return f"Can't read that — {e}."
    if not p.exists():
        return f"No file at '{path}' (looked in the project, Desktop, and ~/.orb_brain)."
    if not p.is_file():
        return f"'{path}' is a folder — use list_system_dir."
    try:
        txt = p.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        log.error(f"[read_system_file] {e}")
        return f"Couldn't read '{path}': {e}."
    if tail_lines and int(tail_lines) > 0:
        txt = "\n".join(txt.splitlines()[-int(tail_lines):])
        return txt[-MAX_READ:] if len(txt) > MAX_READ else (txt or "(empty file)")
    if len(txt) > MAX_READ:
        return txt[:MAX_READ] + f"\n\n[truncated — {p.stat().st_size:,} bytes total; use tail_lines]"
    return txt or "(empty file)"


@tool(
    name="list_system_dir",
    description=(
        "List a folder in your own world so you can find files before reading them. "
        "Defaults to the backend project root. Use for 'what's in the data folder', "
        "'what session logs do I have', etc."
    ),
    parameters={
        "path": {"type": "string", "description": "Folder path (bare or absolute). Empty = the backend project root."},
    },
    required=[],
)
async def list_system_dir(path: str = "") -> str:
    raw = (path or "").strip()
    try:
        target = PROJECT if not raw else _resolve_read(raw)
    except ValueError as e:
        return f"Can't list that — {e}."
    if not target.exists() or not target.is_dir():
        return f"No folder at '{path or '(project root)'}'."
    items = sorted(target.iterdir(), key=lambda x: (x.is_file(), x.name.lower()))
    if not items:
        return "That folder is empty."
    out = []
    for it in items[:60]:
        if it.is_dir():
            out.append(f"{it.name}/")
        elif not _is_secret(it):
            try:
                out.append(f"{it.name} ({it.stat().st_size:,}b)")
            except Exception:
                out.append(it.name)
        else:
            out.append(f"{it.name} (credential — hidden)")
    extra = f" +{len(items) - 60} more" if len(items) > 60 else ""
    return ", ".join(out) + extra


@tool(
    name="read_backend_errors",
    description=(
        "Read the tail of your own backend error and output logs — the fastest way to "
        "answer 'what were those errors', 'is anything broken', 'why did X fail'. "
        "This is the tool to reach for whenever a notification mentions backend errors."
    ),
    parameters={
        "lines": {"type": "integer", "description": "How many trailing lines per log (default 40)."},
    },
    required=[],
)
async def read_backend_errors(lines: int = 40) -> str:
    n = int(lines) if lines else 40
    out = []
    for name in ("backend_err.log", "backend.log"):
        f = PROJECT / name
        if f.exists():
            tail = f.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
            out.append(f"=== {name} (last {len(tail)} lines) ===\n" + "\n".join(tail))
    return ("\n\n".join(out) or "No backend logs found (backend may be running fresh).")[:MAX_READ]


@tool(
    name="write_self_file",
    description=(
        "Write a file into your OWN state — the data folder, ~/.orb_brain, or "
        "Desktop/JARVIS_Files. This is real self-modification of your notes, knowledge, "
        "and working files (no Claude Code spawn, no timeout). You cannot write source "
        "code (*.py) or engine-managed files here — use run_claude_code for code changes."
    ),
    parameters={
        "path": {"type": "string", "description": "Filename/path. Relative names land in the data folder "
                 "(e.g. 'interests.md', 'orb_status.md')."},
        "content": {"type": "string", "description": "Full text to write (replaces the file)."},
    },
    required=["path", "content"],
)
async def write_self_file(path: str, content: str) -> str:
    if len(content) > MAX_WRITE:
        return f"Too large — limit is {MAX_WRITE:,} characters."
    try:
        p = _resolve_write(path)
    except ValueError as e:
        return f"Can't write there — {e}."
    try:
        _atomic_write(p, content)
        return f"Wrote {len(content):,} characters to {p.name} (in {p.parent.name}/)."
    except Exception as e:
        log.error(f"[write_self_file] {e}")
        return f"Couldn't write '{path}': {e}."


@tool(
    name="remember_about_me",
    description=(
        "Save something durable you learned about the user into your growing profile of them "
        "(data/about_me.md) — their projects, how they work, what they care about, what they want "
        "from you, what to surface proactively. This is how you actually get to KNOW them "
        "instead of being re-reminded. It's injected into every conversation. Use a clear "
        "section like 'Projects', 'Work', 'How to be present', 'Interests'."
    ),
    parameters={
        "section": {"type": "string", "description": "Which part of his profile (e.g. 'Interests', 'Projects')."},
        "text": {"type": "string", "description": "The fact/observation, one self-contained line."},
        "replace": {"type": "boolean", "description": "Replace the whole section instead of appending (default false)."},
    },
    required=["section", "text"],
)
async def remember_about_me(section: str, text: str, replace: bool = False) -> str:
    f = DATA / "about_me.md"
    entry = (text or "").strip()
    if not entry:
        return "Give me something to remember."
    doc = f.read_text(encoding="utf-8", errors="replace") if f.exists() else "# About me\n"
    header = f"## {(section or 'Notes').strip()}"
    lines = doc.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip().lower() == header.lower()), None)
    if idx is None:
        doc = doc.rstrip() + f"\n\n{header}\n- {entry}\n"
    else:
        end = next((j for j in range(idx + 1, len(lines)) if lines[j].startswith("## ")), len(lines))
        if replace:
            newsec = [header, f"- {entry}"]
        else:
            newsec = lines[idx:end]
            while newsec and not newsec[-1].strip():
                newsec.pop()
            newsec.append(f"- {entry}")
        lines = lines[:idx] + newsec + [""] + lines[end:]
        doc = "\n".join(lines)
    if len(doc) > ABOUT_CAP:
        return "Your profile of him is full — trim about_me.md first."
    try:
        _atomic_write(f, doc)
    except Exception as e:
        return f"Couldn't save that: {e}."
    return f"Noted under '{section}': {entry[:80]}"


@tool(
    name="update_situation",
    description=(
        "Rewrite your mind's situation model (data/situation.md) — the running picture of "
        "Now / Active threads / Expected next / Open questions that drives your proactivity. "
        "Use this to CORRECT it when it's stale or wrong (e.g. a deadline that already passed, "
        "a thread that's actually resolved). This is the fix for things silently rotting: you "
        "can keep your own model honest. Your between-conversation mind builds on it next wake."
    ),
    parameters={
        "text": {"type": "string", "description": "The full updated situation document (markdown; keep the "
                 "Now / Active threads / Expected next / Open questions shape; tight)."},
    },
    required=["text"],
)
async def update_situation(text: str) -> str:
    body = (text or "").strip()
    if not body:
        return "Give me the situation text to write."
    body = body[:SITUATION_CAP]
    try:
        _atomic_write(DATA / "situation.md", body)
    except Exception as e:
        return f"Couldn't update the situation model: {e}."
    return (f"Situation model updated ({len(body)} chars). Your mind will build on this "
            "at its next wake.")

"""Session status + management tools — read status, send messages, start and resume CC sessions.

Any CC session, training script, or background task can write a status file to
Desktop/jarvis_sessions/<name>.json. This tool reads all of them so the user can
ask "what's happening with MyProject?" and get a real answer.

Status file format (write from any process):
{
  "session": "myproject",
  "status": "running",          # running | done | waiting | failed
  "message": "Epoch 23/30, loss 1.23",
  "progress": 75,               # optional 0-100
  "updated": "2026-06-29T21:45:00"
}

Write from a CC session or training script:
  import json, pathlib, datetime
  pathlib.Path.home().joinpath("Desktop/jarvis_sessions").mkdir(exist_ok=True)
  pathlib.Path.home().joinpath("Desktop/jarvis_sessions/myproject.json").write_text(
      json.dumps({"session":"myproject","status":"running","message":"Epoch 23/30","progress":75,
                  "updated": datetime.datetime.now().isoformat(timespec="seconds")})
  )
"""
import asyncio
import json
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from tool_registry import tool

_SESSIONS_DIR = Path.home() / "Desktop" / "jarvis_sessions"


@tool(
    name="check_sessions",
    description=(
        "Check the status of background processes and running tasks — training jobs, "
        "Claude Code sessions, builds, or any script that reports status. Returns what "
        "each process is doing and how far along it is. Use when the user asks 'what's "
        "happening with X', 'how's the training going', 'is the build done', etc."
    ),
    parameters={
        "name": {
            "type": "string",
            "description": "Specific session name to check (optional). Omit to see all sessions.",
        }
    },
    required=[],
)
async def check_sessions(name: str = "") -> str:
    if not _SESSIONS_DIR.exists():
        return "No active sessions — nothing has reported status yet."

    files = sorted(_SESSIONS_DIR.glob("*.json"))
    if not files:
        return "No active sessions — nothing has reported status yet."

    if name:
        name_lower = name.lower()
        files = [f for f in files if name_lower in f.stem.lower()]
        if not files:
            return f"No session matching '{name}' found."

    results = []
    for f in files:
        try:
            data = json.loads(f.read_text())
            session = data.get("session") or f.stem
            status  = data.get("status", "unknown")
            message = data.get("message", "")
            progress = data.get("progress")
            updated = data.get("updated", "")

            line = f"{session}: {status}"
            if progress is not None:
                line += f" ({progress}%)"
            if message:
                line += f" — {message}"
            if updated:
                line += f" [updated {updated}]"
            results.append(line)
        except Exception:
            results.append(f"{f.stem}: (unreadable status file)")

    return "\n".join(results)


import re as _re
import shutil as _shutil


def _find_session(name: str) -> dict | None:
    """Load session data by exact or partial name match."""
    exact = _SESSIONS_DIR / f"{name}.json"
    if exact.exists():
        try:
            return json.loads(exact.read_text(encoding="utf-8-sig"))
        except Exception:
            return None
    for f in _SESSIONS_DIR.glob("*.json"):
        if "_inbox" in f.name:
            continue
        if name.lower() in f.stem.lower():
            try:
                return json.loads(f.read_text(encoding="utf-8-sig"))
            except Exception:
                pass
    return None


async def _relay_to_cc(session_name: str, session_id: str, message: str) -> str:
    """Run claude --resume and return the spoken response."""
    claude_bin = _shutil.which("claude")
    if not claude_bin:
        return "Claude CLI not found."
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "--resume", session_id, "-p", message,
            "--permission-mode", "bypassPermissions",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=90.0)
        raw = stdout.decode("utf-8", errors="replace").strip()
        # Strip JSON protocol wrapper {"say":"..."} if present
        m = _re.search(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)"', raw)
        reply = m.group(1).replace('\\"', '"') if m else raw
        return f"[{session_name}]: {reply}" if reply else f"Message delivered — no response from '{session_name}'."
    except asyncio.TimeoutError:
        return f"Message delivered to '{session_name}' but timed out waiting for a reply."
    except Exception as e:
        return f"Relay error for '{session_name}': {e}"


@tool(
    name="message_session",
    description=(
        "Send a message to a running session and get its response. For Claude Code sessions "
        "this does a live round-trip — the CC session is activated, processes the message, "
        "and you get its reply back right now. For training jobs and builds this queues a "
        "control instruction (stop/pause/checkpoint). Use when the user wants to talk to a "
        "CC session, check in with a running job, or send it instructions."
    ),
    parameters={
        "session_name": {
            "type": "string",
            "description": "Name of the session to contact (from check_sessions).",
        },
        "message": {
            "type": "string",
            "description": "The message or instruction to send.",
        },
    },
    required=["session_name", "message"],
)
async def message_session(session_name: str, message: str) -> str:
    from jtools.activity_log import log_activity
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    data = _find_session(session_name)
    if not data:
        return f"No session '{session_name}' found. Use check_sessions to list active sessions."

    actual_name = data.get("session", session_name)
    session_id = data.get("session_id", "")
    session_type = data.get("type", "")
    log_activity(actual_name, "message", message, speaker="orb")

    # Liveness first: a dead session must get an HONEST answer, not an inbox
    # file nothing will ever read plus an optimistic "queued for next
    # checkpoint" (exactly what happened live 2026-07-01 — messaging an ended
    # terminal sounded like it worked). A cc session with a session_id is the
    # exception: `claude --resume` revives it even after the process exited.
    status = (data.get("status") or "").lower()
    pid = data.get("pid")
    proc_alive = True
    if pid:
        try:
            import psutil
            proc_alive = psutil.pid_exists(int(pid))
        except Exception:
            pass
    ended = status in ("done", "stopped", "failed", "ended") or not proc_alive

    # Visible session (its own real terminal window) — type into it directly.
    # This beats the claude --resume relay: it's the SAME running process the
    # user is actually looking at, not a separate spawned one.
    if data.get("visible") and not ended:
        try:
            from jtools.terminal_tool import resolve_session_window, focus_and_type
            win = resolve_session_window(data)
            if win:
                focus_and_type(win["hwnd"], message, True)
                # Keep using this exact window next time too — persist the hwnd
                # if it changed (first resolution, or the old one had gone stale).
                if win["hwnd"] != data.get("hwnd"):
                    data["hwnd"] = win["hwnd"]
                    (_SESSIONS_DIR / f"{actual_name}.json").write_text(
                        json.dumps(data, indent=2), encoding="utf-8")
                log_activity(actual_name, "lifecycle", f"Typed into live window: {message[:80]}")
                return f"Typed \"{message}\" into {actual_name}'s open terminal."
        except Exception:
            pass  # fall through to the relay/inbox paths below

    # CC session (headless, or its window is gone): direct live relay via
    # claude --resume — activates the session and returns its response.
    if session_type == "cc" and session_id:
        result = await _relay_to_cc(actual_name, session_id, message)
        log_activity(actual_name, "message", result, speaker=actual_name)
        return result

    # Dead with no resumable session id — nothing is listening; say so.
    if ended:
        log_activity(actual_name, "lifecycle", "Message not deliverable — session has ended")
        return (f"'{actual_name}' has already ended (status: {status or 'process gone'}) — "
                f"nothing is listening, so the message was NOT queued. Say the word and I'll "
                f"start a fresh session with it as the task instead.")

    # Training / build / other: inbox-based control (session checks at its next checkpoint)
    inbox = _SESSIONS_DIR / f"{actual_name}_inbox.json"
    payload = {
        "id": uuid.uuid4().hex[:8],
        "from": "orb",
        "message": message,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }
    inbox.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    log_activity(actual_name, "lifecycle", "Instruction queued for next checkpoint")
    return f"Instruction queued for '{actual_name}'. It will act on it at its next checkpoint."


@tool(
    name="start_cc_session",
    description=(
        "Start a new Claude Code session for a task, running it in the background. "
        "Returns the session name and PID so you can track it with check_sessions. "
        "Use when the user asks you to start a build, research task, or any long-running "
        "job that should run independently while the conversation continues."
    ),
    parameters={
        "task": {
            "type": "string",
            "description": "The task or instructions for the Claude Code session.",
        },
        "session_name": {
            "type": "string",
            "description": "Short name for tracking (e.g. 'myproject-train'). Auto-generated if omitted.",
        },
        "working_dir": {
            "type": "string",
            "description": "Working directory for the session. Defaults to Desktop.",
        },
    },
    required=["task"],
)
async def start_cc_session(task: str, session_name: str = "", working_dir: str = "",
                           visible: bool = False, engine: str = "claude-code",
                           machine: str = "pc") -> str:
    """Start a CC session. visible=True opens a Windows Terminal tab the user can watch.
    visible=False (default) runs headless in background and is tracked via check_sessions.
    engine: 'claude-code'|'grok'|'grok-build'|'codex' — shown as identity label in iOS.
    machine: 'pc'|'mac' — which machine is running this."""
    from jtools.activity_log import log_activity
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return "Claude CLI not found — make sure Claude Code is installed."
    name = session_name or (task[:32].lower().strip().replace(" ", "-").replace("/", "-") + "-" + uuid.uuid4().hex[:4])
    wd = working_dir or str(Path.home() / "Desktop")
    if not Path(wd).is_dir():
        # A bad working_dir (typo'd from the phone) must degrade, not 500 —
        # the spawn's cwd has to exist. The task itself can mkdir whatever.
        wd = str(Path.home() / "Desktop")
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    status_path = _SESSIONS_DIR / f"{name}.json"
    status_path.write_text(json.dumps({
        "session": name, "status": "starting", "type": "cc",
        "description": task[:120], "message": "Launching…",
        "working_dir": wd, "engine": engine, "machine": machine,
        "updated": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))
    log_activity(name, "lifecycle", f"Starting: {task[:120]}")

    if visible:
        # Open a DEDICATED Windows Terminal window (-w new, not just a tab in an
        # existing one) so this session can be reliably found+focused later by
        # title — see terminal_tool.py. Uses -p to seed the initial task;
        # terminal stays open with -NoExit for follow-up prompts.
        from jtools.terminal_tool import resolve_shell
        # The task text used to be embedded inline, where it must survive THREE
        # parsers (wt → PowerShell → claude argv). An apostrophe in a proposal
        # description broke that chain live (wt error 0x80070002, 2026-07-02).
        # Sidestep all of them: prompt goes in a file; only paths are embedded.
        task_file = status_path.with_name(f"{name}_task.txt")
        task_file.write_text(task, encoding="utf-8")
        wt = shutil.which("wt") or "wt.exe"
        proc = await asyncio.create_subprocess_exec(
            wt, "-w", "new", "new-tab", "--title", name,
            resolve_shell(), "-NoExit", "-Command",
            f'Set-Location "{wd}"; claude -p (Get-Content -Raw -Encoding UTF8 "{task_file}") '
            f'--permission-mode bypassPermissions',
        )
        # Capture the real window handle now, while the title still matches
        # exactly what we set it to — this is what makes later messaging
        # reliable even after the terminal retitles itself (CC does this).
        hwnd = None
        try:
            from jtools.terminal_tool import wait_for_window
            win = await wait_for_window(name, timeout=5.0)
            hwnd = win["hwnd"] if win else None
        except Exception:
            pass
        status_path.write_text(json.dumps({
            "session": name, "status": "running", "type": "cc",
            "description": task[:120], "message": f"Visible terminal (PID {proc.pid})",
            "working_dir": wd, "pid": proc.pid, "visible": True, "hwnd": hwnd,
            "engine": engine, "machine": machine,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }, indent=2))
        log_activity(name, "lifecycle", f"Visible terminal opened (PID {proc.pid})")
        _book_supervision(name, task)
        return f"Opened terminal tab '{name}' — task is running. You can interact with it directly."
    else:
        # Headless background — tracked via sessions status file. stdout is
        # stream-json (2026-07-01): every tool call the spawned agent makes
        # becomes a live session_event + activity-log line at zero LLM cost —
        # the "watch the AI work" feed — and process exit finally marks the
        # record done/failed instead of leaving it "running" forever.
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "-p", task, "--permission-mode", "bypassPermissions",
            "--output-format", "stream-json", "--verbose",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            cwd=wd,
        )
        status_path.write_text(json.dumps({
            "session": name, "status": "running", "type": "cc",
            "description": task[:120], "message": f"Running (PID {proc.pid})",
            "working_dir": wd, "pid": proc.pid,
            "engine": engine, "machine": machine,
            "updated": datetime.now().isoformat(timespec="seconds"),
        }, indent=2))
        log_activity(name, "lifecycle", f"Headless session running (PID {proc.pid})")
        asyncio.create_task(_watch_cc_stream(name, proc, status_path))
        _book_supervision(name, task)
        return f"Started session '{name}' (PID {proc.pid}) — headless. Track with check_sessions."


# ── Supervision (2026-07-02, ninjahawk: "a problem for me not to be updated,
# for sessions and builds being unsupervised") ─────────────────────────────────
# Two layers: a deterministic completion push when any watched session ends
# (zero AI), and a mind check-back wake booked at every launch so an agent
# actually looks at the work again after a while and keeps looking until done.

_notify_fn = None  # wired by server_win at startup → _push_notification


def wire_notify(fn) -> None:
    global _notify_fn
    _notify_fn = fn


def _book_supervision(name: str, task: str) -> None:
    """Every launched session books the mind a check-back wake."""
    try:
        import os as _os
        import mind as _mind
        mins = float(_os.getenv("JARVIS_SESSION_CHECKBACK_MIN", "20"))
        _mind.request_wake_at(mins, f"supervision: check on session '{name}' ({task[:70]})")
    except Exception:
        pass


def _cc_step_event(obj: dict) -> tuple[str, str]:
    """One (kind, text) line for one stream-json event from a headless claude
    run — the salient argument only (the file being edited, the command run),
    so the phone's feed reads like steps, not JSON."""
    try:
        if obj.get("type") == "assistant":
            for block in (obj.get("message") or {}).get("content") or []:
                if block.get("type") == "tool_use":
                    inp = block.get("input") or {}
                    arg = str(inp.get("file_path") or inp.get("command")
                              or inp.get("pattern") or inp.get("url")
                              or inp.get("prompt") or "")[:160]
                    nm = block.get("name", "tool")
                    return ("tool_start", f"{nm}: {arg}" if arg else nm)
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    return ("message", block["text"].strip()[:200])
    except Exception:
        pass
    return ("", "")


async def _watch_cc_stream(name: str, proc, status_path: Path) -> None:
    """Follow a headless session's stream-json stdout live, then close out the
    session record on exit. Chunk+split (not readline) so an oversized line —
    a tool result carrying a whole file — can't blow the stream limit and
    deadlock the pipe."""
    from jtools.activity_log import log_activity
    final = ""
    buf = b""
    sid_saved = False
    try:
        while True:
            chunk = await proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                # Capture the claude session id from the init event and persist
                # it — THIS is what makes a headless session resumable and
                # message-able (`claude --resume <id>`) even after its process
                # exits. Before 2026-07-01 headless sessions never recorded it.
                if obj.get("session_id") and not sid_saved:
                    sid_saved = True
                    try:
                        d = json.loads(status_path.read_text(encoding="utf-8-sig"))
                    except Exception:
                        d = {"session": name, "type": "cc"}
                    d["session_id"] = obj["session_id"]
                    try:
                        status_path.write_text(json.dumps(d, indent=2))
                    except Exception:
                        pass
                if obj.get("type") == "result":
                    final = str(obj.get("result") or "")[:400]
                    continue
                kind, text = _cc_step_event(obj)
                if kind:
                    log_activity(name, kind, text)
    except Exception as e:
        log_activity(name, "lifecycle", f"Output stream error: {e}")
    rc = await proc.wait()
    try:
        d = json.loads(status_path.read_text(encoding="utf-8-sig"))
    except Exception:
        d = {"session": name, "type": "cc"}
    if d.get("status") != "stopped":   # a user Stop beat us here — keep that
        d["status"] = "done" if rc == 0 else "failed"
        d["message"] = (final or ("Finished." if rc == 0 else f"Exited with code {rc}"))[:300]
        d["updated"] = datetime.now().isoformat(timespec="seconds")
        try:
            status_path.write_text(json.dumps(d, indent=2))
        except Exception:
            pass
        log_activity(name, "lifecycle", f"Session {d['status']}: {d['message'][:200]}")
        # Proposal-spawned runs (mind-<id>/proposal-<id>) confirm their idea
        # card: in_progress → done/failed (BACKEND_MIGRATION 07-23 §8).
        if name.startswith(("mind-", "proposal-")):
            try:
                import proactive_engine as _pe
                _pe.mark_proposal_for_session(name, d["status"] == "done")
            except Exception:
                pass
        # Deterministic completion push — builds must never finish silently.
        # `session=` rides the push extras so the phone opens this session's
        # feed directly (BACKEND_TODO 07-02 #3) instead of parsing the title.
        if _notify_fn:
            try:
                ok = d["status"] == "done"
                await _notify_fn(
                    f"Session {d['status']}: {name}",
                    d["message"][:300] or ("Finished." if ok else "Failed."),
                    kind="done" if ok else "alert",
                    topic=f"session-{name}", source="sessions",
                    session=name)
            except Exception:
                pass


@tool(
    name="resume_cc_session",
    description=(
        "Resume a previous Claude Code session by session ID. Use when the user asks to "
        "pick back up a prior conversation, continue a task, or restart a session. "
        "The session ID comes from check_sessions (stored in the session file)."
    ),
    parameters={
        "session_name": {
            "type": "string",
            "description": "Name of the session to resume (from check_sessions output).",
        },
        "extra_instructions": {
            "type": "string",
            "description": "Additional instructions or context to add when resuming (optional).",
        },
    },
    required=["session_name"],
)
async def resume_cc_session(session_name: str, extra_instructions: str = "") -> str:
    claude_bin = shutil.which("claude")
    if not claude_bin:
        return "Claude CLI not found."
    session_file = _SESSIONS_DIR / f"{session_name}.json"
    if not session_file.exists():
        return f"No session file for '{session_name}'. Use check_sessions to list sessions."
    try:
        data = json.loads(session_file.read_text())
    except Exception as e:
        return f"Could not read session file: {e}"
    session_id = data.get("session_id", "")
    wd = data.get("working_dir") or str(Path.home() / "Desktop")
    if session_id:
        args = [claude_bin, "--resume", session_id, "--permission-mode", "bypassPermissions"]
        if extra_instructions:
            args += ["-p", extra_instructions]
    else:
        if not extra_instructions:
            return (f"Session '{session_name}' has no stored session_id for resumption. "
                    f"Provide extra_instructions to start a follow-up instead.")
        args = [claude_bin, "-p", extra_instructions, "--permission-mode", "bypassPermissions"]
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        cwd=wd,
    )
    from jtools.activity_log import log_activity
    log_activity(session_name, "lifecycle", f"Resumed (PID {proc.pid})")
    return f"Resumed session '{session_name}' (PID {proc.pid})."

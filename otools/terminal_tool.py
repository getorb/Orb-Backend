"""Real terminal session management — detect live terminal windows on the PC
(whether Orb started them or not) and talk to a specific one by typing
directly into it, the way a person would switch windows and type.

Why this exists: `claude --resume <id> -p <msg>` (sessions_tool.py's relay)
spawns a brand-new short-lived process that shares the CC session's history
but is NOT the same running process the user is watching in a terminal tab —
so replies never appear where the user is actually looking, and it can't
continue truly interactive state. For any VISIBLE session (one opened with
`start_cc_session(visible=True)` or `start_terminal_session`), typing directly
into the real window is a better answer where it's available; the relay stays
as the fallback for headless sessions or if the window was closed.

Targeting relies on each visible session owning its OWN top-level OS window
(`wt -w new`, not just a new tab in a shared one) so a window title lookup is
unambiguous — see the `-w new` in sessions_tool.start_cc_session.
"""

from __future__ import annotations

import asyncio
import shutil
import time

from tool_registry import tool

_SHELL: str | None = None


def resolve_shell() -> str:
    """PowerShell 7 (pwsh) if installed, else the built-in Windows PowerShell
    5.1 (powershell.exe) that's always present. Cached — checked once, not on
    every terminal spawn. Confirmed live 2026-07-01: pwsh isn't installed on
    this machine, hardcoding it made every new terminal fail to launch at all."""
    global _SHELL
    if _SHELL is None:
        _SHELL = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    return _SHELL

_TERMINAL_PROCESSES = {"windowsterminal.exe", "powershell.exe", "pwsh.exe", "cmd.exe"}


def _enum_terminal_windows() -> list[dict]:
    """Every visible top-level window whose owning process is a known terminal
    executable. Best-effort — returns [] on any import/API failure."""
    try:
        import win32gui
        import win32process
        import psutil
    except ImportError:
        return []

    out: list[dict] = []

    def _cb(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            if not title:
                return True
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            try:
                pname = psutil.Process(pid).name().lower()
            except Exception:
                return True
            if pname in _TERMINAL_PROCESSES:
                out.append({"hwnd": hwnd, "title": title, "pid": pid, "process": pname})
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return []
    return out


def _find_window_for(name: str) -> dict | None:
    """Best match for a session/terminal name: exact title match first, then
    substring, case-insensitive."""
    windows = _enum_terminal_windows()
    name_l = name.lower()
    for w in windows:
        if w["title"].lower() == name_l:
            return w
    for w in windows:
        if name_l in w["title"].lower():
            return w
    return None


def _hwnd_still_valid(hwnd: int) -> bool:
    try:
        import win32gui
        return bool(win32gui.IsWindow(hwnd))
    except Exception:
        return False


def resolve_session_window(session_data: dict) -> dict | None:
    """Find the real window for a tracked session, hwnd-first.

    Window TITLES drift — Claude Code retitles its own terminal tab to reflect
    whatever it's currently doing, so title-matching on every call is fragile
    once a session's been running a while. Once a session is matched to a
    window, callers should persist the hwnd (see sessions_tool.py) and this
    function will keep using that EXACT window as long as it still exists,
    only falling back to a fresh title search if it's gone.
    """
    hwnd = session_data.get("hwnd")
    if hwnd and _hwnd_still_valid(hwnd):
        try:
            import win32gui
            return {"hwnd": hwnd, "title": win32gui.GetWindowText(hwnd)}
        except Exception:
            pass
    # Stale or never-recorded — fall back to a title search.
    name = session_data.get("session", "")
    return _find_window_for(name) if name else None


async def wait_for_window(title: str, timeout: float = 5.0) -> dict | None:
    """Poll for a just-spawned window with this exact title, so its hwnd can be
    cached at creation time instead of guessed at later via title search."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        win = await asyncio.to_thread(_find_window_for, title)
        if win and win["title"].lower() == title.lower():
            return win
        await asyncio.sleep(0.3)
    return None


def focus_and_type(hwnd: int, text: str, press_enter: bool = True) -> None:
    """Bring a window to the foreground and type into it. STEALS FOCUS from
    whatever the user is currently doing — only call this for an explicit
    'send this to session X' request, never opportunistically."""
    import win32gui
    import win32con
    import pyautogui

    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    win32gui.SetForegroundWindow(hwnd)
    time.sleep(0.15)  # let the window actually take focus before typing
    pyautogui.typewrite(text, interval=0.01)
    if press_enter:
        pyautogui.press("enter")


@tool(
    name="detect_pc_sessions",
    description=(
        "List every terminal window currently open on the PC — Windows Terminal, "
        "PowerShell, or cmd — whether Orb started it or the user opened it manually. "
        "Use when the user asks what terminals/sessions are open, or before messaging a "
        "session to confirm it still has a live window."
    ),
    parameters={},
    required=[],
)
async def detect_pc_sessions() -> str:
    windows = await asyncio.to_thread(_enum_terminal_windows)
    if not windows:
        return "No terminal windows currently open (or window detection isn't available)."
    lines = [f'  "{w["title"]}" ({w["process"]}, PID {w["pid"]})' for w in windows]
    return f"{len(windows)} terminal window(s) open:\n" + "\n".join(lines)


@tool(
    name="send_to_terminal_session",
    description=(
        "Type a message directly into a specific open terminal window and press Enter — "
        "as if the user switched to it and typed it themselves. Works for any session "
        "started with visible=True (its own dedicated window, titled with its session "
        "name) or any manually-opened terminal matched by title. This STEALS SCREEN FOCUS "
        "momentarily. Use when the user wants to actually talk to a visible/interactive "
        "session, not just queue an instruction for later. IMPORTANT: this reaches an "
        "EXISTING, already-running window — it is NOT the same as running a shell command "
        "yourself, which would spawn a separate new process and never touch the named "
        "session. If a session name is given, this is the only tool that can actually "
        "reach it; do not substitute your own shell/exec tool for this."
    ),
    parameters={
        "session_name": {
            "type": "string",
            "description": "Session name or a substring of the terminal window's title.",
        },
        "message": {
            "type": "string",
            "description": "Text to type into the terminal.",
        },
    },
    required=["session_name", "message"],
)
async def send_to_terminal_session(session_name: str, message: str) -> str:
    from pathlib import Path
    import json as _json

    # If this is a tracked session, use its cached hwnd (title-independent —
    # survives the terminal retitling itself) and persist any change to it.
    sessions_dir = Path.home() / "Desktop" / "orb_sessions"
    status_path = sessions_dir / f"{session_name}.json"
    data = {}
    if status_path.exists():
        try:
            data = _json.loads(status_path.read_text(encoding="utf-8-sig"))
        except Exception:
            data = {}

    win = await asyncio.to_thread(resolve_session_window, data) if data else \
        await asyncio.to_thread(_find_window_for, session_name)
    if not win:
        return (f"No open terminal window matches '{session_name}'. "
                f"Use detect_pc_sessions to see what's actually open.")
    try:
        await asyncio.to_thread(focus_and_type, win["hwnd"], message, True)
    except Exception as e:
        return f"Could not type into '{win['title']}': {e}"

    if data and win["hwnd"] != data.get("hwnd"):
        data["hwnd"] = win["hwnd"]
        try:
            status_path.write_text(_json.dumps(data, indent=2), encoding="utf-8")
        except Exception:
            pass
    return f"Typed into \"{win['title']}\" and pressed Enter."


@tool(
    name="start_terminal_session",
    description=(
        "Open a brand-new, plain terminal window — NOT a Claude Code session. Use when the "
        "user wants a normal terminal/shell open (to run their own commands, watch a build, "
        "or just have a window to work in), as opposed to 'start a session to build X' "
        "which should use start_cc_session instead. If a command is given it runs "
        "immediately; if omitted, it's just an open interactive shell."
    ),
    parameters={
        "session_name": {
            "type": "string",
            "description": "Short name for tracking + the window title (e.g. 'scratch').",
        },
        "command": {
            "type": "string",
            "description": "Optional command to run immediately in the new shell.",
        },
        "working_dir": {
            "type": "string",
            "description": "Working directory. Defaults to Desktop.",
        },
    },
    required=["session_name"],
)
async def start_terminal_session(session_name: str, command: str = "", working_dir: str = "") -> str:
    import json
    import shutil
    import uuid as _uuid
    from datetime import datetime
    from pathlib import Path
    from otools.activity_log import log_activity

    sessions_dir = Path.home() / "Desktop" / "orb_sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    wd = working_dir or str(Path.home() / "Desktop")
    wt = shutil.which("wt") or "wt.exe"

    args = [wt, "-w", "new", "new-tab", "--title", session_name, resolve_shell(), "-NoExit"]
    if command:
        safe = command.replace('"', '\\"')
        args += ["-Command", safe]

    proc = await asyncio.create_subprocess_exec(*args, cwd=wd)

    win = await wait_for_window(session_name, timeout=5.0)
    hwnd = win["hwnd"] if win else None

    status_path = sessions_dir / f"{session_name}.json"
    status_path.write_text(json.dumps({
        "session": session_name, "status": "running", "type": "terminal",
        "description": command[:120] if command else "Plain terminal",
        "message": f"Open terminal (PID {proc.pid})",
        "working_dir": wd, "pid": proc.pid, "visible": True, "hwnd": hwnd,
        "engine": "terminal", "machine": "pc",
        "updated": datetime.now().isoformat(timespec="seconds"),
    }, indent=2))
    log_activity(session_name, "lifecycle", "Terminal opened" + (f": {command}" if command else ""))
    return f"Opened a plain terminal '{session_name}' (PID {proc.pid})." + (
        f" Running: {command}" if command else " Ready for input.")

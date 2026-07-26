"""AI/file-write activity tracking — watches the known project directories
(the same list the proactive engine already uses for staleness/git checks)
for real file changes and surfaces them as live session activity. This is
the "what's actually being worked on" signal — broader than git (catches
uncommitted work) and more meaningful than a raw mtime check (catches WHICH
files, live, as it happens).

Honest limitation: OS file-change events carry a path, not a process identity
— there's no reliable way to say "Claude Code wrote this" vs. "the user typed
it in an editor" from filesystem events alone. This tracks WORK happening in
known project directories as a proxy for "something (an AI session, or you)
is actively in here" — it's not literal per-process AI attribution.

Architecture: watchdog's Observer runs on its own thread and calls handler
methods from THAT thread, not the asyncio loop — so the handler only pushes
onto a plain thread-safe queue.Queue. All debouncing and log_activity() calls
(which needs a running event loop to broadcast live) happen in run_forever(),
a normal asyncio task on the main loop.

Writes are coalesced per project over a short debounce window rather than
logged one-by-one — "3 files modified in Tide" as one live event, not three
disconnected ones, matching "track what's being worked on" not raw noise.
"""

from __future__ import annotations

import asyncio
import logging
import queue
import time
from pathlib import Path

log = logging.getLogger("orb.file_watcher")

_EXCLUDE_DIR_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".vs", ".idea", "checkpoints", ".pytest_cache", ".claude",
    ".qtwebengine_data", ".webview_data",
}
_EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".log", ".lock", ".tmp",
                     ".db", ".db-shm", ".db-wal")  # sqlite + sidecars — runtime
                     # state churn, not work (data/orb.db-shm was flooding
                     # the Orb feed alongside the mcps tmp files, 07-03)
_OWN_DATA_DIR = (Path(__file__).parent.parent / "data").resolve()
_OWN_LOG_FILES = {"backend.log", "backend_err.log"}

_DEBOUNCE_SECONDS = 4.0   # settle time before flushing a burst as one event
_FLUSH_INTERVAL = 2.0     # how often the flush task checks for settled bursts

_event_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()  # (project_name, relative_path)
_pending: dict[str, dict] = {}  # project_name -> {"files": set(), "last": float}
_started = False


def _is_excluded(path: Path, project_root: Path) -> bool:
    try:
        rel_parts = path.relative_to(project_root).parts
    except ValueError:
        return True
    if any(part in _EXCLUDE_DIR_NAMES for part in rel_parts):
        return True
    try:
        if path.resolve().is_relative_to(_OWN_DATA_DIR):
            return True  # Orb's own runtime state -- watching this self-loops
    except (ValueError, OSError):
        pass
    if path.name in _OWN_LOG_FILES:
        return True
    if path.suffix.lower() in _EXCLUDE_SUFFIXES:
        return True
    if path.name.startswith("~$") or path.name.endswith("~"):
        return True
    # tempfile-style dotted temp names (".tmpXXXXXX") have NO Path.suffix — the
    # leading dot makes them dotfiles — so the .tmp suffix exclude misses them.
    # An MCP server's tools dir churned 466+ of these per flush into the
    # activity feed ("469 file(s) changed: mcps\\agentos\\tools\\.tmp...", 07-03).
    if path.name.startswith(".tmp"):
        return True
    return False


def start_watching() -> None:
    """Start watching all known project dirs. Safe to call multiple times."""
    global _started
    if _started:
        return
    _started = True
    try:
        from watchdog.observers import Observer
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        log.warning("[file_watcher] watchdog not installed — pip install watchdog")
        return

    import proactive_engine

    class _ProjectHandler(FileSystemEventHandler):
        def __init__(self, project_name: str, project_root: Path):
            self.project_name = project_name
            self.project_root = project_root

        def _push(self, event):
            if event.is_directory:
                return
            path = Path(event.src_path)
            if _is_excluded(path, self.project_root):
                return
            # Windows fires "modified" on attribute/access-only churn too (AV
            # scans, the indexer) — 13 llama.cpp .gitignores "changed" at 2:43am
            # with nobody home. A real write refreshes mtime; metadata churn
            # doesn't. Stale mtime => not actual work, drop it.
            try:
                if time.time() - path.stat().st_mtime > 120:
                    return
            except OSError:
                pass
            try:
                rel = str(path.relative_to(self.project_root))
            except ValueError:
                rel = path.name
            _event_queue.put((self.project_name, rel))

        def on_created(self, event):
            self._push(event)

        def on_modified(self, event):
            self._push(event)

        def on_moved(self, event):
            self._push(event)

    observer = Observer()
    watched = 0
    for name, path in proactive_engine._PROJECT_DIRS:
        p = Path(path) if not isinstance(path, Path) else path
        if not p.is_dir():
            continue
        try:
            observer.schedule(_ProjectHandler(name, p), str(p), recursive=True)
            watched += 1
        except Exception as e:
            log.warning(f"[file_watcher] could not watch {name}: {e}")
    if watched:
        observer.start()
        log.info(f"[file_watcher] watching {watched} project dir(s) for file activity")


async def run_forever() -> None:
    """Drain the queue and flush settled bursts as live activity. Runs on the
    main asyncio loop (unlike the watchdog thread) so log_activity's live
    broadcast actually has an event loop to schedule onto."""
    while True:
        try:
            while True:
                project, rel = _event_queue.get_nowait()
                bucket = _pending.setdefault(project, {"files": set(), "last": 0.0})
                bucket["files"].add(rel)
                bucket["last"] = time.time()
        except queue.Empty:
            pass

        now = time.time()
        for project, bucket in list(_pending.items()):
            if not bucket["files"] or now - bucket["last"] < _DEBOUNCE_SECONDS:
                continue
            _flush(project, bucket["files"])
            _pending[project] = {"files": set(), "last": 0.0}

        await asyncio.sleep(_FLUSH_INTERVAL)


def _flush(project: str, files: set) -> None:
    from jtools.activity_log import log_activity
    import json as _json
    from pathlib import Path as _Path
    from datetime import datetime as _dt

    names = sorted(files)
    shown = ", ".join(names[:3])
    if len(names) > 3:
        shown += f", +{len(names) - 3} more"
    text = f"{len(names)} file(s) changed: {shown}"
    log_activity(project, "file_write", text, files=names[:20])

    try:
        d = _Path.home() / "Desktop" / "orb_sessions"
        d.mkdir(parents=True, exist_ok=True)
        status_path = d / f"{project}.json"
        existing = {}
        if status_path.exists():
            try:
                existing = _json.loads(status_path.read_text(encoding="utf-8-sig"))
            except Exception:
                existing = {}
        # Don't clobber a real tracked session (a CC/training/terminal session
        # already has richer status) — this ambient signal is a fallback for
        # projects with no explicitly tracked session.
        if existing.get("type") not in ("cc", "training", "terminal"):
            status_path.write_text(_json.dumps({
                "session": project, "status": "active", "type": "file_activity",
                "description": f"File activity in {project}",
                "message": text, "machine": "pc",
                "updated": _dt.now().isoformat(timespec="seconds"),
            }, indent=2), encoding="utf-8")
    except Exception:
        pass

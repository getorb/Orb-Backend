"""supervisor.py — keeps server_win.py alive through crashes AND intentional
self-modification restarts.

Why this exists (ninjahawk, 2026-07-01): the self-mod story — an AI session
edits backend code, then needs the backend restarted WITHOUT a human — dead-ends
if the thing doing the restart IS the backend. This tiny separate process is
that "manual mechanism activated when it does the restart thing": the backend
just exits (POST /api/system/restart → exit code 42), and the supervisor
relaunches it and health-checks the result. A CC session doing self-mod stays
alive the whole time (it's a separate process), so it can verify the backend
came back and fix things if it didn't.

Airtight-ness, honestly stated: if new code crashloops (3 fast crashes in a
row), we do NOT try to auto-revert anything (destructive ops stay barred) — we
back off to 5-minute retries, mark the `backend-supervisor` session row
"failed" so it's visible the moment anything can read it, and keep the crash
evidence in backend_err.log (append mode, launch separators). The manual
fallback is backend_toggle.ps1 or a Claude Code session fixing the code.

Run:  venv\\Scripts\\python.exe supervisor.py   (backend_toggle.ps1 does this)
Stop: kill the supervisor FIRST, then the backend child (the toggle does both,
      in that order — killing the child first just gets it relaunched).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
PY = str(ROOT / "venv" / "Scripts" / "python.exe")


def _env_port() -> str:
    """ORB_PORT / ORB_PORT from .env or the process env — the supervisor must
    poll the same port the server binds, or a self-hoster who moves the port gets
    an eternal restart loop."""
    vals = {}
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                vals[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:
        pass
    return (vals.get("ORB_PORT") or vals.get("ORB_PORT")
            or os.environ.get("ORB_PORT") or os.environ.get("ORB_PORT") or "8340")


HEALTH = f"http://localhost:{_env_port()}/api/health"
RESTART_EXIT_CODE = 42   # server_win exits with this on an intentional restart
GRACE_S = 30             # boot time before health checks start counting
CHECK_EVERY_S = 15
FAILS_TO_RESTART = 3     # consecutive failed health checks => hung, bounce it
FAST_CRASH_S = 60        # died sooner than this after launch = "fast crash"
STATUS = Path.home() / "Desktop" / "orb_sessions" / "backend-supervisor.json"


def _status(status: str, message: str) -> None:
    try:
        STATUS.parent.mkdir(parents=True, exist_ok=True)
        STATUS.write_text(json.dumps({
            "session": "backend-supervisor", "status": status,
            "type": "supervisor", "description": "Keeps the Orb backend alive",
            "message": message, "machine": "pc", "pid": os.getpid(),
            "updated": datetime.now().isoformat(timespec="seconds"),
        }, indent=2), encoding="utf-8")
    except Exception:
        pass


def _healthy() -> bool:
    try:
        with urllib.request.urlopen(HEALTH, timeout=4) as r:
            return r.status == 200
    except Exception:
        return False


def _launch(out, err) -> subprocess.Popen:
    env = dict(os.environ, ORB_SUPERVISED="1")
    return subprocess.Popen(
        [PY, "server_win.py"], cwd=str(ROOT), stdout=out, stderr=err, env=env,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def main() -> None:
    fast_crashes = 0
    while True:
        # Append-mode logs with a launch separator — crash evidence has to
        # survive the relaunch (Start-Process's truncate-on-start didn't).
        out = open(ROOT / "backend.log", "ab")
        err = open(ROOT / "backend_err.log", "ab")
        sep = (f"\n===== supervisor launch "
               f"{datetime.now().isoformat(timespec='seconds')} =====\n").encode()
        out.write(sep); out.flush()
        err.write(sep); err.flush()

        started = time.time()
        proc = _launch(out, err)
        _status("running", f"Backend up (PID {proc.pid})")

        fails = 0
        rc = None
        while True:
            time.sleep(CHECK_EVERY_S)
            rc = proc.poll()
            if rc is not None:
                break
            if time.time() - started < GRACE_S:
                continue
            if _healthy():
                fails = 0
            else:
                fails += 1
                if fails >= FAILS_TO_RESTART:
                    _status("running", "Backend unresponsive — force-restarting it")
                    proc.kill()
                    try:
                        proc.wait(timeout=10)
                    except Exception:
                        pass
                    rc = proc.poll()
                    break
        out.close(); err.close()

        alive_for = time.time() - started
        if rc == RESTART_EXIT_CODE:
            fast_crashes = 0
            _status("running", "Intentional restart (self-mod) — relaunching now")
            continue
        fast_crashes = fast_crashes + 1 if alive_for < FAST_CRASH_S else 0
        if fast_crashes >= 3:
            _status("failed", f"Backend crashlooping (exit {rc}) — retrying every "
                              f"5 min; the code needs a fix")
            time.sleep(300)
        else:
            _status("running", f"Backend exited (code {rc}, up {int(alive_for)}s) "
                               f"— relaunching in 5s")
            time.sleep(5)


if __name__ == "__main__":
    main()

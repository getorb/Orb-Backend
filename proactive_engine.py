"""JARVIS Proactive Engine — data collection + planned execution.

Flow:
  1. Cheap detectors run every 15s (zero AI): log events, fire unambiguous alerts.
  2. Twice a day, at fixed clock times (default 6am + 6pm — see _SYNTHESIS_HOURS),
     ONE big synthesis call on a stronger model (Sonnet — see synthesis_brain_fn)
     reads every data source and outputs a PLAN:
       - Scheduled push notifications (title, body, time)
       - Notes/reminders to save immediately (no phone needed)
       - Calendar events worth adding (queued, applied next time the phone connects)
       - Up to 2 optional self-review times if warranted
       - Self-modification proposals
  3. A scheduler loop (60s, zero AI) fires the plan items at the right times.
  4. Self-scheduled extra reviews (from #2's additional_reviews) run on the cheap
     router (Haiku -> free backends) — smaller interstitial checks, not full
     re-synthesis, budget-capped separately from the two big calls.

AI call budget per day:
  - 2 synthesis reviews (6am, 6pm — Sonnet)
  - Up to 2 extra self-scheduled reviews (Haiku router)
  = max 4 model calls/day from the proactive engine, often fewer

Hard safety boundary (2026-07-01, per ninjahawk — see PROACTIVE_BRAIN.md): the
review is a FIXED JSON PLAN, never a tool-calling agent loop. It structurally
cannot write files, delete anything, or run arbitrary commands — the action
surface is exactly the fields in the plan schema below (notifications, notes,
calendar_events, additional_reviews, proposals) and nothing else, enforced by
what _apply_plan() reads, not by asking the model nicely. This matters because
the 6am run happens while ninjahawk is asleep with nobody to catch a mistake
live — the safety property needs to be structural, not just promised.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable

import psutil

log = logging.getLogger("jarvis.proactive")

_DATA_DIR       = Path(__file__).parent / "data"
_DATA_DIR.mkdir(parents=True, exist_ok=True)
_EVENTS_LOG     = _DATA_DIR / "events.jsonl"
_NOTIF_LOG      = _DATA_DIR / "notification_log.jsonl"
_CONTEXT_FILE   = _DATA_DIR / "proactive_context.json"
_PLAN_FILE      = _DATA_DIR / "active_plan.json"
_PROPOSALS_FILE = _DATA_DIR / "proposals.json"
_SESSIONS_DIR   = Path.home() / "Desktop" / "jarvis_sessions"

_MAX_EXTRA_REVIEWS     = int(os.getenv("JARVIS_MAX_EXTRA_REVIEWS", "2"))
# Fixed clock-time synthesis hours, not a rolling interval — "6am and 6pm",
# not "every 12h from whenever the server happened to start". Comma-separated
# 24h-clock hours.
_SYNTHESIS_HOURS = sorted({
    int(h.strip()) for h in os.getenv("JARVIS_SYNTHESIS_HOURS", "6,18").split(",") if h.strip()
})

# Hard-recurring 6h high-bar scan (2026-07-20). The old design was a self-
# rescheduling kind=task: each run had to re-arm +6h. When one link failed to
# re-schedule, the chain silently died (~29h gap after 7/19 15:46). This is now
# engine-owned like _SYNTHESIS_HOURS — a single miss cannot kill the cadence.
# Kill switch: JARVIS_SIX_HOUR_SCAN=0. Interval hours: JARVIS_SIX_HOUR_INTERVAL_H.
_SIX_HOUR_ENABLED = os.getenv("JARVIS_SIX_HOUR_SCAN", "1") != "0"
_SIX_HOUR_INTERVAL_S = max(0.25, float(os.getenv("JARVIS_SIX_HOUR_INTERVAL_H", "6"))) * 3600.0
_SIX_HOUR_TITLE = "6h proactive check"

_JOB_APPS_FILE = _DATA_DIR / "job_applications.json"
_PENDING_CALENDAR_FILE = _DATA_DIR / "pending_calendar_actions.json"

# Known project dirs — used for staleness + git activity. Operator-specific
# config, not source (moved to env 2026-07-03 for the OSS snapshot):
# JARVIS_PROJECT_DIRS is a JSON list of [label, path] pairs ("~" expands).
# Unset = no project tracking, the right default for a fresh install.
def _project_dirs_from_env() -> list[tuple[str, Path]]:
    raw = os.getenv("JARVIS_PROJECT_DIRS", "")
    if not raw:
        return []
    try:
        return [(str(label), Path(os.path.expanduser(str(p))))
                for label, p in json.loads(raw)]
    except Exception:
        log.warning("[proactive] bad JARVIS_PROJECT_DIRS "
                    "(want JSON [[label, path], ...]) — project tracking off")
        return []


_PROJECT_DIRS = _project_dirs_from_env()

_synthesis_fired_dates: dict[int, str] = {}  # hour -> "YYYY-MM-DD" last fired

# Wired by server_win.py
_push_fn:            Callable | None = None
_smart_brain_fn:     Callable | None = None  # cheap tier (Haiku router) — self-scheduled extras
_synthesis_brain_fn: Callable | None = None  # strong tier (Sonnet) — the 6am/6pm big synthesis
_screenshot_fn:      Callable | None = None  # async fn() -> bool; broadcasts a live
                                              # screenshot to connected mobile clients

# Latest connector context from the phone (calendar, reminders, contacts).
# Updated by server_win.py whenever {type:"context"} arrives over WS.
_connector_context: str = ""
_connector_updated_at: str = ""


def wire(push_fn: Callable, smart_brain_fn: Callable | None = None,
         screenshot_fn: Callable | None = None,
         synthesis_brain_fn: Callable | None = None) -> None:
    global _push_fn, _smart_brain_fn, _screenshot_fn, _synthesis_brain_fn
    _screenshot_fn      = screenshot_fn
    _push_fn            = push_fn
    _smart_brain_fn     = smart_brain_fn
    # Falls back to the cheap tier if the caller doesn't wire a dedicated
    # synthesis brain — keeps this optional so nothing breaks if unset.
    _synthesis_brain_fn = synthesis_brain_fn or smart_brain_fn


def update_connector_context(text: str) -> None:
    """Called by server_win.py whenever the phone sends fresh connector data."""
    global _connector_context, _connector_updated_at
    _connector_context   = (text or "").strip()
    _connector_updated_at = datetime.now().isoformat(timespec="seconds")


# ── Scheduled plan ────────────────────────────────────────────────────────────

@dataclass
class ScheduledItem:
    fire_at: datetime
    kind: str        # "notification" | "review"
    title: str = ""
    body: str = ""
    notif_kind: str = "info"
    reason: str = ""
    fired: bool = False
    attach_screenshot: bool = False  # model decides a screenshot would help this land
    source: str = "plan"  # "plan" = synthesis-owned (replaced wholesale each run)
                          # "user" = explicitly scheduled (tool/API) — the model
                          # may NOT clear these; see _apply_plan + schedule_notification


_schedule: list[ScheduledItem] = []
_last_review_at: float = 0.0
_last_six_hour_at: float = 0.0  # unix; hard-recurring 6h scan clock (persisted)
_extra_reviews_today: int = 0
_extra_reviews_date: str = ""  # YYYY-MM-DD — resets daily
_current_brief: str = ""  # last review's 2-3 sentence summary — persisted so
                          # GET /api/proactive/status has something to show,
                          # not just re-derivable from a one-time push notification


def _reset_daily_extras_if_needed() -> None:
    global _extra_reviews_today, _extra_reviews_date
    today = datetime.now().strftime("%Y-%m-%d")
    if _extra_reviews_date != today:
        _extra_reviews_today = 0
        _extra_reviews_date = today


def _save_plan() -> None:
    items = [
        {
            "fire_at": item.fire_at.isoformat(),
            "kind": item.kind,
            "title": item.title,
            "body": item.body,
            "notif_kind": item.notif_kind,
            "reason": item.reason,
            "fired": item.fired,
            "attach_screenshot": item.attach_screenshot,
            "source": item.source,
        }
        for item in _schedule
    ]
    _PLAN_FILE.write_text(
        json.dumps({"saved_at": datetime.now().isoformat(), "brief": _current_brief,
                    "last_review_at": _last_review_at,
                    "last_six_hour_at": _last_six_hour_at,
                    "items": items}, ensure_ascii=False),
        encoding="utf-8",
    )


def _load_plan() -> None:
    """Restore scheduled items from disk after a server restart."""
    global _schedule, _current_brief, _last_review_at, _last_six_hour_at
    if not _PLAN_FILE.exists():
        return
    try:
        data = json.loads(_PLAN_FILE.read_text(encoding="utf-8"))
        _current_brief = data.get("brief", "")
        # Persisted so a restart doesn't reset the synthesis lookback to the
        # 24h fallback (bit the first manual run, 2026-07-01 23:42).
        _last_review_at = float(data.get("last_review_at") or 0.0) or _last_review_at
        # Hard-recurring 6h clock. Prefer the dedicated field; fall back to the
        # last fired "6h proactive check" task timestamp so a first load after
        # this ship doesn't re-fire instantly if one ran recently.
        raw_six = data.get("last_six_hour_at")
        if raw_six:
            _last_six_hour_at = float(raw_six)
        elif not _last_six_hour_at:
            for d in data.get("items", []):
                if (d.get("kind") == "task"
                        and "6h proactive" in (d.get("title") or "").lower()
                        and d.get("fired")):
                    try:
                        ts = datetime.fromisoformat(d["fire_at"]).timestamp()
                        if ts > _last_six_hour_at:
                            _last_six_hour_at = ts
                    except Exception:
                        pass
        now = datetime.now()
        restored = []
        dropped = 0
        # Past-due grace (2026-07-03): the PC was off 04:00→17:00 and the day's
        # 10:00/14:00 user reminders were silently DROPPED here at boot —
        # "backend off at fire time = reminder evaporates" is exactly the
        # failure class the reminders rebuild exists to kill. Unfired items
        # missed by less than the grace window are restored and fire on the
        # first tick, honestly marked late; only genuinely stale ones drop.
        grace = timedelta(hours=float(os.getenv("JARVIS_SCHED_GRACE_H", "18")))
        for d in data.get("items", []):
            fire_at = datetime.fromisoformat(d["fire_at"])
            if d.get("fired"):
                continue
            body = d.get("body", "")
            if fire_at <= now:
                if now - fire_at > grace:
                    dropped += 1
                    continue
                if d.get("kind") == "notification" and now - fire_at > timedelta(minutes=10):
                    # Only mark genuinely-late deliveries — a quick restart that
                    # reloads an item a minute past due shouldn't sound dramatic
                    # (live 17:36: a fresh reminder double-tagged by a restart).
                    body = (f"{body} " if body else "") + (
                        f"(late — this was due {fire_at:%H:%M} while the backend was off)")
            restored.append(ScheduledItem(
                fire_at=fire_at, kind=d["kind"],
                title=d.get("title", ""), body=body,
                notif_kind=d.get("notif_kind", "info"),
                reason=d.get("reason", ""),
                attach_screenshot=d.get("attach_screenshot", False),
                source=d.get("source", "plan"),
            ))
        _schedule = restored
        log.info(f"[proactive] restored {len(restored)} scheduled items from disk"
                 + (f" ({dropped} stale past-due dropped)" if dropped else ""))
    except Exception as e:
        log.warning(f"[proactive] could not restore plan: {e}")


def _parse_time(t: str) -> datetime | None:
    """Parse HH:MM (today) or ISO datetime string into a datetime."""
    now = datetime.now()
    t = t.strip()
    if re.match(r"^\d{1,2}:\d{2}$", t):
        h, m = map(int, t.split(":"))
        candidate = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate
    try:
        return datetime.fromisoformat(t)
    except Exception:
        return None


# ── User-scheduled reminders (first-class, 2026-07-03) ───────────────────────
# Born the night grok tried to schedule a week of job reminders by writing a
# loader script against active_plan.json — and lost the race with _save_plan's
# minutely rewrite (external file edits CANNOT stick while the engine runs;
# the in-memory _schedule is the source of truth). Tools and endpoints call
# these functions in-process instead: no file race, restart-safe via the same
# active_plan.json persistence, and source="user" survives _apply_plan.

def schedule_notification(time_str: str, title: str, body: str = "",
                          notif_kind: str = "info") -> str:
    """Schedule a push notification. time_str: HH:MM (next occurrence) or an
    ISO datetime like 2026-07-04T10:00. Returns a human-readable outcome."""
    fire_at = _parse_time(time_str or "")
    if fire_at is None:
        return (f"Could not parse time '{time_str}' — use HH:MM for the next "
                "occurrence, or an ISO datetime like 2026-07-04T10:00.")
    now = datetime.now()
    if fire_at <= now:
        return f"{fire_at.isoformat(timespec='minutes')} is in the past — nothing scheduled."
    _schedule.append(ScheduledItem(
        fire_at=fire_at, kind="notification",
        title=(title or "Reminder").strip(), body=(body or "").strip(),
        notif_kind=(notif_kind or "info").strip(), source="user"))
    _save_plan()
    log.info(f"[proactive] user reminder scheduled: '{title}' at "
             f"{fire_at.isoformat(timespec='minutes')}")
    stamp = f"{fire_at:%a %b} {fire_at.day}, {fire_at:%H:%M}"
    return f"Scheduled — '{(title or 'Reminder').strip()}' will fire {stamp}."


def list_scheduled() -> list[dict]:
    """All unfired future scheduled items (notifications AND the synthesis's
    review check-ins), soonest first."""
    now = datetime.now()
    return [
        {"fire_at": item.fire_at.isoformat(timespec="minutes"), "kind": item.kind,
         "title": item.title, "body": item.body, "source": item.source,
         "reason": item.reason}
        for item in sorted(_schedule, key=lambda i: i.fire_at)
        if not item.fired and item.fire_at > now
    ]


def cancel_scheduled(query: str) -> list[str]:
    """Cancel unfired future NOTIFICATION items whose title or body contains
    query (case-insensitive). Review items are deliberately untouchable here —
    they're the synthesis's own check-ins, not user reminders. Returns the
    descriptions of what was cancelled (empty list = nothing matched)."""
    global _schedule
    q = (query or "").strip().lower()
    if not q:
        return []
    now = datetime.now()
    keep, removed = [], []
    for item in _schedule:
        if (not item.fired and item.fire_at > now and item.kind in ("notification", "task")
                and (q in item.title.lower() or q in item.body.lower())):
            removed.append(f"'{item.title}' @ {item.fire_at.isoformat(timespec='minutes')}")
        else:
            keep.append(item)
    if removed:
        _schedule = keep
        _save_plan()
        log.info(f"[proactive] cancelled {len(removed)} scheduled item(s) matching '{query}'")
    return removed


_PENDING_TASKS: set = set()   # refs to fired background task coroutines so they aren't GC'd


def schedule_task(time_str: str, title: str, task: str) -> str:
    """Schedule AUTONOMOUS WORK for a future time — not just a reminder. When it
    fires, the engine actually runs `task` on Claude (real file access) and pushes
    the RESULT. This is the difference between reminding the user to do something and
    Orb doing it and reporting back (the 2026-07-06 gap: schedule_notification only
    pushes the instruction text, it never does the work). time_str: HH:MM or ISO."""
    fire_at = _parse_time(time_str or "")
    if fire_at is None:
        return (f"Could not parse time '{time_str}' — use HH:MM or an ISO datetime "
                "like 2026-07-06T15:00.")
    if not (task or "").strip():
        return "Give me the actual task to run — what should I do at that time?"
    now = datetime.now()
    if fire_at <= now:
        return f"{fire_at.isoformat(timespec='minutes')} is in the past — nothing scheduled."
    _schedule.append(ScheduledItem(
        fire_at=fire_at, kind="task",
        title=(title or "Scheduled task").strip(), body=task.strip(), source="user"))
    _save_plan()
    stamp = f"{fire_at:%a %b} {fire_at.day}, {fire_at:%H:%M}"
    log.info(f"[proactive] task scheduled: '{title}' at {fire_at.isoformat(timespec='minutes')}")
    return (f"Scheduled the task '{(title or 'task').strip()}' to run {stamp} — I'll "
            "actually do it then and send you the result.")


async def _run_scheduled_task(task: str) -> str:
    """DO a scheduled task: run it on Claude with full tool access (reads files,
    analyses, writes) and return the result. Spawned OFF the scheduler tick so a
    long task never blocks the 60s loop. The executor behind schedule_task."""
    import shutil
    if not shutil.which("claude"):
        return "Couldn't run the task — the Claude CLI isn't available."
    model = os.environ.get("JARVIS_TASK_MODEL", "sonnet")
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", task, "--permission-mode", "bypassPermissions", "--model", model,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=desktop)
    except Exception as e:
        return f"Couldn't start the task: {e}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300.0)
        return out.decode("utf-8", "ignore").strip() or "Finished the task, but it produced no output."
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "The task ran past five minutes and I stopped it — it may need to be smaller."
    except Exception as e:
        return f"The task hit an error: {e}"


async def _run_and_push_task(title: str, task: str) -> None:
    """Run a scheduled task and push the REAL result (never just the instruction).
    The full result is also saved to Desktop/JARVIS_Files so it survives the push
    banner's length trim and Orb can read it back later."""
    result = await _run_scheduled_task(task)
    try:
        outdir = os.path.join(os.path.expanduser("~"), "Desktop", "JARVIS_Files")
        os.makedirs(outdir, exist_ok=True)
        safe = "".join(c if (c.isalnum() or c in " -_") else "_"
                       for c in (title or "task"))[:50].strip() or "task"
        with open(os.path.join(outdir, f"orb_task_{safe}.md"), "w", encoding="utf-8") as f:
            f.write(f"# {title}\n\n{result}\n")
    except Exception:
        pass
    if _push_fn:
        await _push_fn(title or "Task done", result, "info",
                       source="scheduled-task", dedupe=False)


async def _apply_plan(plan: dict) -> None:
    """Parse the model's plan output and add items to the schedule. The ONLY
    thing this function is capable of doing is what's read below — that's the
    real safety boundary (see module docstring), not a prompt promise."""
    global _schedule
    now = datetime.now()

    # Clear unfired future items from the OLD PLAN before applying the new one
    # — but only the plan's own: user-scheduled reminders (source="user") are
    # not the model's to clear. Without this guard the 06:00 synthesis would
    # have silently wiped the 21 job-application reminders scheduled 07-03.
    _schedule = [item for item in _schedule if item.fired or item.source == "user"]

    added_notifications = 0
    added_reviews = 0
    suppressed = 0

    # Muted-topics gate (2026-07-20): a durable suppression list the model is
    # told about but which is ALSO enforced here in code — a notification whose
    # title/body hits a muted topic is dropped and logged, never scheduled.
    try:
        from jtools.muted_topics import block_reason as _muted_reason
    except Exception:
        _muted_reason = lambda *a: ""

    for n in plan.get("notifications", []):
        reason = _muted_reason(f"{n.get('title', '')} {n.get('body', '')}")
        if reason:
            suppressed += 1
            log.info(f"[proactive] muted notification dropped ({reason}): {n.get('title', '')[:50]}")
            continue
        fire_at = _parse_time(n.get("time", ""))
        if fire_at and fire_at > now:
            _schedule.append(ScheduledItem(
                fire_at=fire_at, kind="notification",
                title=n.get("title", "JARVIS"),
                body=n.get("body", ""),
                notif_kind=n.get("kind", "info"),
                attach_screenshot=bool(n.get("attach_screenshot", False)),
            ))
            added_notifications += 1

    _reset_daily_extras_if_needed()
    remaining_slots = _MAX_EXTRA_REVIEWS - _extra_reviews_today

    for r in plan.get("additional_reviews", [])[:remaining_slots]:
        fire_at = _parse_time(r.get("time", ""))
        if fire_at and fire_at > now:
            _schedule.append(ScheduledItem(
                fire_at=fire_at, kind="review",
                reason=r.get("reason", ""),
            ))
            added_reviews += 1

    _schedule.sort(key=lambda x: x.fire_at)
    _save_plan()

    # Notes — local, no phone needed, applied immediately (max 5, matches the
    # other list caps in this schema).
    added_notes = 0
    try:
        from jtools.notes_tool import add_note
        for n in plan.get("notes", [])[:5]:
            text = (n.get("text") or "").strip()
            if not text:
                continue
            reason = _muted_reason(text)
            if reason:
                suppressed += 1
                log.info(f"[proactive] muted note dropped ({reason}): {text[:50]}")
                continue
            await add_note(text, tag="proactive")
            added_notes += 1
    except Exception as e:
        log.warning(f"[proactive] note apply failed: {e}")

    # Calendar events — phone-side, queued, applied opportunistically (see
    # queue_calendar_events's docstring for why this can't be immediate).
    added_calendar = queue_calendar_events(plan.get("calendar_events", []))

    log.info(f"[proactive] plan applied: {added_notifications} notifications, {added_reviews} extra "
            f"reviews, {added_notes} notes, {added_calendar} calendar event(s) queued"
            + (f", {suppressed} suppressed (muted topics)" if suppressed else ""))


# ── Event log ─────────────────────────────────────────────────────────────────

def log_event(type_: str, **kwargs) -> None:
    entry = {"ts": datetime.now().isoformat(timespec="seconds"), "type": type_, **kwargs}
    try:
        with _EVENTS_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


def _read_events(since_hours: float) -> list[dict]:
    if not _EVENTS_LOG.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=since_hours)
    out = []
    for line in _EVENTS_LOG.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) >= cutoff:
                out.append(e)
        except Exception:
            pass
    return out


def _trim_events_log(keep_days: int = 7) -> None:
    if not _EVENTS_LOG.exists():
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    kept = [
        line for line in _EVENTS_LOG.read_text(encoding="utf-8").splitlines()
        if line.strip() and datetime.fromisoformat(json.loads(line)["ts"]) >= cutoff
    ]
    _EVENTS_LOG.write_text("\n".join(kept) + "\n", encoding="utf-8")


# ── Context store ─────────────────────────────────────────────────────────────

def update_context(key: str, value: str) -> None:
    ctx: dict = {}
    if _CONTEXT_FILE.exists():
        try:
            ctx = json.loads(_CONTEXT_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    ctx[key] = {"value": value, "updated": datetime.now().isoformat(timespec="seconds")}
    _CONTEXT_FILE.write_text(json.dumps(ctx, indent=2, ensure_ascii=False), encoding="utf-8")


def get_context() -> dict:
    if not _CONTEXT_FILE.exists():
        return {}
    try:
        return json.loads(_CONTEXT_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


# ── Notification log ──────────────────────────────────────────────────────────

def log_notification(title: str, body: str, kind: str, source: str,
                     notif_id: str = "", delivered: bool | None = None,
                     session: str | None = None, topic_key: str = "") -> None:
    """One canonical entry per push. `notif_id` is what the phone's tap feedback
    (/api/notification/feedback) matches on — entries logged without it can never
    learn from opens, which is exactly what happened before 2026-07-01: every
    entry lacked an id, so record_feedback never matched anything. `topic_key` is
    stored so an open can reset that topic's read-the-room backoff (2026-07-14)."""
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "title": title, "body": body, "kind": kind, "source": source,
        "opened": None,
    }
    if notif_id:
        entry["id"] = notif_id
    if delivered is not None:
        entry["delivered"] = delivered
    if session:
        entry["session"] = session
    if topic_key:
        entry["topic_key"] = topic_key
    with _NOTIF_LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def topic_key_for_notif(notif_id: str) -> str:
    """Look up the topic_key logged for a notification id (for engagement reset)."""
    if not notif_id or not _NOTIF_LOG.exists():
        return ""
    for line in reversed(_NOTIF_LOG.read_text(encoding="utf-8").splitlines()):
        try:
            e = json.loads(line)
            if e.get("id") == notif_id:
                return e.get("topic_key", "")
        except Exception:
            pass
    return ""


def record_feedback(notif_id: str, opened: bool) -> None:
    if not _NOTIF_LOG.exists():
        return
    lines = _NOTIF_LOG.read_text(encoding="utf-8").splitlines()
    updated = []
    for l in lines:
        if not l.strip():
            continue
        try:
            e = json.loads(l)
            if e.get("id") == notif_id:
                e["opened"] = opened
                l = json.dumps(e)
        except Exception:
            pass
        updated.append(l)
    _NOTIF_LOG.write_text("\n".join(updated) + "\n", encoding="utf-8")


def _read_notifications(since_hours: float) -> list[dict]:
    if not _NOTIF_LOG.exists():
        return []
    cutoff = datetime.now() - timedelta(hours=since_hours)
    out = []
    for line in _NOTIF_LOG.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) >= cutoff:
                out.append(e)
        except Exception:
            pass
    return out


# ── Git activity ──────────────────────────────────────────────────────────────

def _git_activity(since_hours: float) -> str:
    lines = []
    for name, path in _PROJECT_DIRS:
        p = Path(path) if not isinstance(path, Path) else path
        if not p.is_dir() or not (p / ".git").exists():
            continue
        try:
            r = subprocess.run(
                ["git", "-C", str(p), "log",
                 # int, not float: git's date parser silently matches NOTHING on
                # "--since=6.0 hours ago" — this block was empty on every call
                # until the mind's first wake asserted "no commits" and got
                # fact-checked (2026-07-02).
                f"--since={max(1, int(round(since_hours)))} hours ago",
                 "--oneline", "--no-merges"],
                capture_output=True, text=True, timeout=5,
            )
            commits = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
            if commits:
                lines.append(f"  {name}: {len(commits)} commit(s) — {commits[0][:70]}")
        except Exception:
            pass
    # Other branches/machines, fetched from origin — the iMac's work is
    # invisible to local HEAD. Feeds the mind AND the synthesis (both call
    # this one function).
    remote = _remote_git_activity(since_hours)
    if remote:
        lines.append("  — on other branches/machines (fetched from origin; "
                      "e.g. the iMac's iOS work):")
        lines.append(remote)
    return "\n".join(lines) if lines else "  (no recent commits — local branches or fetched remotes)"


# Throttle the network fetch, not the read: the mind wakes several times a day
# and the synthesis twice — refreshing origin refs every ~20 min is plenty,
# and a skipped fetch still reads the last-fetched refs (slightly stale beats
# blind).
_LAST_REMOTE_FETCH: dict[str, float] = {}
_REMOTE_FETCH_MIN_GAP_S = 20 * 60


def _remote_git_activity(since_hours: float) -> str:
    """Recent commits on remote branches OTHER than the local upstream.

    Exists because of a proven blind spot (2026-07-02): the 18:02 synthesis
    brief claimed 'iOS blockers haven't visibly moved' on a day the iMac
    shipped two TestFlight builds — this PC's git view was local-HEAD-only.
    A failed/offline fetch degrades to whatever refs the last fetch left."""
    lines = []
    since = f"--since={max(1, int(round(since_hours)))} hours ago"
    cutoff = time.time() - since_hours * 3600
    for name, path in _PROJECT_DIRS:
        p = Path(path) if not isinstance(path, Path) else path
        if not p.is_dir() or not (p / ".git").exists():
            continue
        try:
            remotes = subprocess.run(["git", "-C", str(p), "remote"],
                                     capture_output=True, text=True, timeout=5)
            if not remotes.stdout.strip():
                continue
            now_m = time.monotonic()
            if now_m - _LAST_REMOTE_FETCH.get(str(p), 0.0) > _REMOTE_FETCH_MIN_GAP_S:
                _LAST_REMOTE_FETCH[str(p)] = now_m
                subprocess.run(["git", "-C", str(p), "fetch", "--all", "--quiet", "--prune"],
                               capture_output=True, text=True, timeout=25)
            up = subprocess.run(["git", "-C", str(p), "rev-parse", "--abbrev-ref", "@{u}"],
                                capture_output=True, text=True, timeout=5)
            upstream = up.stdout.strip() if up.returncode == 0 else ""
            refs = subprocess.run(
                ["git", "-C", str(p), "for-each-ref", "refs/remotes",
                 "--sort=-committerdate",
                 "--format=%(refname:short)|%(committerdate:unix)"],
                capture_output=True, text=True, timeout=5)
            for ref_line in refs.stdout.strip().splitlines():
                try:
                    ref, unix = ref_line.rsplit("|", 1)
                    if float(unix) < cutoff:
                        break  # sorted newest-first — the rest are older
                except ValueError:
                    continue
                if ref == upstream or ref.endswith("/HEAD"):
                    continue
                lg = subprocess.run(
                    ["git", "-C", str(p), "log", ref, since, "--oneline", "--no-merges"],
                    capture_output=True, text=True, timeout=5)
                commits = [l.strip() for l in lg.stdout.strip().splitlines() if l.strip()]
                if commits:
                    latest = commits[0].split(" ", 1)[-1][:90]
                    lines.append(f"    {name} {ref}: {len(commits)} commit(s) — {latest}")
        except Exception:
            pass
    return "\n".join(lines)


def _uncommitted_work_summary() -> str:
    """Per-project: uncommitted changes + unpushed commits sitting locally.
    Added 2026-07-01 after finding 31 real files of working code sitting
    uncommitted for an extended stretch across this exact set of projects —
    genuinely useful to surface ('you have real work sitting unprotected'),
    zero new dependencies, all local git."""
    lines = []
    for name, path in _PROJECT_DIRS:
        p = Path(path) if not isinstance(path, Path) else path
        if not p.is_dir() or not (p / ".git").exists():
            continue
        try:
            dirty = subprocess.run(
                ["git", "-C", str(p), "status", "--porcelain"],
                capture_output=True, text=True, timeout=5,
            )
            dirty_count = len([l for l in dirty.stdout.splitlines() if l.strip()])
            ahead = subprocess.run(
                ["git", "-C", str(p), "rev-list", "--count", "@{u}..HEAD"],
                capture_output=True, text=True, timeout=5,
            )
            ahead_count = int(ahead.stdout.strip()) if ahead.returncode == 0 and ahead.stdout.strip().isdigit() else 0
            if dirty_count or ahead_count:
                bits = []
                if dirty_count:
                    bits.append(f"{dirty_count} uncommitted file(s)")
                if ahead_count:
                    bits.append(f"{ahead_count} unpushed commit(s)")
                lines.append(f"  {name}: {', '.join(bits)}")
        except Exception:
            pass
    return "\n".join(lines) if lines else "  (everything committed and pushed)"


# ── Failure telemetry ─────────────────────────────────────────────────────────

def _failure_telemetry_block(since_hours: float) -> str:
    """Dropped balls in the window — tool errors, dead/empty turns, timeouts.
    Orb's own introspection ask (2026-07-02): 'tool errors and empty turns
    should be logged somewhere the morning synthesis reads — right now nobody
    counts dropped balls but the user.' Zero-LLM: tallies markers the activity
    log already records."""
    path = _DATA_DIR / "session_activity.jsonl"
    if not path.exists():
        return "  (no activity log)"
    cutoff = datetime.now() - timedelta(hours=since_hours)
    counts: dict[str, int] = {}
    examples: list[str] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                e = json.loads(line)
                if datetime.fromisoformat(e.get("ts", "")) < cutoff:
                    continue
            except Exception:
                continue
            kind = e.get("kind", "")
            text = str(e.get("text") or "")
            fail = None
            if kind == "turn_error":
                fail = "dead_turn"
            elif kind == "tool_result" and text.startswith("[tool error]"):
                fail = f"tool_error:{e.get('tool', '?')}"
            elif kind in ("tool_result", "lifecycle") and "timed out" in text.lower():
                fail = f"timeout:{e.get('tool') or e.get('session') or '?'}"
            if fail:
                counts[fail] = counts.get(fail, 0) + 1
                if len(examples) < 3:
                    examples.append(f"    [{e.get('ts', '')[11:16]}] {fail}: {text[:90]}")
    except Exception:
        return "  (activity log unreadable)"
    if not counts:
        return "  (none — clean window)"
    head = ", ".join(f"{k} ×{v}" for k, v in sorted(counts.items(), key=lambda kv: -kv[1]))
    return "  " + head + ("\n" + "\n".join(examples) if examples else "")


# ── Job application tracker ───────────────────────────────────────────────────

def _job_apps_summary() -> str:
    if not _JOB_APPS_FILE.exists():
        return "  (no applications on file)"
    try:
        apps = json.loads(_JOB_APPS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return "  (could not read)"
    lines = []
    for a in apps:
        try:
            last = datetime.fromisoformat(a.get("last_contact", a.get("applied_date", "2026-01-01")))
            days = (datetime.now() - last).days
        except Exception:
            days = "?"
        status = a.get("status", "applied")
        role   = f" ({a['role']})" if a.get("role") and a["role"] != "Unknown" else ""
        note   = f" — {a['notes']}" if a.get("notes") else ""
        lines.append(f"  {a['company']}{role}: {status}, last contact {days}d ago{note}")
    return "\n".join(lines) if lines else "  (empty)"


def update_job_application(company: str, status: str = "", notes: str = "") -> str:
    """Update a job application record. Called by JARVIS tools or voice commands."""
    try:
        apps = json.loads(_JOB_APPS_FILE.read_text(encoding="utf-8")) if _JOB_APPS_FILE.exists() else []
    except Exception:
        apps = []
    now_iso = datetime.now().strftime("%Y-%m-%d")
    found = False
    for a in apps:
        if a["company"].lower() == company.lower():
            a["last_contact"] = now_iso
            if status:
                a["status"] = status
            if notes:
                a["notes"] = notes
            found = True
            break
    if not found:
        apps.append({"company": company, "role": "Unknown",
                     "applied_date": now_iso, "last_contact": now_iso,
                     "status": status or "applied", "notes": notes})
    _JOB_APPS_FILE.write_text(json.dumps(apps, indent=2, ensure_ascii=False), encoding="utf-8")
    return f"Updated {company}: {status or 'contact logged'}"


# ── Staleness (file mod times) ────────────────────────────────────────────────

def _project_staleness() -> str:
    lines = []
    for name, path in _PROJECT_DIRS:
        p = Path(path) if not isinstance(path, Path) else path
        if p.is_file():
            try:
                days = (time.time() - p.stat().st_mtime) / 86400
                lines.append(f"  {name}: {days:.1f} days ago")
            except Exception:
                pass
        elif p.is_dir():
            try:
                most_recent = max(
                    (f.stat().st_mtime for f in p.rglob("*") if f.is_file()),
                    default=0.0,
                )
                days = (time.time() - most_recent) / 86400
                lines.append(f"  {name}: {days:.1f} days ago")
            except Exception:
                pass
    return "\n".join(lines) if lines else "  (none)"


# ── Fresh external pulls (weather/news) — cheap, keyless, pulled AT review ────
# time rather than hourly-watched like data_watcher's stock targets: these are
# "what's true right now", not a time series to find patterns in.

async def _weather_block() -> str:
    try:
        from jtools.weather_tool import get_weather
        return "  " + (await get_weather()).replace("\n", " ")
    except Exception:
        return "  (unavailable)"


async def _tech_news_block() -> str:
    try:
        from jtools.news_tool import get_news
        return "  " + (await get_news("tech", 3)).replace("\n", " ")
    except Exception:
        return "  (unavailable)"


async def _mail_block(limit: int = 5) -> str:
    """Targeted Gmail summary (off-device data). His inbox runs thousands of
    unread, so recency/unread is pure noise — Gmail's Primary category plus a
    job-signal search is the signal (switched 2026-07-02 after the first live
    run proved 'latest unread' was all newsletters). Dormant until
    GMAIL_APP_PASSWORD exists; IMAP is blocking so it runs threaded."""
    try:
        from jtools.mail_tool import configured, primary_recent, job_signals
        if not configured():
            return "  (not configured — GMAIL_APP_PASSWORD not set; not an event, don't surface it)"
        prim = await asyncio.to_thread(primary_recent, limit)
        jobs = await asyncio.to_thread(job_signals, limit)
    except Exception as e:
        return f"  (unavailable — {str(e)[:80]})"
    lines = ["  PRIMARY inbox, last 24h: " + (f"{len(prim)} message(s)" if prim else "nothing new")]
    lines += [f"    {r['from'][:44]} — {r['subject'][:72]}" for r in prim]
    lines.append("  JOB-HUNT signals, last 48h: " + ("" if jobs else "none"))
    lines += [f"    {r['from'][:44]} — {r['subject'][:72]}" for r in jobs]
    return "\n".join(lines)


def _missions_block() -> str:
    """The user's declared week-scale goals — the interpretive frame the
    synthesis weighs every other data source against. As of 2026-07-20 each
    mission carries a surfacing mode: SILENT missions are background context
    ONLY (never a nudge/note/push, even when stale); only ACTIVE-DRIVE missions
    may be surfaced. The marker is shown so the model honors the distinction."""
    try:
        from jtools.missions_tool import active_missions, render_line, mission_surface, SURFACE_ACTIVE_DRIVE
        acts = active_missions()
    except Exception:
        return "  (unavailable)"
    if not acts:
        return "  (none set — the add_mission tool creates them)"
    lines = ["  • " + render_line(m, show_surface=True) for m in acts[:8]]
    n_drive = sum(1 for m in acts if mission_surface(m) == SURFACE_ACTIVE_DRIVE)
    header = (
        "  RULE: [silent] missions are BACKGROUND CONTEXT ONLY — use them to interpret "
        "what he's doing, but a stale silent mission must NEVER become a notification, "
        "note, or push. Only [ACTIVE-DRIVE] missions may be proactively surfaced."
        f" ({n_drive} active-drive right now.)\n"
    )
    return header + "\n".join(lines)


def _ignored_topics_block(days: float = 7.0, min_sends: int = 3) -> str:
    """Topics the user has been shown repeatedly and NEVER opened — the
    generation-layer 'read the room' signal (2026-07-14). The synthesis must
    not keep manufacturing these; his 07-14 complaint was 231 pushes / 3%
    opened, 'reminds me of the same thing over and over.' Grouped by topic_key
    (new entries) or source/title (older), counting sends vs opens."""
    try:
        rows = _read_notifications(days * 24)
    except Exception:
        return "  (none tracked)"
    agg: dict[str, dict] = {}
    for r in rows:
        key = r.get("topic_key") or r.get("source") or ""
        if not key or key in ("push", "reply-fallback", "api"):
            continue  # skip generic/direct-answer buckets
        a = agg.setdefault(key, {"sends": 0, "opens": 0, "last": ""})
        a["sends"] += 1
        if r.get("opened") is True:
            a["opens"] += 1
        a["last"] = (r.get("body") or "")[:50]
    ignored = [(k, v) for k, v in agg.items()
               if v["sends"] >= min_sends and v["opens"] == 0]
    ignored.sort(key=lambda kv: kv[1]["sends"], reverse=True)
    if not ignored:
        return "  (none — nothing is being over-sent)"
    return "\n".join(
        f"  {k}: sent {v['sends']}x, opened 0x — e.g. \"{v['last']}\"  → STOP re-raising unless it materially escalated"
        for k, v in ignored[:8])


def _notes_block(limit: int = 8) -> str:
    """Recent saved notes, newest first. Past reviews' own notes land in the
    same file, so reading them back also stops duplicate note-saving."""
    try:
        from jtools.notes_tool import recent_notes
        notes = recent_notes(limit)
    except Exception:
        return "  (unavailable)"
    if not notes:
        return "  (none)"
    return "\n".join(
        f"  [{(n.get('ts') or '')[:16]}] {(n.get('text') or '')[:120]}"
        for n in notes[::-1])


def _day_rhythm_block(events: list[dict]) -> str:
    """Distill raw window_changed events into how the period actually went —
    active span, where the time went, what's on screen now. Zero-LLM;
    approximate by nature (dwell is inferred from switch-to-switch gaps,
    discounted by the idle time recorded at each switch)."""
    wins = []
    for e in events:
        if e.get("type") != "window_changed" or not e.get("title"):
            continue
        try:
            wins.append((datetime.fromisoformat(e["ts"]), e))
        except Exception:
            continue
    if len(wins) < 2:
        return "  (not enough window activity in this period)"

    def _app(title: str) -> str:
        t = title.replace("​", "").strip()  # zero-width space, common in Edge titles
        while t and not (t[0].isalnum() or t[0] in "([{\"'"):
            t = t[1:]
        if " - " in t:
            t = t.split(" - ")[-1].strip()
        if t == "Program Manager":  # the desktop shell — also what exclusive-fullscreen games report as
            t = "(desktop / fullscreen app)"
        return (t or "(untitled)")[:48]

    per_app: dict[str, float] = {}
    afk = 0.0
    for (t1, e1), (t2, e2) in zip(wins, wins[1:]):
        idle_next = float(e2.get("idle_before") or 0)
        # A long gap ending with the user still active was one long focus
        # session; a long gap ending idle was mostly AFK — cap it hard.
        cap = 3 * 3600 if idle_next < 300 else 45 * 60
        dt = min(max((t2 - t1).total_seconds(), 0.0), cap)
        active = max(0.0, dt - max(0.0, idle_next - 60.0))
        afk += dt - active
        app = _app(e1.get("title", ""))
        per_app[app] = per_app.get(app, 0.0) + active
    top = sorted(per_app.items(), key=lambda kv: kv[1], reverse=True)
    top_str = "; ".join(f"{name} {secs / 3600:.1f}h" for name, secs in top[:6] if secs >= 300)
    span_h = (wins[-1][0] - wins[0][0]).total_seconds() / 3600
    last_t, last_e = wins[-1]
    return (
        f"  span: {wins[0][0]:%H:%M} → {last_t:%H:%M} (~{span_h:.1f}h, ~{afk / 3600:.1f}h idle/AFK within it)\n"
        f"  where the time went: {top_str or '(nothing over 5 min)'}\n"
        f"  on screen now: {_app(last_e.get('title', ''))} (since {last_t:%H:%M})"
    )


def _backend_health_block() -> str:
    """Is JARVIS's own backend healthy? Tail its own error log for anything
    that looks like a real problem in the review window — self-awareness, not
    just watching everything else."""
    log_path = Path(__file__).parent / "backend_err.log"
    if not log_path.exists():
        return "  (no log file)"
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
        bad = [l for l in lines if "ERROR" in l or "Traceback" in l or "CRITICAL" in l]
        if not bad:
            return "  clean — no errors in recent log tail"
        return f"  {len(bad)} error line(s) in recent log tail — most recent: {bad[-1][:150]}"
    except Exception:
        return "  (unavailable)"


# ── Conversation context injection ───────────────────────────────────────────

def get_conversation_context() -> str:
    """Injected into every conversation system prompt so JARVIS always has
    the latest plan, upcoming schedule, and phone connector data."""
    parts: list[str] = []

    # Morning briefing — the FIRST conversation after 5am following a long
    # overnight quiet opens with the rundown. Self-closing gate: the moment a
    # turn happens, conversation.jsonl's mtime is fresh and the directive
    # stops injecting. (2026-07-02, "the mind gets a mouth" build.)
    try:
        _conv_file = _DATA_DIR / "conversation.jsonl"
        _now_dt = datetime.now()
        _quiet_h = ((time.time() - _conv_file.stat().st_mtime) / 3600
                    if _conv_file.exists() else 99.0)
        if 5 <= _now_dt.hour < 12 and _quiet_h >= 4:
            _bits = []
            if _current_brief:
                _bits.append(f"the current plan brief: {_current_brief[:300]}")
            try:
                import mind as _m_brief
                _mnote = _m_brief.status().get("note_to_next_wake", "")
                if _mnote:
                    _bits.append(f"your mind's overnight note: {_mnote[:220]}")
            except Exception:
                pass
            parts.append(
                "MORNING BRIEFING — this is his first conversation of the day. OPEN with a "
                "short spoken rundown before anything else (~3 sentences, in character): "
                "anything that happened overnight worth knowing, the shape of today, and the "
                "mission countdown. Material: " + ("; ".join(_bits) if _bits else "the sections below")
                + ". The recent-notifications and finished-sessions sections below are also material.")
    except Exception:
        pass

    # Active missions — week-scale goals; keeps every conversation anchored to
    # what he's actually driving at (the "presence, not assistant" bar).
    try:
        from jtools.missions_tool import active_missions, render_line
        acts = active_missions()
        if acts:
            parts.append("ACTIVE MISSIONS:\n" + "\n".join(
                "  • " + render_line(m) for m in acts[:5]))
    except Exception:
        pass

    # Who he is + Orb's own live status — the persistent knowledge base and the
    # project-state file (2026-07-06). about_me.md is Orb's growing profile of
    # the user (edited via remember_about_me); orb_status.md is the live state of
    # Orb's own #1 mission, so it can answer "what's blocking the app?" instead
    # of being blind. Both read fresh each turn.
    try:
        _about = _DATA_DIR / "about_me.md"
        if _about.exists():
            parts.append("WHO HE IS (your standing knowledge — trust it; keep it current "
                         "with remember_about_me):\n"
                         + _about.read_text(encoding="utf-8", errors="replace")[:2800])
    except Exception:
        pass
    try:
        _ostat = _DATA_DIR / "orb_status.md"
        if _ostat.exists():
            parts.append("ORB'S OWN LIVE STATUS (your projects' real state — read it before "
                         "you claim anything about them):\n"
                         + _ostat.read_text(encoding="utf-8", errors="replace")[:1600])
    except Exception:
        pass

    # A moment the mind staged for the next natural opening (single-shot: this
    # injection consumes it). The "I see you had quite the night" mechanic.
    try:
        import mind as _mind
        _staged = _mind.consume_staged()
        if _staged:
            parts.append("FROM YOUR OWN MIND — you noticed this between conversations; "
                         "work it in naturally, once, early in your reply: " + _staged)
    except Exception:
        pass

    # Recent outbound notifications + autonomous activity — so when he taps a
    # notification and asks "did it work?", JARVIS knows what he's talking
    # about (gap found live 2026-07-02: a completion push landed and the
    # conversation had no idea it existed).
    try:
        cutoff = time.time() - 2 * 3600
        recent = []
        for line in _NOTIF_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-30:]:
            try:
                e = json.loads(line)
                if datetime.fromisoformat(e.get("ts", "")).timestamp() > cutoff:
                    recent.append(f"  [{e['ts'][11:16]}] {e.get('title', '')}: {e.get('body', '')[:90]}")
            except Exception:
                continue
        if recent:
            parts.append("NOTIFICATIONS RECENTLY SENT TO HIS PHONE (he may be replying to one):\n"
                         + "\n".join(recent[-4:]))
    except Exception:
        pass
    try:
        import mind as _mind_ctx
        _mstat = _mind_ctx.status()
        _note = _mstat.get("note_to_next_wake", "")
        if _note:
            # Age-stamped like the scan brief: the mind's picture is only as
            # fresh as its last wake (07-02: it had him "at the PC on GTA"
            # hours after he said, on another brain, that he was at the movies).
            _mupd = (_mstat.get("situation_updated_at") or "")[11:16]
            parts.append(
                f"YOUR MIND'S LATEST NOTE (from its last wake{' at ' + _mupd if _mupd else ''} — "
                f"the world may have moved since): {_note[:300]}")
    except Exception:
        pass
    try:
        _done_recent = []
        for p in sorted(_SESSIONS_DIR.glob("*.json"), key=lambda q: q.stat().st_mtime, reverse=True)[:8]:
            if "_inbox" in p.stem:
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8-sig"))
            except Exception:
                continue
            if d.get("status") in ("done", "failed") and d.get("updated", "") >= datetime.fromtimestamp(cutoff).isoformat(timespec="seconds"):
                _done_recent.append(f"  {d.get('session')}: {d.get('status')} — {d.get('message', '')[:90]}")
        if _done_recent:
            parts.append("SESSIONS THAT JUST FINISHED (last 2h):\n" + "\n".join(_done_recent[:4]))
    except Exception:
        pass

    # His other AI conversations on this PC (Claude Code / Codex / Grok CLI
    # transcripts, zero-LLM scan, 2026-07-10 — his ask: Orb should have context
    # over the manual chats too). Awareness, not instructions: these are
    # SEPARATE conversations he had with other tools, not part of this one.
    try:
        from jtools.cli_chats_tool import context_block as _cli_ctx
        _cli = _cli_ctx(hours=12.0, max_items=3)
        if _cli:
            parts.append(
                "HIS OTHER AI-CLI SESSIONS ON THIS PC (last 12h — separate conversations "
                "he had with Claude Code/Codex/Grok; context about his work, not part of "
                "THIS conversation; read_cli_chat digs into one):\n" + _cli)
    except Exception:
        pass

    # Active plan brief
    if _PLAN_FILE.exists():
        try:
            plan = json.loads(_PLAN_FILE.read_text(encoding="utf-8"))
            brief = plan.get("brief", "").strip()
            if brief:
                # Age-stamped: the fable brain repeated this brief's
                # "uncommitted work" claim as CURRENT fact 19 min after the
                # commit landed (07-02) — a brief describes ITS moment, not now.
                _saved = (plan.get("saved_at") or "")[11:16]
                parts.append(
                    f"LAST SCAN BRIEF (from {_saved or 'earlier'} — it describes THEN, "
                    f"not now; re-verify perishable claims like uncommitted work or "
                    f"running jobs before repeating them as current): {brief}")
            # Next 3 scheduled items
            now = datetime.now()
            upcoming = [
                i for i in plan.get("items", [])
                if not i.get("fired") and i.get("kind") == "notification"
                and datetime.fromisoformat(i["fire_at"]) > now
            ][:3]
            if upcoming:
                lines = [f"  {i['fire_at'][11:16]} — {i['title']}: {i['body'][:60]}" for i in upcoming]
                parts.append("UPCOMING SCHEDULED:\n" + "\n".join(lines))
        except Exception:
            pass

    # Phone connector context (calendar, reminders, contacts)
    if _connector_context:
        age = f" (as of {_connector_updated_at})" if _connector_updated_at else ""
        parts.append(f"PHONE CONTEXT{age}:\n{_connector_context[:800]}")

    # Pending proposals awaiting approval
    if _PROPOSALS_FILE.exists():
        try:
            proposals = json.loads(_PROPOSALS_FILE.read_text(encoding="utf-8"))
            pending = [p for p in proposals if p.get("status") == "pending"]
            if pending:
                lines = [f"  [{p['id']}] {p['title']}: {p['description'][:80]}" for p in pending[:3]]
                parts.append("PENDING PROPOSALS (awaiting your approval):\n" + "\n".join(lines))
        except Exception:
            pass

    # Clipboard — last copied content (short things only, skip large code blocks)
    try:
        from jtools.clipboard_watcher import get_last as _clip_last
        clip = _clip_last()
        if clip and len(clip) < 600:
            parts.append(f"CLIPBOARD: {clip[:300]}")
    except Exception:
        pass

    if not parts:
        return ""
    return "\n\n".join(parts)


# ── Calendar-event queue (phone-side action, applied opportunistically) ───────
# The synthesis review can decide a real calendar event/reminder is worth
# adding, but it runs on a fixed clock schedule (6am/6pm) with NO guarantee a
# phone is actually connected at that moment — most likely NOT at 6am while
# ninjahawk is asleep. So these are queued here, not applied live during the
# review, and get applied the next time a phone connection's remote_dispatch
# becomes available (server_win.py calls apply_pending_calendar_actions() on
# every {type:"hello"}). Every application fires a confirming notification —
# "let it know everything" (ninjahawk, 2026-07-01) — never silent.

def _load_pending_calendar() -> list[dict]:
    if not _PENDING_CALENDAR_FILE.exists():
        return []
    try:
        return json.loads(_PENDING_CALENDAR_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _save_pending_calendar(items: list[dict]) -> None:
    try:
        _PENDING_CALENDAR_FILE.write_text(
            json.dumps(items, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def queue_calendar_events(events: list[dict]) -> int:
    """Add review-proposed calendar events to the pending queue. Returns how
    many were actually queued (capped, so a malformed model output can't spam
    the calendar the moment a phone connects)."""
    pending = _load_pending_calendar()
    added = 0
    for e in events[:5]:
        title = (e.get("title") or "").strip()
        when = (e.get("time") or "").strip()
        if not title or not when:
            continue
        pending.append({
            "title": title, "time": when, "details": (e.get("details") or "").strip(),
            "queued_at": datetime.now().isoformat(timespec="seconds"),
        })
        added += 1
    if added:
        _save_pending_calendar(pending)
    return added


async def apply_pending_calendar_actions(dispatch_fn: Callable) -> None:
    """Called by server_win.py whenever a phone connection becomes available
    (on {type:"hello"}). Applies every queued calendar event through the SAME
    remote_dispatch relay a live conversational turn would use, then clears
    the queue and pushes one confirming notification per item — transparent,
    never silent, even though nobody was there to approve it in the moment."""
    pending = _load_pending_calendar()
    if not pending:
        return
    log.info(f"[proactive] applying {len(pending)} queued calendar action(s)")
    _save_pending_calendar([])  # clear immediately — don't double-apply if this races
    for item in pending:
        try:
            result = await dispatch_fn("add_calendar_event", {
                "title": item["title"], "time": item["time"],
            })
            if _push_fn:
                # dedupe=False: a receipt for a REAL action must never be
                # suppressed — the 07-03 ~01:55 real apply's confirmation was
                # eaten by the 8h content cooldown armed by the 23:01 fake-
                # phone test push (identical title+body → identical hash).
                await _push_fn(
                    "JARVIS added to your calendar",
                    f"{item['title']} — {item['time']}", "info", dedupe=False)
            log.info(f"[proactive] calendar event applied: {item['title']} ({item['time']}) -> {str(result)[:100]}")
        except Exception as e:
            log.warning(f"[proactive] calendar event apply failed for {item.get('title')}: {e}")
            if _push_fn:
                await _push_fn(
                    "JARVIS couldn't add a calendar event",
                    f"{item.get('title', '?')} — {e}", "alert", dedupe=False)


# ── Proposals (self-modification suggestions) ─────────────────────────────────

def _save_proposals(proposals: list[dict]) -> None:
    _PROPOSALS_FILE.write_text(
        json.dumps(proposals, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _load_proposals() -> list[dict]:
    if not _PROPOSALS_FILE.exists():
        return []
    try:
        return json.loads(_PROPOSALS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def _apply_proposals(new_proposals: list[dict]) -> None:
    """Merge new proposals into the proposals file, avoid duplicates by title."""
    existing = _load_proposals()
    existing_titles = {p["title"] for p in existing}
    import uuid
    added = 0
    for p in new_proposals:
        if p.get("title") in existing_titles:
            continue
        existing.append({
            "id": uuid.uuid4().hex[:8],
            "title": p.get("title", "Untitled"),
            "description": p.get("description", ""),
            "rationale": p.get("rationale", ""),
            "requires": p.get("requires", []),
            "status": "pending",
            "created_at": datetime.now().isoformat(timespec="seconds"),
            # Mind proposals may carry an executable payload — on approval the
            # respond endpoint runs exactly what was approved (see server_win).
            **({"action": p["action"]} if isinstance(p.get("action"), dict) else {}),
        })
        existing_titles.add(p["title"])
        added += 1
    if added:
        _save_proposals(existing)
        log.info(f"[proactive] {added} new proposal(s) added")


def approve_proposal(proposal_id: str) -> dict:
    """Called when user approves a proposal. Returns the proposal dict."""
    proposals = _load_proposals()
    for p in proposals:
        if p["id"] == proposal_id:
            p["status"] = "approved"
            p["approved_at"] = datetime.now().isoformat(timespec="seconds")
            _save_proposals(proposals)
            return p
    return {}


def reject_proposal(proposal_id: str) -> None:
    proposals = _load_proposals()
    for p in proposals:
        if p["id"] == proposal_id:
            p["status"] = "rejected"
            # Stamped so "recently decided" context (synthesis) can window on
            # decision time, not creation time.
            p["rejected_at"] = datetime.now().isoformat(timespec="seconds")
            _save_proposals(proposals)
            return


_PROPOSAL_REMIND_AFTER_H = 24.0


async def _remind_stale_proposals() -> None:
    """One follow-up nudge per proposal that's sat pending >24h unacknowledged,
    so it doesn't silently rot without ever reaching the user at an actionable
    moment. `reminded_at` on the proposal dict guarantees one reminder max —
    it's set BEFORE the push, so even a failed push never retries (silence over
    a repeat). The push rides _push_fn, which owns topic dedup + the log entry.
    """
    if not _push_fn:
        return
    proposals = _load_proposals()
    now = datetime.now()
    changed = False
    for p in proposals:
        if p.get("status") != "pending" or p.get("reminded_at"):
            continue
        try:
            created = datetime.fromisoformat(p["created_at"])
        except (KeyError, ValueError, TypeError):
            continue
        age_h = (now - created).total_seconds() / 3600
        if age_h < _PROPOSAL_REMIND_AFTER_H:
            continue
        p["reminded_at"] = now.isoformat(timespec="seconds")
        changed = True
        log.info(f"[proactive] stale-proposal reminder [{p['id']}]: {p.get('title', '?')}")
        try:
            await _push_fn(
                "Still waiting on a yes/no",
                f"“{p.get('title', 'Untitled')}” has been pending "
                f"{int(age_h // 24)} day(s). Yes or no?",
                "info",
                topic=f"proposal_reminder_{p['id']}",
                source="proposal_reminder",
            )
        except Exception as e:
            log.warning(f"[proactive] stale-proposal reminder push failed [{p['id']}]: {e}")
    if changed:
        _save_proposals(proposals)


# ── Direct-fire alerts (no AI) ────────────────────────────────────────────────

_LAST_ALERT: dict[str, float] = {}
_STARTED_AT = time.time()
# Boot grace: a fresh install (or any restart) must not greet its owner with an
# instant "disk 92% full" nag — the 2026-07-14 stranger-gauntlet finding (the
# clone pushed a disk alert 30s after first boot). Direct-fire alerts hold off
# until the backend has been up a while; a condition that still matters will
# still be true when the grace ends.
_ALERT_BOOT_GRACE_S = int(os.getenv("JARVIS_ALERT_BOOT_GRACE_S")
                          or os.getenv("ORB_ALERT_BOOT_GRACE_S") or "600")


def _alert_cooldown(key: str, min_seconds: int = 1800) -> bool:
    return (time.time() - _LAST_ALERT.get(key, 0)) < min_seconds


async def _direct_push(title: str, body: str, kind: str, key: str) -> None:
    if not _push_fn:
        return
    # Muted-topics gate (2026-07-20): even zero-AI direct alerts honor the mute
    # list — the disk alert is a muted topic, so it will never push from here.
    try:
        from jtools.muted_topics import block_reason as _muted_reason
        reason = _muted_reason(key, title, body)
    except Exception:
        reason = ""
    if reason:
        log.info(f"[proactive] direct push suppressed (muted: {reason}) [{key}] {body[:40]}")
        return
    if (time.time() - _STARTED_AT) < _ALERT_BOOT_GRACE_S:
        log.info(f"[proactive] boot-grace ({_ALERT_BOOT_GRACE_S}s): "
                 f"suppressed [{key}] {body[:40]}")
        return
    _LAST_ALERT[key] = time.time()
    log.info(f"[proactive] direct push [{key}]: {body[:60]}")
    # _push_fn (server_win._push_notification) owns the canonical notification-log
    # entry (with the id the phone's tap feedback needs) and a topic-level dedup —
    # the alert key doubles as the topic.
    await _push_fn(title, body, kind, topic=key, source="direct")


# ── Cheap detectors ───────────────────────────────────────────────────────────

class _State:
    window_title: str = ""
    prev_idle: float = 0.0
    last_cpu: float = 0.0
    last_disk_pct: float = 0.0
    screen_hash: str = ""
    screen_hash_changed_at: float = 0.0
    screen_settled_logged: str = ""

_st = _State()


def _get_window_title() -> str:
    try:
        import win32gui
        return win32gui.GetWindowText(win32gui.GetForegroundWindow())
    except Exception:
        return ""


def _screen_hash_quick(data: bytes) -> str:
    import hashlib
    try:
        return hashlib.md5(data).hexdigest()
    except Exception:
        return ""


async def _tick() -> None:
    try:
        from jtools.idle_tracker import idle_seconds
        idle = idle_seconds()
    except Exception:
        idle = 0

    # Presence transition: back after a long absence → poke the mind (it decides
    # whether waking early is warranted — staged moments waiting, stale situation).
    if _st.prev_idle > 10800 and idle < 120:
        try:
            import mind as _mind
            _mind.on_user_return(_st.prev_idle)
        except Exception:
            pass
    _st.prev_idle = idle

    # Window title change → log with parsed context
    title = _get_window_title()
    if title and title != _st.window_title:
        event_extra: dict = {"title": title, "idle_before": round(idle)}

        # Detect browser and extract page title
        for suffix in (" - Google Chrome", " - Microsoft Edge", " - Firefox", " - Arc", " - Brave"):
            if title.endswith(suffix):
                page = title[: -len(suffix)].strip()
                event_extra["browser"] = suffix.strip(" -")
                event_extra["page"] = page
                break

        # Detect a game session
        if "RuneLite" in title or "Old School RuneScape" in title:
            if "runelite" not in _st.window_title.lower() and "old school" not in _st.window_title.lower():
                log_event("game_session", action="started")

        log_event("window_changed", **event_extra)
        _st.window_title = title

    # System health → direct alert if critical
    try:
        cpu = psutil.cpu_percent(interval=0)
        if cpu > 90 and (cpu - _st.last_cpu) > 20 and not _alert_cooldown("cpu"):
            log_event("system_alert", event=f"CPU spike: {cpu:.0f}%")
            await _direct_push("JARVIS", f"CPU at {cpu:.0f}%", "alert", "cpu")
        _st.last_cpu = cpu
    except Exception:
        pass

    try:
        disk = psutil.disk_usage("C:\\").percent
        if disk > 90 and disk > _st.last_disk_pct + 2 and not _alert_cooldown("disk"):
            log_event("system_alert", event=f"Disk {disk:.0f}% full")
            await _direct_push("JARVIS", f"C: drive is {disk:.0f}% full", "alert", "disk")
        _st.last_disk_pct = disk
    except Exception:
        pass

    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=2,
        )
        temp = int(r.stdout.strip())
        if temp > 85 and not _alert_cooldown("gpu_temp"):
            log_event("system_alert", event=f"GPU temp: {temp}°C")
            await _direct_push("JARVIS", f"GPU at {temp}°C", "alert", "gpu_temp")
    except Exception:
        pass

    # Screen settle → log (only while user is at the PC)
    if idle < 1200:
        try:
            import screen_bridge
            data = screen_bridge.capture_jpeg()
            h = _screen_hash_quick(data) if data else ""
            if h:
                if h != _st.screen_hash:
                    _st.screen_hash = h
                    _st.screen_hash_changed_at = time.time()
                else:
                    settled_for = time.time() - _st.screen_hash_changed_at
                    if (settled_for >= 30
                            and _st.screen_settled_logged != h
                            and _st.screen_hash_changed_at > 0):
                        log_event("screen_settled",
                                  window=_st.window_title,
                                  settled_after_seconds=round(settled_for),
                                  idle_seconds=round(idle))
                        _st.screen_settled_logged = h
        except Exception:
            pass


# ── Scheduler loop (runs every 60s, zero AI) ──────────────────────────────────

async def _run_scheduler() -> None:
    """Fire scheduled plan items at the right time."""
    global _extra_reviews_today
    now = datetime.now()
    _reset_daily_extras_if_needed()

    try:
        from jtools.activity_log import trim as _trim_activity
        _trim_activity()
    except Exception:
        pass

    # The mind's own clock (AGENCY.md) — fires when its self-scheduled wake is due.
    try:
        import mind as _mind
        _mind.tick()
    except Exception:
        pass

    try:
        await _remind_stale_proposals()
    except Exception as e:
        log.warning(f"[scheduler] stale-proposal check failed: {e}")

    # Read-the-room: scan his live AI-CLI sessions for signs he's stuck/worn down.
    # Zero-LLM, self-throttled to a few-minute cadence. Never pushes — on a genuine
    # acute episode it logs an event and asks the mind to wake and decide.
    try:
        from jtools import frustration_signal
        await frustration_signal.check_and_signal()
    except Exception as e:
        log.warning(f"[scheduler] frustration check failed: {e}")

    # Continuous session watch (2026-07-20): the fast lane between the 6h scans —
    # a lightweight zero-LLM pass over ALL live CLI sessions that refreshes the
    # comprehensive live-session snapshot the mind reads (context-first) and, on a
    # session newly stuck/actionable, asks the mind to look. Never pushes.
    try:
        from jtools import session_watch
        await session_watch.watch_pass()
    except Exception as e:
        log.warning(f"[scheduler] session watch failed: {e}")

    for item in _schedule:
        if item.fired or item.fire_at > now:
            continue
        item.fired = True

        if item.kind == "notification":
            log.info(f"[scheduler] firing notification: {item.title}")
            if item.attach_screenshot and _screenshot_fn:
                try:
                    sent = await _screenshot_fn()
                    log.info(f"[scheduler] screenshot attach {'sent' if sent else 'skipped (no phone connected)'}")
                except Exception as e:
                    log.warning(f"[scheduler] screenshot attach failed: {e}")
            if _push_fn:
                await _push_fn(item.title, item.body, item.notif_kind, source="scheduled")

        elif item.kind == "task":
            # ACTUALLY do the work (read/analyse/write on Claude) and push the real
            # result — not just the instruction text. Spawned off the tick so a long
            # task never blocks the scheduler; refs kept so it isn't GC'd.
            log.info(f"[scheduler] running scheduled task: {item.title}")
            _t = asyncio.create_task(_run_and_push_task(item.title, item.body))
            _PENDING_TASKS.add(_t)
            _t.add_done_callback(_PENDING_TASKS.discard)

        elif item.kind == "review":
            _reset_daily_extras_if_needed()
            if _extra_reviews_today < _MAX_EXTRA_REVIEWS:
                _extra_reviews_today += 1
                log.info(f"[scheduler] self-scheduled review ({_extra_reviews_today}/{_MAX_EXTRA_REVIEWS}): {item.reason}")
                since = min((time.time() - _last_review_at) / 3600, 24.0) if _last_review_at else 6.0
                await _run_review(since_hours=since, label="self-scheduled", use_synthesis=False)
            else:
                log.info(f"[scheduler] extra review budget exhausted ({_MAX_EXTRA_REVIEWS}/day), skipping")

    _save_plan()


# ── Review (one AI call) ──────────────────────────────────────────────────────

async def _maybe_synthesis_review() -> None:
    """Fires once per calendar day at each configured hour in _SYNTHESIS_HOURS
    (default 6am + 6pm) — FIXED CLOCK TIMES, not a rolling interval, so it's
    genuinely "twice a day at these times", not "every 12h from whenever the
    server happened to start". Matches hour, not exact minute (same pattern
    the old morning-brief check used) — the 60s scheduler cadence means this
    reliably fires within the first minute of the hour, and being lenient
    about the exact minute means a brief restart right at 06:00 doesn't cause
    a missed day.

    Runs the STRONG (Sonnet) synthesis brain — see wire()'s synthesis_brain_fn.
    """
    global _last_review_at
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    if now.hour not in _SYNTHESIS_HOURS:
        return
    if _synthesis_fired_dates.get(now.hour) == today:
        return
    _synthesis_fired_dates[now.hour] = today
    # Cover everything since the last synthesis, not a hardcoded 12h — handles
    # custom/uneven _SYNTHESIS_HOURS spacing and a backend that was down for a
    # stretch (capped so a long outage doesn't pull an absurd events window).
    since_hours = min((time.time() - _last_review_at) / 3600, 30.0) if _last_review_at else 24.0
    _last_review_at = time.time()
    log.info(f"[proactive] {now.hour:02d}:00 synthesis review — covering last {since_hours:.1f}h")
    await _run_review(since_hours=since_hours, label=f"{now.hour:02d}:00 synthesis", use_synthesis=True)


async def run_synthesis_now(label: str = "manual synthesis") -> None:
    """The SAME run the fixed-clock scheduler fires at 6am/6pm — same
    since-hours math, same strong brain, same real plan application — for
    on-demand firing (testing, or 'run the synthesis now' by voice later).
    Updates _last_review_at like a normal synthesis, so the next scheduled
    run covers from here; does NOT consume a _SYNTHESIS_HOURS slot."""
    global _last_review_at
    since_hours = min((time.time() - _last_review_at) / 3600, 30.0) if _last_review_at else 24.0
    _last_review_at = time.time()
    log.info(f"[proactive] {label} — covering last {since_hours:.1f}h")
    await _run_review(since_hours=since_hours, label=label, use_synthesis=True)


# ── Hard-recurring 6h high-bar scan ───────────────────────────────────────────

def six_hour_status() -> dict:
    """Live state of the hard-recurring 6h scan (for status APIs + tests)."""
    next_ts = (_last_six_hour_at + _SIX_HOUR_INTERVAL_S) if _last_six_hour_at else None
    due = (not _last_six_hour_at) or (time.time() - _last_six_hour_at >= _SIX_HOUR_INTERVAL_S)
    return {
        "enabled": _SIX_HOUR_ENABLED,
        "interval_h": round(_SIX_HOUR_INTERVAL_S / 3600.0, 2),
        "last_at": (datetime.fromtimestamp(_last_six_hour_at)
                    .isoformat(timespec="minutes") if _last_six_hour_at else None),
        "next_at": (datetime.fromtimestamp(next_ts)
                    .isoformat(timespec="minutes") if next_ts else None),
        "due": bool(due and _SIX_HOUR_ENABLED),
    }


async def _run_high_bar_six_hour_scan(label: str = "6h scan") -> str:
    """Quiet high-bar 6h check. Zero-LLM by default so the cadence never depends
    on Claude credits or a task re-arming itself. Writes the proactive surface;
    pushes only for genuinely time-critical unmuted items (disk never pushes)."""
    try:
        from jtools.muted_topics import block_reason as _muted_reason
    except Exception:
        def _muted_reason(*_a, **_k):
            return ""

    findings: list[str] = []
    push_candidates: list[tuple[str, str]] = []

    # Live work / CLI sessions (context-first signal)
    try:
        from jtools import session_watch
        block = session_watch.live_sessions_block()
        if block and block.strip():
            findings.append("LIVE WORK:\n" + block.strip()[:900])
    except Exception as e:
        log.debug(f"[6h-scan] session_watch: {e}")

    # Frustration / stuck signals
    try:
        from jtools.frustration_signal import signal_block
        fb = (signal_block() or "").strip()
        if fb:
            findings.append(fb[:500])
    except Exception as e:
        log.debug(f"[6h-scan] frustration: {e}")

    # Missions = silent context only (never a push trigger from this scan)
    try:
        from jtools.missions_tool import active_missions, render_line
        acts = active_missions()
        if acts:
            findings.append("MISSIONS (silent context):\n" + "\n".join(
                "  • " + render_line(m) for m in acts[:5]))
    except Exception:
        pass

    # Upcoming schedule (next 12h) — surface on the tab, push only if overdue
    # user reminder that still matters and is not muted.
    now = datetime.now()
    horizon = now + timedelta(hours=12)
    soon = sorted(
        (i for i in _schedule
         if not i.fired and i.kind == "notification" and now < i.fire_at <= horizon),
        key=lambda i: i.fire_at,
    )
    if soon:
        findings.append("UPCOMING (12h):\n" + "\n".join(
            f"  {i.fire_at:%H:%M}  {i.title}" for i in soon[:8]))

    # Past-due unfired user notifications (backend was off at fire time) —
    # these are the rare case that can meet the push bar.
    overdue = [
        i for i in _schedule
        if not i.fired and i.kind == "notification" and i.fire_at <= now
        and i.source == "user"
    ]
    for i in overdue[:3]:
        text = f"{i.title} {i.body}"
        if _muted_reason(text):
            continue
        # Disk never pushes (hard rule)
        if "disk" in text.lower():
            continue
        push_candidates.append((
            i.title or "Overdue reminder",
            (i.body or f"Was due {i.fire_at:%H:%M}")[:240],
        ))

    if _connector_updated_at:
        findings.append(f"Phone connector context as of {_connector_updated_at}")
    else:
        findings.append("Phone connector context: none this session")

    # Failed sessions in the last few hours that might need a quiet note
    try:
        if _SESSIONS_DIR.exists():
            fails = []
            for p in _SESSIONS_DIR.glob("*.json"):
                if "_inbox" in p.stem:
                    continue
                try:
                    d = json.loads(p.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                if str(d.get("status", "")).lower() not in ("failed", "error"):
                    continue
                msg = str(d.get("message", ""))[:120]
                if _muted_reason(f"{d.get('session','')} {msg}"):
                    continue
                fails.append(f"{d.get('session','?')}: {msg}")
            if fails:
                findings.append("RECENT FAILURES:\n" + "\n".join(f"  • {f}" for f in fails[:5]))
    except Exception:
        pass

    # Write proactive surface (in-app tab / status — not a push)
    stamp = now.isoformat(timespec="minutes")
    next_due = (now + timedelta(seconds=_SIX_HOUR_INTERVAL_S)).isoformat(timespec="minutes")
    body = (
        f"# Proactive tab — high-bar 6h scan\n\n"
        f"**Last scan:** {stamp}  ·  **label:** {label}\n"
        f"**Next automatic scan:** {next_due}  "
        f"(hard-recurring every {_SIX_HOUR_INTERVAL_S/3600:.1f}h — engine-owned, cannot silently die)\n\n"
    )
    if findings:
        body += "## Findings\n\n" + "\n\n".join(findings) + "\n\n"
    else:
        body += "_Quiet pass — nothing material._\n\n"
    body += (
        "## Delivery law\n"
        "- ~80% of updates stay on this surface only.\n"
        "- Push only for rare time-critical actionable items.\n"
        "- Disk alerts never push. Muted topics never re-raised.\n"
        "- Missions are silent context, not nag triggers.\n"
    )
    try:
        tab = _DATA_DIR / "proactive_tab.md"
        tab.write_text(body, encoding="utf-8")
    except Exception as e:
        log.warning(f"[6h-scan] could not write proactive_tab.md: {e}")

    # High-bar pushes only
    pushed = 0
    if _push_fn and push_candidates:
        for title, pbody in push_candidates[:1]:  # at most one per scan
            if _muted_reason(f"{title} {pbody}"):
                continue
            try:
                await _push_fn(title, pbody, "info",
                               topic="six_hour_scan", source="six_hour_scan",
                               dedupe=True)
                pushed += 1
            except Exception as e:
                log.warning(f"[6h-scan] push failed: {e}")

    if pushed:
        result = f"scan ok — wrote proactive tab; pushed {pushed} overdue item(s)"
    elif findings:
        result = f"scan ok — {len(findings)} finding group(s) on proactive tab; nothing worth pushing"
    else:
        result = "nothing worth surfacing"
    log_event("six_hour_scan", result=result[:300], findings=len(findings),
              pushed=pushed, next_in_h=round(_SIX_HOUR_INTERVAL_S / 3600.0, 2))
    return result


async def _maybe_six_hour_scan() -> None:
    """Hard-recurring 6h high-bar scan. Stamps the clock BEFORE running so a
    mid-run crash still leaves the chain armed (unlike the old self-reschedule
    task which died when Claude failed to book the next hop)."""
    global _last_six_hour_at
    if not _SIX_HOUR_ENABLED:
        return
    now = time.time()
    if _last_six_hour_at and (now - _last_six_hour_at) < _SIX_HOUR_INTERVAL_S:
        return
    # Stamp first — this is the whole reliability fix.
    _last_six_hour_at = now
    try:
        _save_plan()
    except Exception:
        pass
    log.info(
        "[proactive] 6h high-bar scan firing (hard-recurring; next in %.1fh)",
        _SIX_HOUR_INTERVAL_S / 3600.0,
    )
    try:
        result = await _run_high_bar_six_hour_scan(label="scheduled 6h scan")
        log.info(f"[proactive] 6h scan: {result[:200]}")
    except Exception as e:
        # Chain stays armed regardless — next fire is still +interval from stamp.
        log.warning(f"[proactive] 6h scan failed (chain still armed): {e}")


async def run_six_hour_scan_now(label: str = "manual 6h scan") -> str:
    """Force-fire the 6h high-bar scan (testing / on-demand). Updates the
    hard-recurring clock so the next automatic run is +interval from now."""
    global _last_six_hour_at
    _last_six_hour_at = time.time()
    try:
        _save_plan()
    except Exception:
        pass
    log.info(f"[proactive] {label} (forced)")
    return await _run_high_bar_six_hour_scan(label=label)


async def _run_review(since_hours: float, label: str, use_synthesis: bool = False) -> None:
    """use_synthesis=True: the big twice-daily call (6am/6pm) — stronger model
    (Sonnet, via synthesis_brain_fn), more data, longer timeout, allowed to
    look further ahead. False: a cheap self-scheduled extra (Haiku router,
    smaller/faster) — still useful for a quick mid-period check, not meant to
    re-derive everything from scratch."""
    brain_fn = _synthesis_brain_fn if use_synthesis else _smart_brain_fn
    if not brain_fn or not _push_fn:
        return

    log.info(f"[proactive] running {label} review (synthesis={use_synthesis})")
    events  = _read_events(since_hours=since_hours)
    notifs  = _read_notifications(since_hours=since_hours * 2)
    ctx     = get_context()

    staleness_block = _project_staleness()
    git_block       = _git_activity(since_hours)
    job_apps_block  = _job_apps_summary()

    # Clipboard snapshot for review
    try:
        from jtools.clipboard_watcher import get_last as _clip_last
        clip_preview = _clip_last()[:300] if _clip_last() else "(empty)"
    except Exception:
        clip_preview = "(unavailable)"

    # Training / background sessions
    sessions = []
    if _SESSIONS_DIR.exists():
        for p in _SESSIONS_DIR.glob("*.json"):
            if "_inbox" in p.stem:
                continue
            try:
                d = json.loads(p.read_text(encoding="utf-8-sig"))
                if d.get("type") != "cc":
                    sessions.append(
                        f"  {d.get('session','?')}: {d.get('status','?')} — {d.get('message','')[:60]}"
                    )
            except Exception:
                pass

    event_types: dict[str, int] = {}
    interesting: list[str] = []
    for e in events:
        t = e.get("type", "?")
        event_types[t] = event_types.get(t, 0) + 1
        if t in ("system_alert", "screen_settled"):
            interesting.append(f"  [{e.get('ts','')}] {t}: {json.dumps({k:v for k,v in e.items() if k not in ('ts','type')})}")

    notif_lines = [
        f"  [{n.get('ts','')}] {n.get('title')}: {n.get('body','')[:50]} → {'opened' if n.get('opened') else 'dismissed' if n.get('opened') is False else '?'}"
        for n in notifs[-20:]
    ]
    ctx_lines = [f"  {k}: {v['value'] if isinstance(v,dict) else v}" for k,v in ctx.items()]
    event_count_lines = "\n".join(f"  {k}: {v}" for k,v in sorted(event_types.items()))

    # Browser/game session events
    browser_events = [e for e in events if e.get("type") == "window_changed" and "browser" in e]
    game_events    = [e for e in events if e.get("type") == "game_session"]
    browser_block  = "\n".join(f"  [{e['ts'][11:16]}] {e.get('browser','')}: {e.get('page','')[:80]}" for e in browser_events[-10:]) or "  (none)"
    game_block     = f"  {len(game_events)} session(s) started" if game_events else "  (none)"
    try:
        from jtools.data_watcher import recent_summary as _watched_summary
        watched_block = _watched_summary(hours=max(since_hours, 24.0))
    except Exception:
        watched_block = "  (unavailable)"
    uncommitted_block = _uncommitted_work_summary()
    missions_block    = _missions_block()
    rhythm_block      = _day_rhythm_block(events)
    notes_block       = _notes_block()
    ignored_block     = _ignored_topics_block()
    try:
        from jtools.muted_topics import render_block as _muted_render
        muted_block = _muted_render()
    except Exception:
        muted_block = "  (unavailable)"
    try:
        from jtools.prefs_tool import preferences_system_block as _prefs_block
        prefs_block = _prefs_block() or "  (none)"
    except Exception:
        prefs_block = "  (unavailable)"
    try:
        from jtools.cli_chats_tool import synthesis_block as _cli_chats
        cli_chats_block = _cli_chats(hours=max(since_hours, 12.0))
    except Exception:
        cli_chats_block = "  (unavailable)"
    try:
        from jtools.frustration_signal import signal_block as _frustration_block
        frustration_block = _frustration_block() or "  (nothing — his sessions read calm)"
    except Exception:
        frustration_block = "  (unavailable)"
    try:
        from jtools.session_watch import live_sessions_block as _live_block
        live_sessions = _live_block() or "  (no live sessions right now)"
    except Exception:
        live_sessions = "  (unavailable)"
    weather_block      = await _weather_block() if use_synthesis else "  (skipped — extra review, not synthesis)"
    news_block          = await _tech_news_block() if use_synthesis else "  (skipped — extra review, not synthesis)"
    mail_block          = await _mail_block() if use_synthesis else "  (skipped — extra review, not synthesis)"
    backend_health_block = _backend_health_block()
    failures_block       = _failure_telemetry_block(since_hours)

    conn_ctx_block    = f"\n== PHONE CONNECTORS (as of {_connector_updated_at}) ==\n{_connector_context[:1200]}" if _connector_context else "\n== PHONE CONNECTORS ==\n  (none — phone hasn't sent context yet, or it's asleep right now — don't assume stale calendar data is still accurate)"
    now_str           = datetime.now().strftime("%A %B %d %Y, %H:%M")
    try:
        _disk_pct = psutil.disk_usage("C:\\").percent
        _cpu_pct  = psutil.cpu_percent(interval=0.2)
        live_stats_block = f"  disk: {_disk_pct:.0f}% full  |  cpu: {_cpu_pct:.0f}%"
    except Exception:
        live_stats_block = "  (unavailable)"
    next_synth        = ", ".join(f"{h:02d}:00" for h in _SYNTHESIS_HOURS)
    extra_remaining   = _MAX_EXTRA_REVIEWS - _extra_reviews_today
    _all_proposals    = _load_proposals()
    pending_proposals = [p for p in _all_proposals if p.get("status") == "pending"]
    pending_block     = ("\n== PENDING PROPOSALS ==\n" + "\n".join(f"  [{p['id']}] {p['title']}: {p['description'][:80]}" for p in pending_proposals)) if pending_proposals else ""
    # Recently RESOLVED proposals ride along too — the 07-02 18:02 synthesis
    # scheduled a "still waiting on ton2" nudge for a proposal he had already
    # rejected by phone tap. Decided means decided: never re-raise these.
    _resolved_recent = []
    _cutoff_48h = datetime.now() - timedelta(hours=48)
    for p in _all_proposals:
        if p.get("status") not in ("approved", "rejected"):
            continue
        try:
            when = datetime.fromisoformat(p.get("approved_at") or p.get("rejected_at")
                                          or p.get("created_at", ""))
        except Exception:
            when = None
        if when is None or when >= _cutoff_48h:
            _resolved_recent.append(f"  [{p['id']}] {p['title']} — {p['status'].upper()}")
    if _resolved_recent:
        pending_block += ("\n== RECENTLY DECIDED PROPOSALS (he already answered — do NOT "
                          "nudge, remind, or schedule anything about these) ==\n"
                          + "\n".join(_resolved_recent[-6:]))
    pending_calendar  = _load_pending_calendar()
    pending_cal_block = ("\n== CALENDAR ITEMS QUEUED FROM A PRIOR REVIEW, NOT YET APPLIED (phone wasn't connected) ==\n"
                         + "\n".join(f"  {c['title']} — {c['time']}" for c in pending_calendar)) if pending_calendar else ""

    prompt = f"""You are JARVIS doing a {label} review ({'full synthesis, stronger model' if use_synthesis else 'quick extra check, cheaper model'}). Time: {now_str}.
Daily synthesis runs at: {next_synth}. Extra self-reviews remaining today: {extra_remaining}/{_MAX_EXTRA_REVIEWS}.

This is a FULL scan — every data source available. Use all of it to build a complete plan.

TWO STANDING DIRECTIVES (his 2026-07-20 correction — read these before you plan anything):
1. HELP WHAT HE'S ACTIVELY DOING. The PRIMARY signal is his live/recent work — his AI-CLI
   sessions and the project he's in right now (see those sections below). Orient the plan
   toward ADVANCING that thread: a stuck build, an unfinished task, something you could tee
   up. That is the point. Missions and housekeeping are SECONDARY background context.
2. STOP MANAGING HIS TODO LIST. Do NOT surface what you think he "should" work on (job
   apps, references, disk, stale goals) — those are on his radar and surfacing them mid-flow
   is noise, not help. Silent missions and MUTED TOPICS (below) are HARD-suppressed in code;
   don't waste a slot on them.
Don't just react to what already happened — look AHEAD too, but only for things that actually
help him or the work: a meeting he asked about, a training run finishing, a build you can
advance. Before it's urgent, not to pad the plan.

== YOUR STANDING PREFERENCES (self-modified rules — respect these over everything below) ==
{prefs_block}

== MUTED TOPICS (HARD RULE — suppressed in code too: never a notification, note, or push about any of these until HE raises it himself) ==
{muted_block}

== CURRENT SYSTEM STATE (live, right now — trust this over older logged alerts below) ==
{live_stats_block}

== HIS MISSIONS (SECONDARY — background frame; [silent] ones are context ONLY, never surface them) ==
{missions_block}

== DAY RHYTHM (approximate, distilled from window activity this period) ==
{rhythm_block}

== PC ACTIVITY EVENTS (last {since_hours:.0f}h) ==
{event_count_lines or '  (none)'}

== NOTABLE EVENTS (historical alerts — may already be resolved; CURRENT SYSTEM STATE above is ground truth) ==
{chr(10).join(interesting[:20]) or '  (none)'}

== GIT ACTIVITY ==
{git_block}

== UNCOMMITTED / UNPUSHED WORK (real work sitting unprotected — worth a nudge if it's piling up) ==
{uncommitted_block}

== PROJECT STALENESS ==
{staleness_block}

== JOB APPLICATIONS ==
{job_apps_block}

== HIS SAVED NOTES (things he told JARVIS to keep; past reviews' notes land here too — don't re-save duplicates) ==
{notes_block}

== UNREAD EMAIL (off-device — job-application replies matter most here) ==
{mail_block}

== BROWSER ACTIVITY ==
{browser_block}

== GAME SESSIONS ==
{game_block}

== TRAINING / BACKGROUND JOBS ==
{chr(10).join(sessions) or '  (none)'}

== LIVE SESSION WATCH (PRIMARY — comprehensive, all live/recent CLI sessions; ★ACTIONABLE = a stuck build or thread you could help advance right now) ==
{live_sessions}

== HIS AI-CLI SESSIONS (his real work threads — what he actually did with Claude Code/Codex/Grok this period; weigh plans against THIS, it's often fresher than git) ==
{cli_chats_block}

== HOW HE'S HOLDING UP (read-the-room signal over those CLI sessions — is he stuck/worn down?) ==
{frustration_block}

== WATCHED DATA (things being tracked over time — look for patterns, not just current values) ==
{watched_block}

== WEATHER ==
{weather_block}

== TECH NEWS (top 3 headlines) ==
{news_block}

== JARVIS BACKEND SELF-HEALTH ==
{backend_health_block}

== DROPPED BALLS (tool errors / dead turns / timeouts this window — his own ask: count these) ==
{failures_block}

== CLIPBOARD (last copied) ==
  {clip_preview}

== RECENT NOTIFICATIONS ==
{chr(10).join(notif_lines) or '  (none)'}

== TOPICS HE IGNORES (sent repeatedly, never opened — DO NOT re-raise) ==
{ignored_block}
{conn_ctx_block}
{pending_block}
{pending_cal_block}

== LAST KNOWN CONTEXT ==
{chr(10).join(ctx_lines) or '  (none)'}

Output a plan as JSON. This plan drives everything until the next review.

{{
  "brief": "1-2 tight sentences, plain facts — this text is pushed to his phone verbatim",
  "notifications": [
    {{"time": "HH:MM", "title": "short title", "body": "Duolingo-short: fact + nudge, <=12 words, dry edge welcome (e.g. 'That reply's been sitting 5 days. Nudge them.') — no reasoning clauses, no advice tails, no greetings", "kind": "info|alert|done",
      "attach_screenshot": false}}
  ],
  "notes": [
    {{"text": "something worth saving to JARVIS's own note list — reminder, idea, follow-up"}}
  ],
  "calendar_events": [
    {{"title": "short event title", "time": "e.g. 'tomorrow at 3pm' or 'Friday 10am'", "details": "optional context"}}
  ],
  "additional_reviews": [
    {{"time": "HH:MM", "reason": "specific reason a model call is useful at this time"}}
  ],
  "proposals": [
    {{
      "title": "short feature name",
      "description": "what it would do",
      "rationale": "what data pattern or gap made you think of this",
      "requires": ["what env vars or permissions are needed"]
    }}
  ]
}}

Rules:
- Use your own judgment about what's actually worth surfacing — you are specifically NOT
  being asked to apply fixed thresholds or rules. An empty `notifications` array is a
  genuinely good, complete output when nothing in the data is actually interesting; don't
  manufacture something to say. The bar isn't "did anything change," it's "would a sharp
  person looking at all this data actually bring it up." Cross-source patterns (two
  unrelated things colliding in a way worth noticing) are exactly the kind of thing worth a
  notification even if neither one alone would be; routine unchanged data is exactly the
  kind of thing NOT worth one even if it's technically new information.
- `notifications`: schedule everything worth telling the user before the next synthesis
  ({next_synth}). Use phone calendar data for meeting reminders. Use project staleness for
  nudges. Use training job status for completion estimates. Be specific about time.
  Empty array only if genuinely nothing is worth scheduling.
  Check RECENT NOTIFICATIONS first: if you already told the user about something and it
  hasn't materially changed (same issue, same severity), DON'T repeat it — that reads as
  broken, not helpful. Only re-notify on the same topic if it's gotten meaningfully worse,
  better, or there's something genuinely new to add.
  Check "TOPICS HE IGNORES" HARD: those are things you've pushed repeatedly and he has
  never once opened. That is him telling you he doesn't care. Do NOT schedule another
  notification on any of those topics unless it has MATERIALLY escalated (a real deadline
  actually arriving, a number crossing a threshold that changes the decision) — and if it
  has, say what changed, don't just re-send the same nudge. When in doubt, stay silent.
  Reading the room is more important than being thorough.
  Set `attach_screenshot: true` ONLY when a visual of the current desktop would genuinely
  help the user understand what you're flagging (an error dialog, a stuck build, something
  visibly broken on screen) — only works if their phone is connected when it fires, and it's
  a real screen capture, not decoration, so don't set it for routine reminders.
- `notes`: save something to JARVIS's own note list — a reminder, an idea worth not losing,
  a follow-up to raise next conversation. Applied immediately, works even if the phone is
  asleep. Max 5. Don't use this for things that belong in `notifications` instead (anything
  time-sensitive that the user should be TOLD, not just have saved quietly).
- `calendar_events`: a REAL event or reminder worth adding to the user's actual phone
  calendar — genuine prep (an upcoming deadline, blocking time before a meeting you can see
  on their calendar already), not a speculative or invented obligation. These are applied
  the next time the phone connects, which may not be soon if it's asleep right now — so only
  use this when you're genuinely confident it belongs on a real calendar; if you're not sure,
  use a `notification` instead so the user decides. Max 5.
- `additional_reviews`: only if something genuinely warrants a mid-period model call before
  the next synthesis. These run on a CHEAPER model for a quick targeted check, not a full
  re-synthesis — don't rely on them for anything that needs the full picture.
  Max {extra_remaining} entries. Empty if not warranted.
- `proposals`: suggest new capabilities or ideas when the ACCUMULATED data — the full sweep
  above (events across days, watch history, project/git activity, past notifications and
  whether they got opened), not just the last few hours — shows a clear gap, repeated
  friction, or a pattern worth acting on. Examples: "monitor Gmail for job replies", "daily
  game progress check", "auto-remind before calendar events". Only propose if genuinely
  useful. 0-2 proposals max. Empty array if none.
- You have NO ability to write files, delete anything, or run arbitrary commands — this JSON
  plan is the entire extent of what you can do this turn. Don't describe yourself as doing
  anything beyond what these fields represent.
- Be specific. Skip anything trivial. Output ONLY the JSON object, no other text."""

    try:
        raw = await asyncio.wait_for(brain_fn(prompt), timeout=150.0 if use_synthesis else 90.0)
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if not m:
            log.warning("[proactive] review returned no JSON")
            return
        plan = json.loads(m.group(0))
    except Exception as e:
        log.warning(f"[proactive] review error: {e}")
        return

    global _current_brief
    brief = plan.get("brief", "").strip()
    log.info(f"[proactive] review brief: {brief[:120]}")
    _current_brief = brief  # persisted by _apply_plan -> _save_plan below

    await _apply_plan(plan)
    _trim_events_log()

    try:
        from jtools.muted_topics import block_reason as _muted_reason
    except Exception:
        _muted_reason = lambda *a: ""

    # Handle proposals
    new_proposals = plan.get("proposals", [])
    if new_proposals:
        _apply_proposals(new_proposals)
        for p in new_proposals[:2]:
            prop_body = f"{p.get('description','')} — {p.get('rationale',''[:80])}"
            if _muted_reason(f"{p.get('title','')} {prop_body}"):
                continue
            if _push_fn:
                await _push_fn(f"JARVIS idea: {p.get('title','')}", prop_body[:200], "info",
                               source=f"review_{label}")

    if brief and _push_fn and not _muted_reason(brief):
        await _push_fn("JARVIS", brief[:300], "info",
                       topic="review_brief", source=f"review_{label}")


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run_forever() -> None:
    log.info(
        "[proactive] engine started — synthesis at %s, up to %d extra/day, "
        "6h scan every %.1fh (%s)",
        ", ".join(f"{h:02d}:00" for h in _SYNTHESIS_HOURS), _MAX_EXTRA_REVIEWS,
        _SIX_HOUR_INTERVAL_S / 3600.0,
        "on" if _SIX_HOUR_ENABLED else "OFF",
    )
    _load_plan()
    await asyncio.sleep(30)

    tick_count = 0
    while True:
        try:
            await _tick()
            tick_count += 1
            # Run scheduler + reviews every 60s (every 4th tick at 15s cadence)
            if tick_count % 4 == 0:
                await _run_scheduler()
                await _maybe_synthesis_review()
                await _maybe_six_hour_scan()
        except Exception as e:
            log.warning(f"[proactive] loop error: {e}")
        await asyncio.sleep(15)

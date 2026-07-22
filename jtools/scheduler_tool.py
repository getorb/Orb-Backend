"""Schedule / list / cancel future push notifications — first-class reminders.

Born 2026-07-03 ~1am: ninjahawk asked for a week of job-application reminders
and the brain had no tool for it — it improvised a loader script against
active_plan.json and lost the race with the engine's minutely _save_plan
rewrite. These tools call proactive_engine's in-process API instead: no file
race, restart-safe (same active_plan.json persistence), fired by the existing
zero-AI scheduler loop through the normal push pipeline (APNs — works with
the app closed). source="user" items survive the twice-daily synthesis plan
replacement; the model's plan can never clear them.
"""
import asyncio
import json
import os
import urllib.request

from tool_registry import tool
import proactive_engine

# ⚠ Process-identity guard: grok/codex tool calls run inside jarvis_mcp.py —
# a SEPARATE stdio process whose proactive_engine import is a blank copy
# (empty _schedule, never wire()d). Scheduling there would append to nothing
# and _save_plan would clobber the real active_plan.json with a near-empty
# file. _push_fn is only ever set by server_win's wire() call, so it doubles
# as "am I the live backend?" — when unset, relay over loopback HTTP to the
# endpoints that hit the real in-memory engine. Claude-family brains use the
# resident in-process /mcp and always take the direct path.
_BASE = "http://localhost:" + (os.environ.get("JARVIS_PORT")
                               or os.environ.get("ORB_PORT") or "8340")


def _in_backend() -> bool:
    return proactive_engine._push_fn is not None


def _http(method: str, path: str, payload: dict | None = None) -> dict:
    # X-Jarvis-Token so the relay keeps working when JARVIS_HTTP_AUTH=1 — the
    # stdio process inherits the backend's environment (.env already loaded).
    headers = {"Content-Type": "application/json"}
    if os.environ.get("JARVIS_TOKEN"):
        headers["X-Jarvis-Token"] = os.environ["JARVIS_TOKEN"]
    req = urllib.request.Request(
        _BASE + path, method=method,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"backend returned {e.code}"}
    except Exception as e:
        return {"error": f"backend unreachable ({type(e).__name__})"}


@tool(
    name="schedule_notification",
    description=(
        "Schedule a push notification (a reminder) for a future time. It fires "
        "even if the app is closed and survives backend restarts. Use for any "
        "'remind me at/on ...' request or when the user wants future nudges "
        "about anything. For a batch (e.g. a week of reminders), call this once "
        "per reminder. Time format: 'HH:MM' for the next occurrence of that "
        "clock time, or an ISO datetime like '2026-07-04T10:00' for a specific day."
    ),
    parameters={
        "time":  {"type": "string", "description": "When to fire: 'HH:MM' (next occurrence) or ISO datetime '2026-07-04T10:00'."},
        "title": {"type": "string", "description": "Short reminder title (~5 words)."},
        "body":  {"type": "string", "description": "One-sentence body — the useful detail (what to do, a URL, etc.)."},
        "kind":  {"type": "string", "description": "Optional kind: 'info' (default) or 'alert'."},
    },
    required=["time", "title"],
)
async def schedule_notification(time: str, title: str, body: str = "", kind: str = "info") -> str:
    if _in_backend():
        return proactive_engine.schedule_notification(time, title, body, kind)
    r = await asyncio.to_thread(_http, "POST", "/api/proactive/schedule",
                                {"time": time, "title": title, "body": body, "kind": kind})
    return r.get("result") or r.get("error") or "Scheduling failed."


@tool(
    name="schedule_task",
    description=(
        "Schedule YOURSELF to actually DO a piece of work at a future time and report "
        "the result — this is NOT a reminder. When it fires, you genuinely run the task "
        "(read files, analyse, write) and send the user what you produced. Use this "
        "whenever he asks you to work on something later: 'at 3pm read the project and tell "
        "me what it does', 'tonight, draft the follow-up email', 'go through my resume "
        "at noon and flag what's weak'. For a plain reminder pushed to HIM, use "
        "schedule_notification instead — scheduling a reminder to yourself is NOT doing "
        "the work. Time: 'HH:MM' (next occurrence) or ISO '2026-07-06T15:00'."
    ),
    parameters={
        "time": {"type": "string", "description": "When to run it: 'HH:MM' or ISO datetime '2026-07-06T15:00'."},
        "task": {"type": "string", "description": "The full instruction to carry out — specific and self-contained (it runs later with NO conversation context, so include everything needed: what to read, what to produce)."},
        "title": {"type": "string", "description": "Short label for the task (~5 words)."},
    },
    required=["time", "task"],
)
async def schedule_task(time: str, task: str, title: str = "") -> str:
    if _in_backend():
        return proactive_engine.schedule_task(time, title or task[:40], task)
    r = await asyncio.to_thread(_http, "POST", "/api/proactive/schedule_task",
                                {"time": time, "title": title, "task": task})
    return r.get("result") or r.get("error") or "Scheduling the task failed."


@tool(
    name="list_scheduled_notifications",
    description=(
        "List everything scheduled to fire in the future: user reminders and "
        "the proactive engine's own self-scheduled check-ins. Use to answer "
        "'what reminders do I have' or before scheduling to avoid duplicates."
    ),
    parameters={},
    required=[],
)
async def list_scheduled_notifications() -> str:
    if _in_backend():
        items = proactive_engine.list_scheduled()
    else:
        r = await asyncio.to_thread(_http, "GET", "/api/proactive/scheduled")
        if "items" not in r:
            return r.get("error") or "Could not reach the scheduler."
        items = r["items"]
    if not items:
        return "Nothing scheduled."
    lines = []
    for i in items:
        tag = "reminder" if i["source"] == "user" else i["kind"]
        detail = i["body"] or i["reason"]
        lines.append(f"{i['fire_at']} [{tag}] {i['title']}" + (f" — {detail}" if detail else ""))
    return f"{len(items)} scheduled:\n" + "\n".join(lines)


@tool(
    name="cancel_scheduled_notifications",
    description=(
        "Cancel future scheduled notifications whose title or body contains the "
        "given text (case-insensitive). Only cancels notifications/reminders — "
        "never the engine's own review check-ins. Returns what was cancelled."
    ),
    parameters={
        "query": {"type": "string", "description": "Text to match against scheduled titles/bodies (e.g. 'Apply: Anthropic')."},
    },
    required=["query"],
)
async def cancel_scheduled_notifications(query: str) -> str:
    if _in_backend():
        removed = proactive_engine.cancel_scheduled(query)
    else:
        r = await asyncio.to_thread(_http, "POST", "/api/proactive/cancel",
                                    {"query": query})
        if "cancelled" not in r:
            return r.get("error") or "Could not reach the scheduler."
        removed = r["cancelled"]
    if not removed:
        return f"Nothing scheduled matches '{query}'."
    return f"Cancelled {len(removed)}: " + "; ".join(removed)

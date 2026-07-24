"""Continuous session watch (the fast lane between broad syntheses).

The 6-hour synthesis and the mind's self-scheduled wakes are the BROAD sweep.
This is the fast lane in between: a lightweight, zero-LLM pass — run every ~90s
from the scheduler tick — that watches EVERY live/recent AI-CLI session (Claude
Code, Codex, Grok) comprehensively, not a sampled subset, so the mind can notice
a stuck build or something it could help with in NEAR-REAL-TIME rather than only
every six hours.

Two outputs, both additive:
  1. A durable snapshot (data/live_sessions.json) + `live_sessions_block()` — the
     comprehensive context-first view of what he's actively doing across ALL
     sessions, read by the mind's wake context and the synthesis. This is the
     PRIMARY signal for JOB 1 ("help what he's actively doing").
  2. `watch_pass()` — on a session that NEWLY becomes actionable (elevated
     frustration OR a build sitting on an unresolved error), it asks the mind to
     wake soon and look. It NEVER pushes a notification itself (same discipline
     as frustration_signal) — the mind decides, under its budgets, muted topics,
     and his notification standing. Debounced per-session + globally so a long
     stuck session can't re-nag.

Scoring reuses jtools/frustration_signal.scan() (which already parses + scores
every session) so this pass adds almost no cost. Muted topics are respected: a
session whose project/last line hits a muted topic never escalates.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("orb.tools.session_watch")

_DATA = Path(__file__).resolve().parent.parent / "data"
_SNAPSHOT = _DATA / "live_sessions.json"
_STATE = _DATA / "session_watch_state.json"

CONFIG = {
    "pass_interval_s": 90.0,        # how often the watch actually does work
    "live_window_min": 20.0,        # a session is "live" if active within this
    "lookback_hours": 6.0,          # which sessions are candidates at all
    "escalate_global_gap_min": 20.0,   # no two escalations globally inside this
    "escalate_per_session_min": 90.0,  # same session can't re-escalate inside this
    "wake_in_minutes": 8.0,         # ask the mind to look this soon on a signal
}

_last_pass_at = 0.0


def _minutes_since(ts: float) -> float:
    return (time.time() - ts) / 60 if ts else 1e9


def _is_actionable(r: dict) -> tuple[bool, str]:
    """Is this scored session something the mind might help with RIGHT NOW?
    Broader than acute frustration: a stuck build counts even with no venting."""
    from jtools import frustration_signal as fs
    level = r.get("level", "calm")
    markers = r.get("markers", {})
    if fs._ge(level, "elevated"):
        return True, f"{level} frustration (score {r.get('score', 0):.0f})"
    if markers.get("stall_after_error"):
        return True, "sitting on an error (stalled after a failure)"
    if markers.get("stuck") and fs._ge(level, "mild"):
        return True, "telling the tool it's still broken / not working"
    if markers.get("repeated_retry"):
        return True, "retrying the same failing request"
    return False, ""


def snapshot() -> list[dict]:
    """Score + shape EVERY recent session. Zero-LLM. Newest-active first."""
    try:
        from jtools import frustration_signal as fs
        results = fs.scan(hours=CONFIG["lookback_hours"])
    except Exception as e:
        log.debug(f"[session_watch] scan failed: {e}")
        return []
    out = []
    for r in results:
        s = r.get("session", {})
        act, why = _is_actionable(r)
        last_active = s.get("last_active", 0.0)
        out.append({
            "id": s.get("id", ""),
            "tool": s.get("tool", ""),
            "project": s.get("project", ""),
            "branch": s.get("branch", ""),
            "last_active": last_active,
            "mins_ago": round(_minutes_since(last_active), 1),
            "live": _minutes_since(last_active) <= CONFIG["live_window_min"],
            "level": r.get("level", "calm"),
            "score": r.get("score", 0.0),
            "markers": list((r.get("markers") or {}).keys()),
            "actionable": act,
            "why": why,
            "last_user": (s.get("last_user") or "")[:100],
        })
    return out


def _write_snapshot(snap: list[dict]) -> None:
    try:
        tmp = _SNAPSHOT.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "updated": datetime.now().isoformat(timespec="seconds"),
            "sessions": snap,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_SNAPSHOT)
    except Exception as e:
        log.debug(f"[session_watch] snapshot write failed: {e}")


def live_sessions_block(max_items: int = 6) -> str:
    """Comprehensive context-first view of ALL his live/recent sessions, for the
    mind wake context and the synthesis. Reads the snapshot (cheap); falls back
    to a fresh pass if none exists yet. Empty string if nothing recent."""
    snap = []
    try:
        if _SNAPSHOT.exists():
            data = json.loads(_SNAPSHOT.read_text(encoding="utf-8"))
            snap = data.get("sessions", [])
        if not snap:
            snap = snapshot()
    except Exception:
        snap = snapshot()
    if not snap:
        return ""
    live = [s for s in snap if s.get("live")]
    shown = (live or snap)[:max_items]
    lines = []
    for s in shown:
        tag = "★ACTIONABLE" if s.get("actionable") else ("live" if s.get("live") else "recent")
        proj = s.get("project") or s.get("tool") or "session"
        why = f" — {s['why']}" if s.get("why") else ""
        lines.append(
            f"  [{tag}] {proj} ({s.get('tool')}, {s.get('branch') or 'no-branch'}), "
            f"active {s.get('mins_ago')}m ago{why}; last: \"{s.get('last_user', '')[:60]}\"")
    n_act = sum(1 for s in snap if s.get("actionable"))
    header = (f"  {len(live)} live / {len(snap)} recent CLI session(s)"
              + (f", {n_act} actionable (a stuck build or something you could advance)" if n_act else "")
              + ":\n")
    return header + "\n".join(lines)


def _load_state() -> dict:
    try:
        if _STATE.exists():
            return json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"global_last_escalate_at": 0.0, "sessions": {}}


def _save_state(st: dict) -> None:
    try:
        tmp = _STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE)
    except Exception:
        pass


def _muted(text: str) -> bool:
    try:
        from jtools.muted_topics import block_reason
        return bool(block_reason(text))
    except Exception:
        return False


async def watch_pass(force: bool = False) -> dict | None:
    """The frequent, lightweight pass. Self-throttled to CONFIG['pass_interval_s'].
    Always refreshes the snapshot; escalates (mind wake) only on a session that
    NEWLY becomes actionable while genuinely live, debounced, and not muted.
    Never pushes — the mind decides. Returns the escalated session dict or None."""
    global _last_pass_at
    now = time.time()
    if not force and now - _last_pass_at < CONFIG["pass_interval_s"]:
        return None
    _last_pass_at = now

    snap = snapshot()
    _write_snapshot(snap)
    if not snap:
        return None

    st = _load_state()
    sessions_state = st.setdefault("sessions", {})

    # Clear the "escalated" latch for any session that has calmed back down, so a
    # genuinely NEW stuck episode later can escalate again.
    for s in snap:
        rec = sessions_state.get(s["id"])
        if rec and rec.get("escalated") and not s.get("actionable"):
            rec["escalated"] = False

    # Pick the most severe NEWLY-actionable, live, non-muted, debounce-clear one.
    def _fireable(s: dict) -> bool:
        if not (s.get("actionable") and s.get("live")):
            return False
        if _muted(f"{s.get('project', '')} {s.get('last_user', '')}"):
            return False
        if now - st.get("global_last_escalate_at", 0.0) < CONFIG["escalate_global_gap_min"] * 60:
            return False
        rec = sessions_state.get(s["id"])
        if rec:
            if rec.get("escalated"):
                return False  # already flagged this episode; wait for calm
            if now - rec.get("last_escalate_at", 0.0) < CONFIG["escalate_per_session_min"] * 60:
                return False
        return True

    candidates = [s for s in snap if _fireable(s)]
    fired = None
    if candidates:
        fired = max(candidates, key=lambda s: s.get("score", 0.0))
        proj = fired.get("project") or fired.get("tool") or "?"
        try:
            import proactive_engine as pe
            pe.log_event("session_actionable", session=fired["id"][:12], project=proj,
                         tool=fired.get("tool"), level=fired.get("level"),
                         why=fired.get("why"), score=fired.get("score"))
        except Exception:
            pass
        try:
            import mind
            mind.request_wake_at(
                CONFIG["wake_in_minutes"],
                f"live session '{proj}' looks actionable ({fired.get('why')}) — "
                f"you might advance it or help. Look; within-envelope help is welcome, "
                f"a nag is not.")
        except Exception:
            pass
        st["global_last_escalate_at"] = now
        sessions_state[fired["id"]] = {
            "last_escalate_at": now, "escalated": True,
            "project": proj, "why": fired.get("why"),
        }
        log.info(f"[session_watch] actionable: {proj} ({fired.get('why')}) — mind wake requested (no push)")

    # Prune stale session records (>3d).
    cutoff = now - 3 * 86400
    st["sessions"] = {k: v for k, v in sessions_state.items()
                      if v.get("last_escalate_at", 0.0) >= cutoff}
    _save_state(st)
    return fired


def status() -> dict:
    try:
        snap = json.loads(_SNAPSHOT.read_text(encoding="utf-8")) if _SNAPSHOT.exists() else {}
    except Exception:
        snap = {}
    return {"snapshot": snap, "config": CONFIG}


# ── Read-only tool (introspection) — auto-registered via import_all_tools ─────
try:
    from tool_registry import tool

    @tool(
        name="watch_sessions",
        description=(
            "Comprehensive live view of ALL his active/recent AI-CLI coding "
            "sessions (Claude Code/Codex/Grok) — which are live, which look stuck "
            "or actionable (a build sitting on an error, repeated failing retries). "
            "Zero-LLM, read-only. Use to see what he's actively working on so you "
            "can help that thread."),
        parameters={},
        required=[],
    )
    async def watch_sessions() -> str:
        block = live_sessions_block()
        return block or "No live or recent CLI sessions right now."
except Exception:
    pass

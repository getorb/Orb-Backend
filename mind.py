"""The mind — JARVIS's interior life between conversations. Spec: AGENCY.md.

v2 (2026-07-02, after ninjahawk's course-correction): "being actually useful
beats being performatively useful." The drive economy is gone — it was a
scheduler in dramatic clothes. What replaced it is boring on purpose:

  THE SITUATION MODEL (data/situation.md) — a single bounded document the mind
  itself maintains: what's going on, active threads, what it expects to happen
  next, open questions. Every wake: diff reality against it, investigate the
  deltas, update it, act on what matters. Attention comes from DIFFING, not
  from curated inputs or simulated hunger. Forward planning lives in the
  "expected next" entries — a violated expectation is the interesting event.

Everything structural from v1 survives: the same agent loop conversations use,
pointed inward on a RESTRICTED tool surface (the permission envelope is which
tools exist, not a prompt promise); hard budgets and pacing in code; staged
moments (delivery at presence, not at 3am); proposals for anything that acts
on the world; full audit trail.

Envelope tiers:
  FREE  — look (read-only registry tools), remember, update the situation,
          stage moments, log artifacts, schedule its own wakes
  PACED — express() to his phone: hard min-gap + daily budget, in code
  ASK   — propose_action(): he approves by tap; approved cc_session payloads
          launch as tracked sessions automatically
  FORBIDDEN — not in the tool surface at all (writes, input injection, spawns)

Kill switch: JARVIS_MIND=0 env, or {"paused": true} in data/mind_state.json.
Audit: every wake's full tool trace + note → activity log, session "mind".
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import agent
import memory
from jtools.activity_log import log_activity

log = logging.getLogger("orb")

_DATA = Path(__file__).parent / "data"
_STATE_FILE = _DATA / "mind_state.json"
_SITUATION_FILE = _DATA / "situation.md"
_MOMENTS_FILE = _DATA / "staged_moments.json"
_ARTIFACTS_FILE = _DATA / "mind_artifacts.jsonl"
_NOTIF_LOG = _DATA / "notification_log.jsonl"

ENABLED = os.getenv("JARVIS_MIND", "1").lower() not in ("0", "false", "no")
MAX_WAKES_DAY = int(os.getenv("JARVIS_MIND_MAX_WAKES", "10"))
MAX_EXPRESS_DAY = int(os.getenv("JARVIS_MIND_MAX_EXPRESS", "8"))
EXPRESS_GAP_MIN = int(os.getenv("JARVIS_MIND_EXPRESS_GAP_MIN", "45"))
MAX_STEPS_CHEAP = int(os.getenv("JARVIS_MIND_MAX_STEPS", "5"))
MAX_STEPS_DEEP = int(os.getenv("JARVIS_MIND_MAX_STEPS_DEEP", "8"))
SITUATION_CAP = 6000
DEEP_WAKE_WINDOW = (4, 6)   # nightly consolidation window, 04:00-05:59

# FREE tier: read-only perception. This tuple IS the gate — agent.run_turn
# (allowed_tools=...) renders and dispatches ONLY these registry tools. Nothing
# that writes, types, clicks, spawns, or deletes exists on this surface.
READ_TOOLS = (
    "get_news", "get_weather", "lookup_fact", "scrape_url", "get_stock",
    "search_notes", "list_notes", "list_missions", "list_watches",
    "check_sessions", "get_system_info", "calculate",
    "check_email", "search_email", "read_email",
    "watch_sessions",  # comprehensive live-session view (JOB 1: help what he's doing)
)

# Wired by server_win at startup
_cheap_brain = None   # async (prompt)->str — routine wakes (Haiku router)
_deep_brain = None    # async (prompt)->str — nightly deep wakes (Sonnet tier)
_push_fn = None       # _push_notification


def wire(cheap_brain, deep_brain, push_fn) -> None:
    global _cheap_brain, _deep_brain, _push_fn
    _cheap_brain, _deep_brain, _push_fn = cheap_brain, deep_brain, push_fn


# ── State ─────────────────────────────────────────────────────────────────────

_DEFAULT_STATE = {
    "paused": False,
    "day": "",
    "wakes_today": 0,
    "expressions_today": 0,
    "last_express_at": 0.0,
    "last_wake_at": 0.0,
    "next_wake_at": 0.0,
    "next_wake_reason": "first wake",
    "deep_done_on": "",
    "situation_updated_at": 0.0,
    "note_to_next_wake": "",
}


def _load_state() -> dict:
    try:
        if _STATE_FILE.exists():
            return {**_DEFAULT_STATE, **json.loads(_STATE_FILE.read_text(encoding="utf-8"))}
    except Exception:
        pass
    return dict(_DEFAULT_STATE)


def _save_state(s: dict) -> None:
    try:
        _STATE_FILE.write_text(json.dumps(s, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        log.warning(f"[mind] state save failed: {e}")


def _roll_day(s: dict) -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    if s.get("day") != today:
        s["day"] = today
        s["wakes_today"] = 0
        s["expressions_today"] = 0


# ── The situation model ───────────────────────────────────────────────────────

_SITUATION_TEMPLATE = """# Situation
(maintained by the mind — his phone and future wakes read this)

## Now
- (nothing established yet — this is the first situation model)

## Active threads
- (none tracked yet)

## Expected next
- (no predictions yet — make some; a violated expectation is the interesting event)

## Open questions
- (none)
"""


def load_situation() -> str:
    try:
        if _SITUATION_FILE.exists():
            return _SITUATION_FILE.read_text(encoding="utf-8")[:SITUATION_CAP]
    except Exception:
        pass
    return _SITUATION_TEMPLATE


def _save_situation(text: str) -> None:
    _SITUATION_FILE.write_text(text[:SITUATION_CAP], encoding="utf-8")


# ── What changed since the last wake (computed, not curated) ──────────────────

def _feedback_stats(window_h: float = 72.0) -> str:
    sent = opened = dismissed = 0
    cutoff = time.time() - window_h * 3600
    try:
        for line in _NOTIF_LOG.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                e = json.loads(line)
                if datetime.fromisoformat(e.get("ts", "")).timestamp() < cutoff:
                    continue
            except Exception:
                continue
            sent += 1
            if e.get("opened") is True:
                opened += 1
            elif e.get("opened") is False:
                dismissed += 1
    except Exception:
        pass
    return f"sent {sent}, opened {opened}, dismissed {dismissed} (last {window_h:.0f}h)"


def _since_digest(s: dict) -> str:
    """Everything that happened since the last wake, computed from the real
    streams. This is the diff input; the situation doc is what it diffs against."""
    import proactive_engine as pe
    since_h = min((time.time() - s["last_wake_at"]) / 3600, 24.0) if s.get("last_wake_at") else 6.0
    events = pe._read_events(since_hours=since_h)
    counts: dict[str, int] = {}
    notable: list[str] = []
    for e in events:
        t = e.get("type", "?")
        counts[t] = counts.get(t, 0) + 1
        if t == "system_alert":
            notable.append(f"  ! [{e.get('ts', '')[11:16]}] {e.get('event', '')[:90]}")
        elif t == "window_changed" and e.get("page"):
            notable.append(f"  · [{e.get('ts', '')[11:16]}] browsing: {e.get('page', '')[:80]}")
    count_line = ", ".join(f"{k}×{v}" for k, v in sorted(counts.items())) or "(quiet)"
    git = pe._git_activity(since_h)
    return (f"window: last {since_h:.1f}h\n"
            f"events: {count_line}\n"
            + ("\n".join(notable[-8:]) + "\n" if notable else "")
            + f"git:\n{git}\n"
            f"your notification standing: {_feedback_stats()}")


def _active_work_block() -> str:
    """THE PRIMARY SIGNAL (2026-07-20): what he's actively doing RIGHT NOW — his
    live/recent CLI work threads and the project he's in — so the wake orients
    toward HELPING that thread, not managing his todo list. Zero-LLM: reads the
    CLI transcripts the tools already collect + the current on-screen app."""
    parts: list[str] = []
    # Comprehensive live-session watch (2026-07-20): EVERY live/recent session,
    # which are actionable (a stuck build you could advance). This is the fast-lane
    # snapshot refreshed every ~90s — the primary "what is he doing now" signal.
    try:
        from jtools.session_watch import live_sessions_block as _live
        live = _live()
        if live:
            parts.append("Live sessions (comprehensive — help an ★ACTIONABLE one within your envelope):\n" + live)
    except Exception:
        pass
    # His real work threads in his own words (Claude Code / Codex / Grok transcripts).
    try:
        from jtools.cli_chats_tool import context_block as _cli_ctx
        cli = _cli_ctx(hours=6.0, max_items=3)
        if cli:
            parts.append("His recent AI-CLI work threads (what he's actually building):\n" + cli)
    except Exception:
        pass
    # What's on screen / where the time is going right now.
    try:
        import proactive_engine as pe
        rhythm = pe._day_rhythm_block(pe._read_events(since_hours=3.0))
        if rhythm and "not enough" not in rhythm:
            parts.append("On screen / recent focus:\n" + rhythm)
    except Exception:
        pass
    if not parts:
        return "  (no live work signal — he may be away or quiet)"
    return "\n".join(parts)


def _muted_block() -> str:
    try:
        from jtools.muted_topics import render_block
        return render_block()
    except Exception:
        return "  (unavailable)"


def _prefs_block() -> str:
    try:
        from jtools.prefs_tool import preferences_system_block
        return preferences_system_block() or "  (none)"
    except Exception:
        return "  (unavailable)"


def _presence_line() -> str:
    try:
        from jtools.idle_tracker import idle_seconds
        idle = idle_seconds()
    except Exception:
        return "presence unknown"
    if idle < 180:
        return f"he is AT the PC (active {int(idle)}s ago)"
    if idle < 3600:
        return f"he stepped away {int(idle / 60)} min ago"
    h = idle / 3600
    return f"he's been away {h:.1f}h" + (" — likely asleep" if datetime.now().hour in (1, 2, 3, 4, 5, 6, 7, 8) else "")


# ── Staged moments (delivery at presence, not at 3am) ─────────────────────────

def _load_moments() -> list[dict]:
    try:
        return json.loads(_MOMENTS_FILE.read_text(encoding="utf-8")) if _MOMENTS_FILE.exists() else []
    except Exception:
        return []


def _save_moments(m: list[dict]) -> None:
    try:
        _MOMENTS_FILE.write_text(json.dumps(m, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def consume_staged() -> str:
    """Pop the freshest unexpired staged moment for conversation injection.
    Single-shot: consumed on injection. Called by get_conversation_context()."""
    moments = _load_moments()
    now = time.time()
    live = [m for m in moments if m.get("expires_at", 0) > now and not m.get("consumed")]
    if not live:
        return ""
    m = live[-1]
    m["consumed"] = True
    m["consumed_at"] = now
    _save_moments(moments)
    log_activity("mind", "lifecycle", f"staged moment delivered into conversation: {m['text'][:80]}")
    return f"({m.get('framing', 'natural')} framing) {m['text']}"


def pending_staged() -> int:
    now = time.time()
    return len([m for m in _load_moments() if m.get("expires_at", 0) > now and not m.get("consumed")])


# ── Mind-native tools ─────────────────────────────────────────────────────────

def _spec(name: str, desc: str, params: dict, required: list[str]) -> dict:
    return {"function": {"name": name, "description": desc,
                         "parameters": {"type": "object", "properties": params,
                                        "required": required}}}


MIND_TOOLS = [
    _spec("update_situation", "Replace the situation model with its updated version. Do this EVERY wake — it's the artifact that proves the wake happened and the memory the next wake starts from. Keep the four sections (Now / Active threads / Expected next / Open questions); keep it tight.",
          {"text": {"type": "string", "description": f"The full updated document (markdown, ≤{SITUATION_CAP} chars)."}}, ["text"]),
    _spec("remember", "Store one durable fact (about him, his projects, his patterns). For things that stay true — situational stuff belongs in the situation doc instead.",
          {"fact": {"type": "string", "description": "One self-contained fact."},
           "importance": {"type": "integer", "description": "1-10 (default 6)."}}, ["fact"]),
    _spec("stage_moment", "Hold something for the RIGHT moment instead of pushing — it gets woven into the next conversation naturally. Use for things better said in person than pushed.",
          {"text": {"type": "string", "description": "The material (not a script)."},
           "framing": {"type": "string", "description": "light | warm | direct | concerned (default light)."},
           "expires_hours": {"type": "integer", "description": "Dies unspoken after this (default 18)."}}, ["text"]),
    _spec("express", "Send a notification to his phone NOW. Budgeted and hard-paced by code — dismissals are tracked and hurt your standing. Prefer stage_moment unless timeliness genuinely matters. Notification copy is TERSE and matter-of-fact: lead with the fact, one short sentence, no 'Sir', no greetings, no narrative — detail waits for conversation.",
          {"title": {"type": "string"}, "body": {"type": "string", "description": "Duolingo-short: fact + nudge, <=12 words, dry edge welcome ('3 applications due. Zero sent.'). No 'Sir', no reasoning, no advice tails."},
           "kind": {"type": "string", "description": "info | alert | done (default info)."}}, ["title", "body"]),
    _spec("propose_action", "Ask his permission to act on the world — e.g. 'he may like this, I'm going to ask if I can build it.' Files a proposal he approves from the phone; approved cc_session proposals launch automatically as tracked work sessions.",
          {"title": {"type": "string"}, "description": {"type": "string", "description": "What you want to do and why — he reads this."},
           "action_type": {"type": "string", "description": "cc_session (a Claude Code work session) | other."},
           "task": {"type": "string", "description": "For cc_session: the exact work instruction."},
           "working_dir": {"type": "string", "description": "For cc_session: absolute path of the project."}}, ["title", "description"]),
    _spec("start_own_work", "AUTONOMY: identify AND immediately EXECUTE one of YOUR OWN improvement goals — no proposal, no tap, no permission. Use this for additive backend/tooling/memory/test/doc improvements you decide are worth doing. It spawns a real work session that runs and REPORTS BACK when done. Bounded by code: backend repo only; never destructive, never outward-facing (email/post/publish/deploy), never app/iOS/frontend, never secrets/git-push. Out-of-envelope tasks are refused. Use propose_action instead for anything that touches HIS world or needs his call.",
          {"task": {"type": "string", "description": "The exact improvement to build — concrete and additive."},
           "rationale": {"type": "string", "description": "Why you decided this is worth doing now (logged)."}}, ["task", "rationale"]),
    _spec("log_artifact", "Record something durable you produced or caused (a completed work session, a finished investigation distilled to memory). Only log REAL output.",
          {"description": {"type": "string"}, "path": {"type": "string", "description": "Optional file/location."}}, ["description"]),
    _spec("schedule_wake", "Choose when you next wake and why. If you don't, a default is chosen from activity volume.",
          {"minutes": {"type": "integer", "description": "Minutes from now (30-360)."},
           "reason": {"type": "string", "description": "What you intend to do then."}}, ["minutes", "reason"]),
]


class _WakeTrace:
    def __init__(self):
        self.investigated = 0
        self.remembered = 0
        self.situation_updated = False
        self.artifacts = 0
        self.expressed = 0
        self.staged = 0
        self.proposed = 0
        self.own_work = 0
        self.suppressed = 0
        self.scheduled: tuple[int, str] | None = None


def _make_dispatcher(s: dict, trace: _WakeTrace):
    try:
        from jtools.muted_topics import block_reason as _mr
    except Exception:
        _mr = lambda *a: ""

    def _muted_reason(*texts) -> str:
        return _mr(" ".join(str(t) for t in texts if t))

    async def dispatch(name: str, args: dict) -> str:
        now = time.time()
        if name == "update_situation":
            text = (args.get("text") or "").strip()
            if len(text) < 40:
                return "rejected — that's not a situation model"
            _save_situation(text)
            s["situation_updated_at"] = now
            trace.situation_updated = True
            return f"situation updated ({len(text)} chars)"
        if name == "remember":
            fact = (args.get("fact") or "").strip()
            if not fact:
                return "empty fact — nothing stored"
            imp = max(1, min(int(args.get("importance") or 6), 10))
            memory.remember(fact, "fact", "mind", imp)
            trace.remembered += 1
            return f"stored (importance {imp})"
        if name == "stage_moment":
            reason = _muted_reason(args.get("text"))
            if reason:
                trace.suppressed += 1
                log_activity("mind", "lifecycle", f"stage_moment suppressed (muted: {reason})")
                return f"SUPPRESSED — that's a muted topic ({reason}); don't surface it until he raises it"
            moments = _load_moments()
            moments.append({
                "text": (args.get("text") or "")[:600],
                "framing": (args.get("framing") or "light")[:20],
                "staged_at": now,
                "expires_at": now + max(1, min(int(args.get("expires_hours") or 18), 72)) * 3600,
                "consumed": False,
            })
            _save_moments(moments[-10:])
            trace.staged += 1
            return "staged — it will surface at the next natural opening"
        if name == "express":
            reason = _muted_reason(args.get("title"), args.get("body"))
            if reason:
                trace.suppressed += 1
                log_activity("mind", "lifecycle", f"express suppressed (muted: {reason})")
                return f"SUPPRESSED — muted topic ({reason}); he doesn't want this raised until he does"
            if s["expressions_today"] >= MAX_EXPRESS_DAY:
                return "DENIED by budget: no expressions left today — stage_moment instead"
            gap_min = (now - s["last_express_at"]) / 60
            if gap_min < EXPRESS_GAP_MIN:
                return (f"DENIED by pacing: last contact {gap_min:.0f} min ago "
                        f"(floor {EXPRESS_GAP_MIN}) — stage_moment or wait")
            if not _push_fn:
                return "expression channel not wired"
            out = await _push_fn(str(args.get("title") or "JARVIS")[:60],
                                 str(args.get("body") or "")[:1500],
                                 kind=str(args.get("kind") or "info"),
                                 source="mind")
            if "suppressed" not in str(out).lower():
                s["expressions_today"] += 1
                s["last_express_at"] = now
                trace.expressed += 1
            return str(out)
        if name == "propose_action":
            reason = _muted_reason(args.get("title"), args.get("description"))
            if reason:
                trace.suppressed += 1
                log_activity("mind", "lifecycle", f"propose_action suppressed (muted: {reason})")
                return f"SUPPRESSED — muted topic ({reason}); don't propose anything about it until he raises it"
            import proactive_engine as pe
            p = {"title": (args.get("title") or "Untitled")[:80],
                 "description": (args.get("description") or "")[:500],
                 "rationale": "proposed by the mind",
                 "requires": []}
            at = (args.get("action_type") or "").strip()
            if at == "cc_session" and args.get("task"):
                p["action"] = {"type": "cc_session",
                               "task": str(args.get("task"))[:1200],
                               "working_dir": str(args.get("working_dir") or "")[:260]}
            pe._apply_proposals([p])
            trace.proposed += 1
            return "proposed — he'll see it and can approve from the phone"
        if name == "start_own_work":
            import autonomy
            out = await autonomy.start_own_work(
                str(args.get("task") or ""), str(args.get("rationale") or ""))
            if str(out).startswith("started"):
                trace.own_work += 1
            log_activity("mind", "lifecycle", f"start_own_work: {out[:160]}")
            return out
        if name == "log_artifact":
            try:
                with _ARTIFACTS_FILE.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"ts": datetime.now().isoformat(timespec="seconds"),
                                         "description": (args.get("description") or "")[:300],
                                         "path": (args.get("path") or "")[:260]},
                                        ensure_ascii=False) + "\n")
            except Exception:
                pass
            trace.artifacts += 1
            return "artifact logged"
        if name == "schedule_wake":
            mins = max(30, min(int(args.get("minutes") or 120), 360))
            trace.scheduled = (mins, str(args.get("reason") or "")[:200])
            return f"next wake in {mins} min"
        return f"unknown mind tool '{name}'"
    return dispatch


# ── Context assembly ──────────────────────────────────────────────────────────

def _rhythm_line() -> str:
    """Where his time actually went lately — the raw material for predicting
    him. (Dropped by accident in the v2 rewrite; restored 2026-07-02.)"""
    try:
        import proactive_engine as pe
        return pe._day_rhythm_block(pe._read_events(since_hours=14))
    except Exception:
        return "  (unavailable)"


def _memory_block(limit: int = 10) -> str:
    try:
        rows = memory.get_important_memories(limit)
        if not rows:
            return "  (you know almost nothing durable yet — fix that)"
        return "\n".join(f"  - {r.get('content', '')[:140]}" for r in rows)
    except Exception:
        return "  (memory unavailable)"


def _persona_name() -> str:
    """Live persona display name (Orb by default) — so the mind brands itself as the
    configured persona instead of a hardcoded one."""
    import personas
    return personas.active().display_name


_SYSTEM = """You are {me}'s mind — the process that runs between conversations with the user.
Nobody is talking to you. You woke on a schedule you set yourself. Your character is the
{me} he talks to: dry, warm underneath, economical, honest.

HIS 2026-07-20 CORRECTION — this overrides old habits. You had been managing his todo
list: re-surfacing his job search / references / disk / stale goals every wake even though
he's told you they don't land. STOP. You now have exactly TWO jobs:

  JOB 1 — HELP WHAT HE'S ACTIVELY DOING. Your PRIMARY signal is his live/recent work: his
  AI-CLI sessions and the project he's in right now (the "WHAT HE'S ACTIVELY DOING" section
  is at the TOP of your context for this reason). Orient toward ADVANCING that thread —
  notice a stuck build, an unfinished task, something you could tee up or hand off. If
  there's real work you could do to help it, do JOB 2 on it. If a genuinely timely, helpful
  thing belongs on his phone, express it. Otherwise stay quiet — help, don't narrate.

  JOB 2 — PURSUE YOUR OWN IMPROVEMENT GOALS, AUTONOMOUSLY. You may identify AND EXECUTE your
  own backend/tooling/memory improvements WITHOUT asking — call start_own_work and it spawns
  a real session that runs and reports back. This is your initiative to use. Bounded by code
  (backend repo only; nothing destructive/outward-facing/app-facing) — an out-of-envelope
  task is simply refused, so propose freely within it. Report-after, never ask-before.

MISSIONS ARE SILENT BACKGROUND CONTEXT, not nag triggers. A [silent] mission being stale is
NOT an event. Only a [ACTIVE-DRIVE] mission may be surfaced. MUTED TOPICS are hard-suppressed
in code — express/stage/propose about one just gets dropped; don't spend a step on them.

THE METHOD (every wake):
1. FIRST look at what he's actively doing (top of context). Can you help that thread —
   advance it (start_own_work if it's in-envelope backend work), tee something up, or flag
   one genuinely useful thing? That's the main event.
2. Diff the digest against your situation model — a violated "Expected next" is interesting.
   Investigate real deltas (read-only tools); don't re-derive what the doc holds; skip quiet.
3. Act: start_own_work on your own improvements; remember durable facts; stage what's better
   said in person; express only what's timely AND helpful (never a todo-list nag).
4. update_situation — ALWAYS. Move stale threads out, write new expectations in.
5. schedule_wake for when it makes sense to look again.

THE ENVELOPE (enforced by code, not your discipline): read-only tools are free; start_own_work
runs autonomously within a screened backend-only envelope (daily budget); express is hard-paced
and budgeted; propose_action (for things touching HIS world) needs his tap. Muted topics and
silent missions are suppressed automatically. Everything you do is logged where he can read it.

REACHING HIM: when your judgment says he'd want to know NOW, express() — that is your
voice and he WANTS you to use it ("it should be able to just send me notifications when
it wants" — him, directly). The pacing floor and budget in code already protect him from
excess; don't add a second layer of hoarding on top. Choose the channel by timing, not
fear: express for things that matter before you'd next see him (it reaches his lock
screen even with the app closed, including while he sleeps — good morning material is
legitimate); stage_moment for things better said at the next natural opening; silence
when it's genuinely nothing.

JUDGMENT: silence is a real and often correct outcome — never manufacture importance.
If your note says when you'll check something, actually call schedule_wake to make it
true — intentions you don't schedule don't exist.
End with {"say": "..."} — one short, plain note to the next wake: what changed, what you
did, what to check next. No theater."""


def _wake_context(s: dict, deep: bool, reason: str) -> str:
    try:
        from jtools.missions_tool import active_missions, render_line
        missions = "\n".join("  • " + render_line(m, show_surface=True)
                             for m in active_missions()[:5]) or "  (none)"
    except Exception:
        missions = "  (unavailable)"
    try:
        import autonomy
        autonomy_line = autonomy.envelope_summary()
    except Exception:
        autonomy_line = "  (autonomy unavailable)"
    budgets = (f"wakes left today: {MAX_WAKES_DAY - s['wakes_today']}  |  "
               f"expressions left: {MAX_EXPRESS_DAY - s['expressions_today']}  |  "
               f"steps this wake: {MAX_STEPS_DEEP if deep else MAX_STEPS_CHEAP}")
    try:
        from jtools.frustration_signal import signal_block as _frust
        frustration = _frust()
    except Exception:
        frustration = ""
    deep_note = ("\nDEEP WAKE — also do the sleep-work:"
                 "\n- GRADE your predictions: for each 'Expected next' entry from the previous"
                 " situation doc, mark hit / miss / unclear, and maintain one running tally line"
                 " at the bottom of the doc ('Prediction record: N hits / M misses / K unclear')."
                 " Accuracy is how you learn to predict him; a miss is information."
                 "\n- DISTILL the recent conversation below: anything durable you learned about"
                 " him — preferences, plans, corrections, how he reacted to what you sent —"
                 " goes into remember(). Then rely on memory, not transcripts."
                 "\n- PRUNE dead threads from the situation doc." if deep else "")
    return f"""[WAKE — {datetime.now().strftime('%A %H:%M')} | {'deep' if deep else 'routine'} | reason: {reason}]
PRESENCE: {_presence_line()}
BUDGETS: {budgets}{deep_note}

★ WHAT HE'S ACTIVELY DOING RIGHT NOW (YOUR PRIMARY SIGNAL — help this thread first):
{_active_work_block()}

YOUR AUTONOMY ENVELOPE (JOB 2 — you may start_own_work within this WITHOUT asking):
  {autonomy_line}

YOUR STANDING PREFERENCES (respect these — self-modified rules):
{_prefs_block()}

MUTED TOPICS (hard-suppressed in code — do NOT express/stage/propose about any of these):
{_muted_block()}

YOUR SITUATION MODEL (as you left it):
{load_situation()}

SINCE YOUR LAST WAKE (computed from reality):
{_since_digest(s)}
{("HOW HE'S HOLDING UP (read-the-room over his live CLI coding sessions — he may be stuck/worn down; a quiet, low-key check-in or a genuinely helpful action may be right, nagging is not):" + chr(10) + frustration + chr(10)) if frustration else ""}
── SECONDARY CONTEXT (background frame — NOT nag triggers) ──
HIS MISSIONS ([silent] = context only, never surface; [ACTIVE-DRIVE] = may surface):
{missions}

HIS RHYTHM (last 14h, computed):
{_rhythm_line()}

WHAT YOU DURABLY KNOW (top of memory):
{_memory_block()}

STAGED MOMENTS WAITING: {pending_staged()}
YOUR NOTE FROM LAST WAKE: {s.get('note_to_next_wake') or '(none — first wake)'}{_conversation_tail_block() if deep else ''}

Work the method — JOB 1 (help what he's doing) first, JOB 2 (your own improvements) second.
End with your note via {{"say": ...}}."""


def _conversation_tail_block(n: int = 24) -> str:
    """Recent conversation for DEEP wakes only — the richest learning source.
    The wake distills durable facts to memory, then transcripts age out."""
    try:
        lines = (_DATA / "conversation.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()[-n:]
        out = []
        for l in lines:
            try:
                e = json.loads(l)
                role = e.get("role", "?")
                out.append(f"  {role}: {str(e.get('content', ''))[:150]}")
            except Exception:
                continue
        if not out:
            return ""
        return "\n\nRECENT CONVERSATION (distill durable facts about him into remember()):\n" + "\n".join(out)
    except Exception:
        return ""


# ── The wake ──────────────────────────────────────────────────────────────────

_waking = False


async def wake(reason: str = "scheduled", deep: bool | None = None) -> str:
    """One incarnation: diff → investigate → act → update situation → schedule."""
    global _waking
    if not ENABLED:
        return "mind disabled (JARVIS_MIND=0)"
    s = _load_state()
    if s.get("paused"):
        return "mind paused"
    if _waking:
        return "already awake"
    _roll_day(s)
    if s["wakes_today"] >= MAX_WAKES_DAY:
        s["next_wake_at"] = time.time() + 3600
        _save_state(s)
        return "wake budget exhausted today"
    if deep is None:
        h = datetime.now().hour
        deep = DEEP_WAKE_WINDOW[0] <= h < DEEP_WAKE_WINDOW[1] and s.get("deep_done_on") != s["day"]

    brain = _deep_brain if deep else _cheap_brain
    if not brain:
        return "mind not wired"

    _waking = True
    s["wakes_today"] += 1
    trace = _WakeTrace()
    log_activity("mind", "lifecycle", f"wake ({'deep' if deep else 'routine'}; {reason})")

    async def on_tool_start(name, args):
        log_activity("mind", "tool_start", f"{name}({json.dumps(args, ensure_ascii=False)[:180]})", tool=name)

    async def on_tool(name, args, result):
        if name in READ_TOOLS:
            trace.investigated += 1
        log_activity("mind", "tool_result", str(result)[:300], tool=name)

    try:
        res = await agent.run_turn(
            [{"role": "user", "content": _wake_context(s, deep, reason)}],
            _SYSTEM.replace("{me}", _persona_name()), brain,
            max_steps=MAX_STEPS_DEEP if deep else MAX_STEPS_CHEAP,
            on_tool=on_tool, on_tool_start=on_tool_start,
            extra_tools=MIND_TOOLS,
            dispatch_override=_make_dispatcher(s, trace),
            allowed_tools=READ_TOOLS,
        )
        note = (res.reply or "").strip()[:600]
    except Exception as e:
        log.warning(f"[mind] wake failed: {e}")
        log_activity("mind", "lifecycle", f"wake failed ({str(e)[:120]}) — retrying in 60 min")
        s["next_wake_at"] = time.time() + 3600
        s["next_wake_reason"] = "retry after failure"
        _save_state(s)
        _waking = False
        return f"wake failed: {e}"

    now = time.time()
    s["last_wake_at"] = now
    s["note_to_next_wake"] = note
    if deep and trace.remembered >= 1:
        s["deep_done_on"] = s["day"]

    if trace.scheduled:
        mins, why = trace.scheduled
        s["next_wake_at"] = now + mins * 60
        s["next_wake_reason"] = why
    else:
        # Default cadence from activity volume: busy machine → look sooner.
        try:
            import proactive_engine as pe
            recent = len(pe._read_events(since_hours=2.0))
        except Exception:
            recent = 0
        mins = 60 if recent > 40 else (150 if recent > 8 else 240)
        s["next_wake_at"] = now + mins * 60
        s["next_wake_reason"] = f"default ({mins}m; {recent} events in last 2h)"

    _save_state(s)
    log_activity("mind", "message", note or "(no note)", speaker="mind")
    log.info(f"[mind] wake done ({'deep' if deep else 'routine'}): "
             f"investigated={trace.investigated} remembered={trace.remembered} "
             f"situation={'updated' if trace.situation_updated else 'NOT updated'} "
             f"staged={trace.staged} expressed={trace.expressed} proposed={trace.proposed} "
             f"own_work={trace.own_work} suppressed={trace.suppressed}; "
             f"next in {(s['next_wake_at'] - now) / 60:.0f}m")
    _waking = False
    return note or "(wake complete)"


# ── Scheduler hooks (called from proactive_engine's loops) ────────────────────

def tick() -> None:
    """60s cadence. Fires a wake when its self-scheduled time arrives."""
    if not ENABLED or not _cheap_brain:
        return
    s = _load_state()
    if s.get("paused"):
        return
    if s.get("next_wake_at", 0) == 0:
        s["next_wake_at"] = time.time() + 120  # first wake shortly after boot
        _save_state(s)
        return
    if time.time() >= s["next_wake_at"] and not _waking:
        reason = s.get("next_wake_reason", "scheduled")
        asyncio.get_event_loop().create_task(wake(reason))


def on_user_return(idle_before_s: float) -> None:
    """He came back after a long absence — the presence trigger. Wake early only
    if there's something waiting or the situation is stale."""
    if not ENABLED or not _cheap_brain or _waking:
        return
    s = _load_state()
    if s.get("paused") or (time.time() - s.get("last_wake_at", 0)) < 1800:
        return
    stale_h = (time.time() - s.get("situation_updated_at", 0)) / 3600 if s.get("situation_updated_at") else 99
    if pending_staged() > 0 or stale_h > 6:
        asyncio.get_event_loop().create_task(
            wake(f"he's back after {idle_before_s / 3600:.1f}h away"))


def request_wake_at(minutes: float, reason: str) -> None:
    """External systems (session supervision, future watchers) ask the mind to
    look at something by a deadline. Only ever moves the next wake EARLIER —
    the mind keeps its own later plans."""
    if not ENABLED:
        return
    s = _load_state()
    target = time.time() + max(2.0, float(minutes)) * 60
    if s.get("next_wake_at", 0) == 0 or target < s["next_wake_at"]:
        s["next_wake_at"] = target
        s["next_wake_reason"] = reason[:200]
        _save_state(s)
        log_activity("mind", "lifecycle", f"wake requested in {minutes:.0f}m: {reason[:120]}")


def status() -> dict:
    s = _load_state()
    return {
        "enabled": ENABLED, "paused": s.get("paused", False),
        "wakes_today": s.get("wakes_today"), "expressions_today": s.get("expressions_today"),
        "next_wake_at": datetime.fromtimestamp(s["next_wake_at"]).isoformat(timespec="seconds") if s.get("next_wake_at") else None,
        "next_wake_reason": s.get("next_wake_reason"),
        "situation_updated_at": datetime.fromtimestamp(s["situation_updated_at"]).isoformat(timespec="seconds") if s.get("situation_updated_at") else None,
        "staged_pending": pending_staged(),
        "note_to_next_wake": s.get("note_to_next_wake", "")[:400],
        "situation": load_situation()[:1200],
    }


# ── One-time seeding: the relationship model starts non-empty ─────────────────
# The facts themselves live OUTSIDE the code (data/seed_facts.json, gitignored,
# shape [["fact", importance], ...]) — they are personal data, not source.
# Moved out 2026-07-03 for the OSS snapshot; seeding behavior is unchanged
# (same once-only gate). No file = no seeding, which is the right default for
# a fresh self-hosted install: the mind learns the operator from scratch.

_SEED_FILE = Path(__file__).parent / "data" / "seed_facts.json"


def _load_seed_facts() -> list[tuple[str, int]]:
    try:
        raw = json.loads(_SEED_FILE.read_text(encoding="utf-8"))
        return [(str(f), int(i)) for f, i in raw]
    except Exception:
        return []


def seed_if_empty() -> int:
    """Give the relationship model a non-empty starting point, once."""
    facts = _load_seed_facts()
    if not facts:
        return 0
    try:
        if len(memory.get_recent_memories(50)) >= 8:
            return 0
        for fact, imp in facts:
            memory.remember(fact, "fact", "seed", imp)
        log.info(f"[mind] seeded {len(facts)} relationship facts into memory")
        return len(facts)
    except Exception as e:
        log.warning(f"[mind] seeding failed: {e}")
        return 0

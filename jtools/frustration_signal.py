"""Frustration / distress signal detection over his AI-CLI sessions (2026-07-20).

WHAT THIS IS
------------
A zero-LLM read-the-room sensor. It scans a recent window of his manual
Claude Code / Codex / Grok transcripts (the same files jtools/cli_chats_tool.py
already reads) and estimates how stuck or worn down he is right now — so the
proactive engine / mind can *notice* and gently check in, instead of waiting
for him to say something.

WHAT THIS IS NOT
----------------
- NOT a profanity filter, censor, or content gate. Profanity is ONE weak marker
  among several. Nothing is ever blocked, filtered, hidden, or altered.
- NOT a notification spammer. This module NEVER pushes a notification itself.
  When it detects a genuine acute episode it logs an event and asks the MIND to
  wake and look — the mind then decides, under its own budgets and his
  crater-level notification standing (proactive_delivery pref: ~80% Proactive
  tab, ~20% push, never cry wolf), whether and how to reach out. The default
  outcome is a quiet in-app surface, essentially never a push.

HOW THE SIGNAL REACHES THE PROACTIVE ENGINE
-------------------------------------------
Three additive channels, all read by the EXISTING pipeline (no parallel one):
  1. signal_block()  — a compact text block injected into the twice-daily
     synthesis prompt and the mind's wake context (only when elevated+; empty
     string otherwise, so quiet periods cost nothing). Passive: the model reads
     it and uses its own judgment.
  2. log_event("frustration_signal", ...) — on a genuine acute episode, written
     to data/events.jsonl like every other proactive event, so it shows up in
     the synthesis event scan and the mind's since-digest.
  3. mind.request_wake_at(...) — on a genuine acute episode, nudges the mind to
     wake soon and look. Only ever moves the wake EARLIER; the mind keeps its
     own plans and its own budgets.

TUNING
------
Everything worth tuning is at the top of this file: MARKERS (the weighted
marker list — add/remove phrases freely) and CONFIG (score thresholds, window
size, debounce/cooldown). Change those; nothing else needs touching.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path

log = logging.getLogger("jarvis.tools.frustration")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_STATE_FILE = _DATA_DIR / "frustration_state.json"


# ══════════════════════════════════════════════════════════════════════════════
#  TUNABLES — this is the whole knob panel. Edit here, nowhere else.
# ══════════════════════════════════════════════════════════════════════════════

# Each marker contributes weighted points to a 0-100 frustration score. Points
# per hit and a per-marker cap (so one marker firing a lot can't dominate). The
# regexes are matched case-insensitively against his USER turns in the window.
# Add phrases you notice him actually using; this list is meant to grow.
MARKERS: dict[str, dict] = {
    # Profanity — genuine but WEAK on its own (he swears casually when things go
    # fine too). Low weight, low cap: it nudges, it never decides.
    "profanity": {
        "weight": 8, "cap": 24,
        "patterns": [
            r"\bf+u+c+k+", r"\bsh+i+t+", r"\bda+mn+\b", r"\bcra+p+\b",
            r"\bbullshit\b", r"\bpiss(?:ed|ing)?\b", r"\bass(?:hole)?\b",
            r"\bgoddamn?\b", r"\bhell\b", r"\bwtf\b", r"\bffs\b", r"\bstfu\b",
        ],
    },
    # Direct expressions of being stuck / it-still-doesn't-work. The strongest
    # single class of signal — these are him telling the tool he's blocked.
    "stuck": {
        "weight": 11, "cap": 44,
        "patterns": [
            r"still (?:broken|not working|doesn'?t work|failing|fails|wrong)",
            r"why (?:won'?t|wont|doesn'?t|does not|isn'?t|is not|can'?t)",
            r"(?:doesn'?t|does not|still doesn'?t) work",
            r"not working", r"(?:same|the same) (?:error|issue|problem|thing)",
            r"keeps? (?:failing|breaking|crashing|erroring)",
            r"nothing (?:works|is working|i try)",
            r"(?:makes no|no) sense", r"i (?:don'?t|do not) (?:get|understand) (?:it|why)",
            r"stuck (?:on|with)", r"can'?t (?:get|figure|make) (?:it|this)",
            r"(?:this|it) (?:is|'?s) (?:broken|busted|hopeless)",
            r"i give up", r"i'?m done", r"forget it",
        ],
    },
    # Exasperation / venting interjections — tone markers.
    "exasperation": {
        "weight": 7, "cap": 28,
        "patterns": [
            r"\bu+g+h+\b", r"\ba+r+g+h*\b", r"\bgr+r+\b", r"\bomg\b",
            r"come on", r"seriously\?*", r"are you kidding",
            r"for (?:the love|god'?s sake|fuck'?s sake|christ'?s sake)",
            r"oh my god", r"jesus christ", r"\bwhat the\b", r"\bagain\?*",
            r"\bplease\b.*\bjust\b", r"just (?:work|do it|fix)",
        ],
    },
    # ALL-CAPS shouting is handled specially in scoring (needs the raw message,
    # not a substring match) but its weights live here so it stays tunable.
    "allcaps_urgency": {"weight": 10, "cap": 30},
    # Re-trying the same failing request over and over (near-duplicate user
    # turns) — computed from turn similarity, weights here.
    "repeated_retry": {"weight": 12, "cap": 36},
    # Long stall right after the assistant reported an error, then he comes back
    # — being stuck staring at a failure. Needs timestamps (Claude Code only).
    "stall_after_error": {"weight": 10, "cap": 20},
    # Rapid-fire escalation — many user turns bunched close together. Needs
    # timestamps (Claude Code only).
    "escalating_frequency": {"weight": 10, "cap": 20},
}

CONFIG = {
    # How far back a "recent window" of a session reaches, in minutes. Only turns
    # newer than this (when timestamps exist) are scored; without timestamps we
    # fall back to the last N turns (below).
    "window_minutes": 90.0,
    "window_turns_fallback": 30,     # when a transcript has no timestamps
    "min_user_turns": 3,             # don't score a session with too little to go on

    # Score → level thresholds (0-100).
    "level_mild": 25,
    "level_elevated": 50,
    "level_acute": 70,

    # signal_block() surfaces sessions at or above this level (passive channel).
    "surface_at": "elevated",
    # check_and_signal() fires the ACTIVE channel (event + mind wake) only at or
    # above this level. Deliberately high — bias hard toward silence.
    "fire_at": "acute",

    # Debounce / cooldown — one signal per genuine episode, never nag.
    "per_session_cooldown_h": 6.0,   # same session can't re-fire within this…
    "global_min_gap_h": 3.0,         # …and no two signals globally inside this
    "require_calm_between": True,    # a session must drop to <=mild after firing
                                     # before it's allowed to fire again
    "calm_level": "mild",

    # How often the periodic checker actually does work (it's called from the
    # 60s scheduler but self-throttles to this).
    "scan_interval_s": 300.0,
    "lookback_hours": 6.0,           # which sessions are candidates at all

    # When an acute episode fires, ask the mind to look this soon (minutes).
    "wake_in_minutes": 12.0,
}

# Assistant-side error markers, for the stall-after-error timing marker.
_ERROR_RE = re.compile(
    r"\b(error|traceback|exception|failed|failure|cannot|command not found|"
    r"stack trace|assert(?:ion)?|undefined|not found|denied)\b", re.I)

_LEVEL_ORDER = ["calm", "mild", "elevated", "acute"]


# ══════════════════════════════════════════════════════════════════════════════
#  Scoring
# ══════════════════════════════════════════════════════════════════════════════

def _level_for(score: float) -> str:
    if score >= CONFIG["level_acute"]:
        return "acute"
    if score >= CONFIG["level_elevated"]:
        return "elevated"
    if score >= CONFIG["level_mild"]:
        return "mild"
    return "calm"


def _ge(level: str, threshold: str) -> bool:
    """level >= threshold in the calm<mild<elevated<acute ordering."""
    try:
        return _LEVEL_ORDER.index(level) >= _LEVEL_ORDER.index(threshold)
    except ValueError:
        return False


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", (text or "").lower()).strip()


def _is_shout(text: str) -> bool:
    """A message that's mostly SHOUTING — enough letters, and the vast majority
    of them uppercase. Short 'OK'/'YES' don't count."""
    letters = [c for c in (text or "") if c.isalpha()]
    if len(letters) < 8:
        return False
    upper = sum(1 for c in letters if c.isupper())
    return upper / len(letters) >= 0.75


def score_turns(turns: list[dict]) -> dict:
    """Score a window of conversation turns for frustration/distress.

    `turns`: ordered oldest→newest list of {"role": "user"|"assistant",
    "ts": float|None, "text": str}. Only USER turns drive most markers; assistant
    turns are used for the stall-after-error timing marker.

    Returns {"score": 0-100, "level": str, "markers": {name: points},
    "hits": {name: [examples]}, "user_turns": int}.
    """
    users = [t for t in turns if t.get("role") == "user" and (t.get("text") or "").strip()]
    breakdown: dict[str, float] = {}
    hits: dict[str, list[str]] = {}

    def _add(name: str, points: float, example: str = "") -> None:
        if points <= 0:
            return
        cap = MARKERS[name]["cap"]
        cur = breakdown.get(name, 0.0)
        breakdown[name] = min(cap, cur + points)
        if example:
            hits.setdefault(name, [])
            if len(hits[name]) < 3:
                hits[name].append(example.strip()[:80])

    # ── Text markers over user turns ──────────────────────────────────────────
    for t in users:
        text = t["text"]
        low = text.lower()
        for name in ("profanity", "stuck", "exasperation"):
            w = MARKERS[name]["weight"]
            for pat in MARKERS[name]["patterns"]:
                if re.search(pat, low):
                    _add(name, w, text)
                    break  # one hit per marker per message — don't stack a rant
        if _is_shout(text):
            _add("allcaps_urgency", MARKERS["allcaps_urgency"]["weight"], text)

    # ── Repeated retries (near-duplicate user turns) ──────────────────────────
    normed = [_norm(t["text"]) for t in users]
    for i in range(1, len(normed)):
        a = normed[i]
        if len(a) < 6:
            continue
        for j in range(i):
            b = normed[j]
            if len(b) < 6:
                continue
            if a == b or difflib.SequenceMatcher(None, a, b).ratio() >= 0.85:
                _add("repeated_retry", MARKERS["repeated_retry"]["weight"],
                     users[i]["text"])
                break

    # ── Time-based markers (only when timestamps exist) ───────────────────────
    ut = [t for t in users if isinstance(t.get("ts"), (int, float)) and t["ts"]]
    if len(ut) >= 4:
        gaps = [ut[i]["ts"] - ut[i - 1]["ts"] for i in range(1, len(ut))]
        gaps = [g for g in gaps if g >= 0]
        if gaps:
            gaps.sort()
            median = gaps[len(gaps) // 2]
            if median < 90:
                _add("escalating_frequency", MARKERS["escalating_frequency"]["weight"] * 2)
            elif median < 180:
                _add("escalating_frequency", MARKERS["escalating_frequency"]["weight"])

    # Stall after error: assistant reports an error, then a long gap before he
    # replies (he's stuck staring at it), or he replies with any frustration.
    timed = [t for t in turns if isinstance(t.get("ts"), (int, float)) and t["ts"]]
    for i in range(1, len(timed)):
        prev, cur = timed[i - 1], timed[i]
        if (prev.get("role") == "assistant" and cur.get("role") == "user"
                and _ERROR_RE.search(prev.get("text") or "")):
            gap = cur["ts"] - prev["ts"]
            if gap >= 180:  # 3+ min sitting on an error
                _add("stall_after_error", MARKERS["stall_after_error"]["weight"],
                     cur.get("text", ""))

    score = min(100.0, sum(breakdown.values()))
    return {
        "score": round(score, 1),
        "level": _level_for(score),
        "markers": {k: round(v, 1) for k, v in breakdown.items()},
        "hits": hits,
        "user_turns": len(users),
    }


# ══════════════════════════════════════════════════════════════════════════════
#  Collecting + scoring real sessions (reuses cli_chats_tool)
# ══════════════════════════════════════════════════════════════════════════════

def _timed_turns_for(rec: dict) -> list[dict]:
    """Ordered [{role, ts, text}] for one session record from cli_chats_tool.
    Claude Code carries per-line timestamps (full time-based markers); Codex and
    Grok degrade to text-only markers (ts=None) — documented, acceptable."""
    from jtools import cli_chats_tool as cc  # local import: avoid load-order issues
    tool = rec.get("tool")
    path = Path(rec.get("path", ""))
    out: list[dict] = []
    try:
        if tool == "claude-code":
            if not path.exists() or path.stat().st_size > cc._MAX_FILE:
                return []
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    is_user = '"type":"user"' in line
                    is_asst = '"type":"assistant"' in line
                    if not (is_user or is_asst):
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    ts = cc._iso_to_ts(d.get("timestamp", "")) or None
                    if d.get("type") == "user":
                        text = cc._cc_human_text(d)
                        if text:
                            out.append({"role": "user", "ts": ts, "text": text})
                    elif d.get("type") == "assistant":
                        text = cc._cc_assistant_text(d)
                        if text:
                            out.append({"role": "assistant", "ts": ts, "text": text})
        else:
            parser = {"codex": cc._parse_codex}.get(tool)
            deep = parser(path, deep=True) if parser else cc._parse_grok(path.parent, deep=True)
            for role, text in (deep or {}).get("turns", []) or []:
                out.append({"role": role.lower(), "ts": None, "text": text})
    except Exception as e:
        log.debug(f"[frustration] parse failed for {path}: {e}")
    return out


def _window(turns: list[dict]) -> list[dict]:
    """Trim to the recent window: by time when we have timestamps, else the last
    N turns."""
    timed = [t for t in turns if isinstance(t.get("ts"), (int, float)) and t["ts"]]
    if timed:
        cutoff = time.time() - CONFIG["window_minutes"] * 60
        win = [t for t in turns if not t.get("ts") or t["ts"] >= cutoff]
        # keep it from being empty if all turns are older than the window but the
        # session is still "recent" by mtime — fall back to the tail
        return win or turns[-CONFIG["window_turns_fallback"]:]
    return turns[-CONFIG["window_turns_fallback"]:]


def scan(hours: float | None = None) -> list[dict]:
    """Score every recent CLI session. Returns a list of result dicts (one per
    session that met the minimum-turns bar), newest-active first, each carrying
    its cli_chats record under 'session'. Zero LLM calls."""
    from jtools import cli_chats_tool as cc
    hrs = float(hours if hours is not None else CONFIG["lookback_hours"])
    try:
        recs = cc.collect_cli_chats(hours=hrs, limit=20)
    except Exception as e:
        log.warning(f"[frustration] collect failed: {e}")
        return []
    results = []
    for rec in recs:
        turns = _window(_timed_turns_for(rec))
        r = score_turns(turns)
        if r["user_turns"] < CONFIG["min_user_turns"]:
            continue
        r["session"] = {
            "id": rec.get("id", ""), "tool": rec.get("tool", ""),
            "project": rec.get("project", ""), "branch": rec.get("branch", ""),
            "last_active": rec.get("last_active", 0.0),
            "last_user": rec.get("last_user", ""),
        }
        results.append(r)
    results.sort(key=lambda x: x["session"]["last_active"], reverse=True)
    return results


# ══════════════════════════════════════════════════════════════════════════════
#  Channel 1 — passive text block for synthesis + mind wake
# ══════════════════════════════════════════════════════════════════════════════

def _describe(r: dict) -> str:
    s = r["session"]
    top = sorted(r["markers"].items(), key=lambda kv: kv[1], reverse=True)
    marker_str = ", ".join(f"{k}" for k, _ in top[:3]) or "mixed"
    proj = s.get("project") or s.get("tool") or "a session"
    return (f"  {proj} ({s.get('tool')}): {r['level']} (score {r['score']:.0f}) — "
            f"signals: {marker_str}; last: \"{(s.get('last_user') or '')[:60]}\"")


def signal_block() -> str:
    """Compact block for the synthesis prompt and mind wake context. Empty string
    unless at least one session is at/above CONFIG['surface_at'] — quiet periods
    cost zero prompt space. Passive: the model reads it and uses its judgment;
    this is NOT an instruction to notify."""
    try:
        results = scan()
    except Exception:
        return ""
    flagged = [r for r in results if _ge(r["level"], CONFIG["surface_at"])]
    if not flagged:
        return ""
    lines = [_describe(r) for r in flagged[:4]]
    return ("  (read-the-room, zero-LLM — how stuck/worn-down his live coding "
            "sessions look. NOT a push directive: use judgment. A gentle, "
            "low-key check-in or a quietly helpful note may be warranted; nagging "
            "is not.)\n" + "\n".join(lines))


# ══════════════════════════════════════════════════════════════════════════════
#  Debounce state
# ══════════════════════════════════════════════════════════════════════════════

def _load_state() -> dict:
    if not _STATE_FILE.exists():
        return {"global_last_signal_at": 0.0, "sessions": {}}
    try:
        return json.loads(_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"global_last_signal_at": 0.0, "sessions": {}}


def _save_state(st: dict) -> None:
    try:
        tmp = _STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(_STATE_FILE)
    except Exception as e:
        log.warning(f"[frustration] state save failed: {e}")


def _fireable(r: dict, st: dict, now: float) -> bool:
    """Debounce gate: acute enough, per-session cooldown clear (with a calm in
    between if required), and the global rate limit clear."""
    if not _ge(r["level"], CONFIG["fire_at"]):
        return False
    if now - st.get("global_last_signal_at", 0.0) < CONFIG["global_min_gap_h"] * 3600:
        return False
    rec = st["sessions"].get(r["session"]["id"])
    if rec:
        if now - rec.get("last_signal_at", 0.0) < CONFIG["per_session_cooldown_h"] * 3600:
            return False
        if CONFIG["require_calm_between"] and not rec.get("calmed_since_signal", False):
            return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Channel 2 + 3 — the active, debounced signal (event + mind wake). Never pushes.
# ══════════════════════════════════════════════════════════════════════════════

_last_scan_at = 0.0


async def check_and_signal(force: bool = False) -> dict | None:
    """Periodic checker, called from the proactive scheduler (self-throttled to
    CONFIG['scan_interval_s']). Scans sessions, updates debounce bookkeeping, and
    — only on a genuine acute, debounce-clear episode — logs a 'frustration_signal'
    event and asks the mind to wake and look.

    It NEVER sends a notification. The mind decides what (if anything) reaches his
    phone, under its own budgets and his notification standing. Returns the fired
    result dict, or None."""
    global _last_scan_at
    now = time.time()
    if not force and now - _last_scan_at < CONFIG["scan_interval_s"]:
        return None
    _last_scan_at = now

    results = scan()
    st = _load_state()

    # Update calm bookkeeping for every scored session first (so cooldowns can lift).
    calm = CONFIG["calm_level"]
    for r in results:
        sid = r["session"]["id"]
        rec = st["sessions"].get(sid)
        if rec and not rec.get("calmed_since_signal", False):
            if not _ge(r["level"], calm) or r["level"] == "calm":
                # level <= calm (i.e. calm, or mild when calm_level==mild)
                if _LEVEL_ORDER.index(r["level"]) <= _LEVEL_ORDER.index(calm):
                    rec["calmed_since_signal"] = True

    # Pick the single most acute fireable session — one signal per episode, never
    # a burst across sessions.
    candidates = [r for r in results if _fireable(r, st, now)]
    fired = None
    if candidates:
        fired = max(candidates, key=lambda r: r["score"])
        sid = fired["session"]["id"]
        proj = fired["session"].get("project") or fired["session"].get("tool") or "?"
        markers = ", ".join(sorted(fired["markers"], key=lambda k: -fired["markers"][k])[:4])

        try:
            import proactive_engine as pe
            pe.log_event(
                "frustration_signal",
                session=sid[:12], project=proj, tool=fired["session"].get("tool"),
                score=fired["score"], level=fired["level"], markers=markers,
                last_user=(fired["session"].get("last_user") or "")[:120],
            )
        except Exception as e:
            log.warning(f"[frustration] log_event failed: {e}")

        try:
            import mind
            mind.request_wake_at(
                CONFIG["wake_in_minutes"],
                f"he looks stuck/worn-down in {proj} ({fired['level']}, "
                f"score {fired['score']:.0f}; {markers}) — look, and decide if a "
                f"low-key check-in helps. Do NOT nag.")
        except Exception as e:
            log.debug(f"[frustration] mind wake request skipped: {e}")

        st["global_last_signal_at"] = now
        st["sessions"][sid] = {
            "last_signal_at": now, "calmed_since_signal": False,
            "last_level": fired["level"], "last_score": fired["score"],
            "project": proj,
        }
        log.info(f"[frustration] acute episode in {proj}: score {fired['score']:.0f} "
                 f"({markers}) — event logged + mind wake requested (no push)")

    # Prune old session records (>7d) so the state file doesn't grow forever.
    week = now - 7 * 86400
    st["sessions"] = {k: v for k, v in st["sessions"].items()
                      if v.get("last_signal_at", 0.0) >= week}
    _save_state(st)
    return fired


# ══════════════════════════════════════════════════════════════════════════════
#  Read-only tool (introspection / testing) — auto-registered via import_all_tools
# ══════════════════════════════════════════════════════════════════════════════

try:
    from tool_registry import tool

    @tool(
        name="check_frustration",
        description=(
            "Read-the-room check: scan his recent AI-CLI coding sessions (Claude "
            "Code/Codex/Grok) for signs he's stuck or worn down — profanity, "
            "ALL-CAPS, repeated failing retries, 'still broken'/'why won't this "
            "work', stalls after errors. Zero-LLM, read-only. Use to gauge whether "
            "a gentle check-in is warranted; it never notifies on its own."),
        parameters={
            "hours": {"type": "number",
                      "description": "Look-back window in hours (default 6)."},
        },
    )
    async def check_frustration(hours: float = 6.0) -> str:
        results = scan(hours=float(hours or 6.0))
        if not results:
            return f"No scorable CLI sessions in the last {hours:g}h."
        flagged = [r for r in results if _ge(r["level"], "elevated")]
        lines = [f"Scanned {len(results)} session(s); "
                 f"{len(flagged)} showing elevated+ frustration:"]
        for r in results[:6]:
            s = r["session"]
            top = ", ".join(sorted(r["markers"], key=lambda k: -r["markers"][k])[:3]) or "—"
            lines.append(f"  {s.get('project') or s.get('tool')} ({s.get('tool')}): "
                         f"{r['level']} · score {r['score']:.0f} · {top}")
        return "\n".join(lines)
except Exception:  # tool_registry unavailable (e.g. standalone unit test) — fine
    pass

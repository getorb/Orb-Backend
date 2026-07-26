"""The user's AI-CLI conversations, visible to Orb — zero-LLM collection (2026-07-10).

The user's real work happens in manual CLI sessions (Claude Code, Codex, Grok)
all over this PC, and until now Orb was blind to every one of them: the
synthesis planned their day without knowing they'd spent the evening deep in a
bug fix two terminals over. These collectors read the
transcripts each CLI already writes to disk — no model calls, no new logging,
same philosophy as the rest of the data pipeline: collect cheap and raw,
judge only at synthesis time.

Sources (formats verified against real files on this machine, 2026-07-10):
  • Claude Code — ~/.claude/projects/<cwd-slug>/<uuid>.jsonl, one JSON event
    per line; human turns are type:"user" lines whose message.content is text
    (tool_results also ride type:"user" — filtered by shape + origin.kind).
  • Codex CLI  — ~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl; first line is
    session_meta (cwd, originator), messages are response_item payloads.
  • Grok CLI   — ~/.grok/sessions/<url-encoded-cwd>/<id>/ dirs; summary.json
    already carries a generated title, cwd, branch, message count.

Exclusions, deliberate: Orb's OWN brain transcripts (~/.orb_brain cwd — that
IS the live conversation, feeding it back would be recursion) and scratchpad/
temp-dir sessions. Spawned/headless sessions (run_claude_code, codex exec)
stay IN, labeled by origin where the format says so — Orb supervises those
too, and the project name tells the story either way.

Read tools are conversation-context surfaces, not secrets surfaces: they
return chat text verbatim, so they never open credential FILES — but anything
he pasted into a chat is, by definition, already conversation.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

from tool_registry import tool

log = logging.getLogger("orb.tools.cli_chats")

CC_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
GROK_ROOT = Path.home() / ".grok" / "sessions"

# CC project-dir name substrings that are noise or recursion, never "his work".
_CC_EXCLUDE = ("--orb-brain", "AppData-Local-Temp", "-Temp-claude-")

# Sessions whose FIRST prompt is one of Orb's own system prompts are Orb's
# internals (mind wakes, persona-seeded delegations) — feeding them back into
# the synthesis that drives the next wake is an echo chamber, and the mind's
# activity is already visible via mind_status / the activity log. Dropped.
# (Scheduled-task and run_claude_code spawns carry plain task text — no
# marker — and stay in: delegated work IS his work.) The persona lines are the
# most stable tell: every Orb conversational delegation (grok/codex) embeds
# the shared conversational-feel block verbatim.
_ORB_MARKERS = (
    "You are Orb", "You are Orb", "Orb's mind",
    "a real presence the person actually talks WITH",
    "real engagement is the cardinal sin",
)


def _is_orb_internal(first_text: str) -> bool:
    head = (first_text or "")[:600]
    return any(m in head for m in _ORB_MARKERS)

_SNIP = 110          # chars kept of any single message snippet in summaries
_MAX_FILE = 16_000_000  # refuse to iterate transcripts beyond this (runaway logs)

# ── tiny sync TTL cache (cache_util.ttl_cache is async-only) ─────────────────
_cache: dict[tuple, tuple[float, list]] = {}
_TTL = 300.0


def _trim(text: str, n: int = _SNIP) -> str:
    return " ".join((text or "").split())[:n]


def _local_hhmm(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    return d.strftime("%H:%M") if d.date() == datetime.now().date() else d.strftime("%m-%d %H:%M")


def _iso_to_ts(s: str) -> float:
    try:
        return datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _project_of(cwd: str) -> str:
    try:
        return Path(cwd).name or cwd
    except Exception:
        return cwd or "?"


# ── Claude Code ───────────────────────────────────────────────────────────────

def _cc_human_text(d: dict) -> str | None:
    """Text of a type:'user' line iff it's a real human turn (not a tool_result,
    not meta, not a sidechain/subagent, not a command wrapper)."""
    if d.get("isSidechain") or d.get("isMeta"):
        return None
    kind = (d.get("origin") or {}).get("kind")
    if kind and kind != "human":
        return None
    c = (d.get("message") or {}).get("content")
    text = None
    if isinstance(c, str):
        text = c
    elif isinstance(c, list):
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
            return None
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                text = b["text"]
                break
    if not text:
        return None
    t = text.lstrip()
    # Harness wrappers and synthetic markers, not him.
    if t.startswith("<") or t.startswith("Caveat:") or t.startswith("[Request interrupted"):
        return None
    return text


def _cc_assistant_text(d: dict) -> str | None:
    if d.get("isSidechain"):
        return None
    c = (d.get("message") or {}).get("content")
    if isinstance(c, list):
        for b in c:
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text"):
                return b["text"]
    elif isinstance(c, str):
        return c
    return None


def _parse_cc(path: Path, deep: bool = False) -> dict | None:
    if path.stat().st_size > _MAX_FILE:
        return None
    cwd = branch = ""
    first = last = ""
    started = 0.0
    n_user = 0
    turns: list[tuple[str, str]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            is_user = '"type":"user"' in line
            is_asst = deep and '"type":"assistant"' in line
            if not (is_user or is_asst):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if not cwd and d.get("cwd"):
                cwd, branch = d["cwd"], d.get("gitBranch") or ""
            if is_user and d.get("type") == "user":
                text = _cc_human_text(d)
                if text is None:
                    continue
                n_user += 1
                if not first:
                    first = text
                    started = _iso_to_ts(d.get("timestamp", ""))
                last = text
                if deep:
                    turns.append(("USER", text))
            elif is_asst and d.get("type") == "assistant":
                text = _cc_assistant_text(d)
                if text and deep:
                    turns.append(("ASSISTANT", text))
    if n_user == 0 or _is_orb_internal(first):
        return None
    rec = {
        "tool": "claude-code", "id": path.stem, "path": str(path),
        "cwd": cwd, "project": _project_of(cwd), "branch": branch,
        "started": started, "last_active": path.stat().st_mtime,
        "msgs": n_user, "title": _trim(first), "last_user": _trim(last),
        "origin": "cli",
    }
    if deep:
        rec["turns"] = turns
    return rec


# ── Codex CLI ─────────────────────────────────────────────────────────────────

def _parse_codex(path: Path, deep: bool = False) -> dict | None:
    if path.stat().st_size > _MAX_FILE:
        return None
    cwd = origin = sid = ""
    first = last = ""
    started = 0.0
    n_user = 0
    turns: list[tuple[str, str]] = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if '"session_meta"' in line and not cwd:
                try:
                    p = json.loads(line).get("payload", {})
                    cwd = p.get("cwd", "")
                    sid = p.get("id", "")
                    origin = "spawned" if "exec" in (p.get("originator") or "") else "cli"
                    started = _iso_to_ts(p.get("timestamp", ""))
                except Exception:
                    pass
                continue
            is_user = '"role":"user"' in line
            is_asst = deep and '"role":"assistant"' in line
            if not (is_user or is_asst):
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            p = d.get("payload", {})
            if p.get("type") != "message":
                continue
            text = ""
            for b in p.get("content") or []:
                if isinstance(b, dict) and b.get("text"):
                    text = b["text"]
                    break
            t = text.lstrip()
            if not t or t.startswith("<"):  # environment_context / permissions blocks
                continue
            if p.get("role") == "user":
                n_user += 1
                first = first or text
                last = text
                if deep:
                    turns.append(("USER", text))
            elif deep and p.get("role") == "assistant":
                turns.append(("ASSISTANT", text))
    if n_user == 0 or _is_orb_internal(first):
        return None
    rec = {
        "tool": "codex", "id": sid or path.stem, "path": str(path),
        "cwd": cwd, "project": _project_of(cwd), "branch": "",
        "started": started, "last_active": path.stat().st_mtime,
        "msgs": n_user, "title": _trim(first), "last_user": _trim(last),
        "origin": origin or "cli",
    }
    if deep:
        rec["turns"] = turns
    return rec


# ── Grok CLI ──────────────────────────────────────────────────────────────────

def _parse_grok(sess_dir: Path, deep: bool = False) -> dict | None:
    summ = sess_dir / "summary.json"
    if not summ.exists():
        return None
    try:
        s = json.loads(summ.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    # Orb-internal check: grok splits the prompt across user messages — a
    # <user_info>/<git_status>/rules wrapper first, the real <user_query>
    # (persona included, for Orb spawns) second — so scan the first few user
    # lines with a wide cap instead of just the head of the first.
    hist = sess_dir / "chat_history.jsonl"
    try:
        if hist.exists():
            seen_user = 0
            with hist.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if '"type":"user"' not in line:
                        continue
                    if any(m in line[:30000] for m in _ORB_MARKERS):
                        return None
                    seen_user += 1
                    if seen_user >= 3:
                        break
    except Exception:
        pass
    cwd = (s.get("info") or {}).get("cwd") or urllib.parse.unquote(sess_dir.parent.name)
    rec = {
        "tool": "grok", "id": s.get("info", {}).get("id", sess_dir.name),
        "path": str(sess_dir / "chat_history.jsonl"),
        "cwd": cwd, "project": _project_of(cwd), "branch": s.get("head_branch") or "",
        "started": _iso_to_ts(s.get("created_at", "")),
        "last_active": summ.stat().st_mtime,
        "msgs": int(s.get("num_chat_messages") or 0),
        "title": _trim(s.get("generated_title") or s.get("session_summary") or ""),
        "last_user": "", "origin": "cli",
    }
    if deep:
        turns: list[tuple[str, str]] = []
        hist = sess_dir / "chat_history.jsonl"
        if hist.exists() and hist.stat().st_size <= _MAX_FILE:
            with hist.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    try:
                        d = json.loads(line)
                    except Exception:
                        continue
                    if d.get("type") not in ("user", "assistant"):
                        continue
                    c = d.get("content")
                    text = c if isinstance(c, str) else ""
                    if isinstance(c, list):
                        for b in c:
                            if isinstance(b, dict) and b.get("text"):
                                text = b["text"]
                                break
                    t = (text or "").lstrip()
                    if not t or t.startswith("<"):
                        continue
                    turns.append(("USER" if d["type"] == "user" else "ASSISTANT", text))
        rec["turns"] = turns
    return rec


# ── Collection ────────────────────────────────────────────────────────────────

def collect_cli_chats(hours: float = 24.0, limit: int = 20) -> list[dict]:
    """Recent CLI sessions across all three tools, newest-activity first.
    Zero-LLM, disk-only; 5-min TTL cache because conversation context calls
    this every single turn."""
    key = (round(hours, 1), limit)
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]

    cutoff = time.time() - hours * 3600
    out: list[dict] = []

    try:
        if CC_ROOT.exists():
            for proj_dir in CC_ROOT.iterdir():
                if not proj_dir.is_dir() or any(x in proj_dir.name for x in _CC_EXCLUDE):
                    continue
                for f in proj_dir.glob("*.jsonl"):
                    try:
                        if f.stat().st_mtime < cutoff:
                            continue
                        rec = _parse_cc(f)
                        if rec:
                            out.append(rec)
                    except Exception:
                        continue
    except Exception as e:
        log.warning(f"[cli_chats] claude-code scan failed: {e}")

    try:
        if CODEX_ROOT.exists():
            for f in CODEX_ROOT.rglob("rollout-*.jsonl"):
                try:
                    if f.stat().st_mtime < cutoff:
                        continue
                    rec = _parse_codex(f)
                    if rec:
                        out.append(rec)
                except Exception:
                    continue
    except Exception as e:
        log.warning(f"[cli_chats] codex scan failed: {e}")

    try:
        if GROK_ROOT.exists():
            for cwd_dir in GROK_ROOT.iterdir():
                if not cwd_dir.is_dir():
                    continue
                for sess in cwd_dir.iterdir():
                    try:
                        if not sess.is_dir() or (sess / "summary.json").stat().st_mtime < cutoff:
                            continue
                        rec = _parse_grok(sess)
                        if rec:
                            out.append(rec)
                    except Exception:
                        continue
    except Exception as e:
        log.warning(f"[cli_chats] grok scan failed: {e}")

    out.sort(key=lambda r: r["last_active"], reverse=True)
    out = out[:limit]
    _cache[key] = (time.time(), out)
    return out


def _one_line(r: dict) -> str:
    tag = f"{r['tool']}·{r['origin']}" if r["origin"] != "cli" else r["tool"]
    br = f" @{r['branch']}" if r.get("branch") else ""
    return (f"  [{_local_hhmm(r['last_active'])}] {tag} in {r['project']}{br} "
            f"({r['msgs']} msg): {r['title']}")


def context_block(hours: float = 12.0, max_items: int = 3) -> str:
    """Compact per-turn block for get_conversation_context — empty string when
    nothing recent, so quiet periods cost zero prompt space."""
    try:
        recs = collect_cli_chats(hours=hours, limit=max_items)
    except Exception:
        return ""
    return "\n".join(_one_line(r) for r in recs)


def synthesis_block(hours: float = 24.0) -> str:
    """Fuller block for the twice-daily synthesis / planning prompt."""
    try:
        recs = collect_cli_chats(hours=hours, limit=12)
    except Exception as e:
        return f"  (unavailable: {e})"
    if not recs:
        return "  (no CLI sessions this period)"
    lines = []
    for r in recs:
        lines.append(_one_line(r))
        if r.get("last_user") and r["last_user"] != r["title"]:
            lines.append(f"      last ask: {r['last_user']}")
    return "\n".join(lines)


# ── Tools ─────────────────────────────────────────────────────────────────────

@tool(
    name="list_cli_chats",
    description=(
        "List his recent AI-CLI work sessions on this PC (Claude Code, Codex, Grok "
        "terminals) — when, which project, how many messages, what they were about. "
        "Use when he references 'that session', asks what he's been working on, or "
        "you need cross-conversation context. Read-only, instant."),
    parameters={
        "hours": {"type": "number", "description": "Look-back window in hours (default 24)"},
    },
)
async def list_cli_chats(hours: float = 24.0) -> str:
    recs = collect_cli_chats(hours=float(hours or 24.0), limit=20)
    if not recs:
        return f"No CLI sessions in the last {hours:g}h."
    lines = [f"{len(recs)} CLI session(s) in the last {hours:g}h (newest first):"]
    for r in recs:
        lines.append(_one_line(r))
        if r.get("last_user") and r["last_user"] != r["title"]:
            lines.append(f"      last ask: {r['last_user']}")
        lines.append(f"      id: {r['id'][:12]}")
    return "\n".join(lines)


@tool(
    name="read_cli_chat",
    description=(
        "Read the tail of one of his AI-CLI sessions (Claude Code / Codex / Grok) — "
        "the actual back-and-forth, newest last. Identify the session by id prefix, "
        "project name, or a word from its title (list_cli_chats shows all three). "
        "Read-only."),
    parameters={
        "session": {"type": "string",
                    "description": "Session id prefix, project name, or title fragment"},
        "chars": {"type": "number", "description": "Max characters returned (default 3000)"},
    },
    required=["session"],
)
async def read_cli_chat(session: str, chars: float = 3000) -> str:
    want = (session or "").strip().lower()
    if not want:
        return "Which session? Give an id prefix, project name, or title word."
    recs = collect_cli_chats(hours=96.0, limit=40)
    match = None
    for r in recs:
        if (r["id"].lower().startswith(want)
                or want in r["project"].lower()
                or want in r["title"].lower()):
            match = r
            break
    if not match:
        return f"No session matching '{session}' in the last 4 days. list_cli_chats shows what exists."
    parser = {"claude-code": _parse_cc, "codex": _parse_codex}.get(match["tool"])
    try:
        if parser:
            deep = parser(Path(match["path"]), deep=True)
        else:
            deep = _parse_grok(Path(match["path"]).parent, deep=True)
    except Exception as e:
        return f"Couldn't read that transcript: {e}"
    turns = (deep or {}).get("turns") or []
    if not turns:
        return f"Found {match['tool']} session in {match['project']} but its transcript has no readable turns."
    cap = int(chars or 3000)
    rendered: list[str] = []
    for role, text in turns:
        rendered.append(f"{role}: {_trim(text, 500)}")
    body = "\n".join(rendered)
    if len(body) > cap:
        body = "…" + body[-cap:]
    head = (f"{match['tool']} session in {match['project']} "
            f"(last active {_local_hhmm(match['last_active'])}, {match['msgs']} user msgs) — tail:")
    return head + "\n" + body

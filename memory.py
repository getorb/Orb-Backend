"""Long-term memory — durable facts the assistant has learned about its user.

Written from the behavioral contract its call sites define (server_win.py,
mind.py) and the on-disk data format of data/jarvis.db.

What it does:
  * remember()               — store one durable fact (the mind + seeding use this)
  * get_important_memories() — highest-importance facts, for briefings/prompts
  * get_recent_memories()    — newest facts, for "do we know anything yet" checks
  * build_memory_context()   — facts RELEVANT to the current user turn, as a
                               small bullet block injected into the system prompt
  * extract_memories()       — async: distill new durable facts out of one
                               user/assistant exchange on a caller-provided
                               local model (never the paid brain)

Storage is SQLite at data/jarvis.db: a `memories` table plus an
external-content FTS5 index (`memory_fts`) kept in sync on every insert.
The same file also holds legacy `tasks`/`notes` tables from an earlier era;
this module neither reads nor writes those.

Feature gating (JARVIS_MEMORY / ORB_MEMORY) lives at the call sites, not
here — the mind stores facts regardless of the conversational flag.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from pathlib import Path

log = logging.getLogger("orb")

_DATA_DIR = Path(__file__).parent / "data"
DB_PATH = _DATA_DIR / "jarvis.db"   # on-disk name kept for existing installs

# Kinds the schema documents; extraction clamps unknown model output to "fact".
MEMORY_TYPES = ("fact", "preference", "project", "person", "decision")

_SCHEMA = (
    "create table if not exists memories ("
    "  id integer primary key autoincrement,"
    "  type text not null, content text not null, source text default '',"
    "  importance integer default 5, created_at real not null,"
    "  last_accessed real, access_count integer default 0);"
    "create virtual table if not exists memory_fts using fts5("
    "  content, type, source, content='memories', content_rowid='id');"
)


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=5)
    con.row_factory = sqlite3.Row
    con.executescript(_SCHEMA)
    return con


def _rows_to_dicts(rows) -> list[dict]:
    return [dict(r) for r in rows]


# ── writing ──────────────────────────────────────────────────────────────────

def remember(content: str, mem_type: str = "fact", source: str = "",
             importance: int = 5) -> int:
    """Store one durable fact. Returns its row id.

    Exact-duplicate contents are not re-inserted; the existing row keeps the
    higher of the two importances (extraction runs every turn — without this
    the same fact would accumulate forever).
    """
    content = (content or "").strip()
    if not content:
        return 0
    if mem_type not in MEMORY_TYPES:
        mem_type = "fact"
    importance = max(1, min(int(importance or 5), 10))
    with _connect() as con:
        dup = con.execute(
            "SELECT id, importance FROM memories WHERE content = ?",
            (content,)).fetchone()
        if dup:
            if importance > (dup["importance"] or 0):
                con.execute("UPDATE memories SET importance = ? WHERE id = ?",
                            (importance, dup["id"]))
            return int(dup["id"])
        cur = con.execute(
            "INSERT INTO memories (type, content, source, importance, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (mem_type, content, source or "", importance, time.time()))
        rowid = cur.lastrowid
        # External-content FTS5 is not self-maintaining — mirror the insert.
        con.execute(
            "INSERT INTO memory_fts (rowid, content, type, source) "
            "VALUES (?, ?, ?, ?)",
            (rowid, content, mem_type, source or ""))
        return int(rowid)


# ── reading ──────────────────────────────────────────────────────────────────

def get_important_memories(limit: int = 10) -> list[dict]:  # noqa: caller contract
    """The facts most worth knowing, ranked by importance then recency."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM memories ORDER BY importance DESC, created_at DESC "
            "LIMIT ?", (max(1, int(limit)),)).fetchall()
        return _rows_to_dicts(rows)


def get_recent_memories(limit: int = 20) -> list[dict]:
    """Newest facts first (seeding checks this to see if memory is empty)."""
    with _connect() as con:
        rows = con.execute(
            "SELECT * FROM memories ORDER BY created_at DESC LIMIT ?",
            (max(1, int(limit)),)).fetchall()
        return _rows_to_dicts(rows)


_STOPWORDS = frozenset(
    "the a an and or but if then this that these those i you he she it we they "
    "me him her them my your his its our their is are was were be been being "
    "do does did have has had can could will would should what when where who "
    "why how not no yes to of in on at by for with from as about into over "
    "after before just so than too very there here all any some out up down".split())


def _query_terms(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z0-9]{3,}", (text or "").lower())
    seen: list[str] = []
    for w in words:
        if w not in _STOPWORDS and w not in seen:
            seen.append(w)
    return seen[:12]


def build_memory_context(user_text: str, limit: int = 6) -> str:
    """Facts relevant to what the user just said, as '- fact' lines.

    Empty string when nothing relevant is stored — the caller only injects
    the memory blurb when this returns content. Surfaced rows get their
    last_accessed/access_count bumped so future ranking can favor what
    actually proves useful.
    """
    terms = _query_terms(user_text)
    if not terms:
        return ""
    match = " OR ".join(f'"{t}"' for t in terms)
    try:
        with _connect() as con:
            rows = con.execute(
                "SELECT m.* FROM memory_fts f JOIN memories m ON m.id = f.rowid "
                "WHERE memory_fts MATCH ? "
                "ORDER BY bm25(memory_fts) ASC, m.importance DESC LIMIT ?",
                (match, max(1, int(limit)))).fetchall()
            if not rows:
                return ""
            now = time.time()
            con.executemany(
                "UPDATE memories SET last_accessed = ?, "
                "access_count = COALESCE(access_count, 0) + 1 WHERE id = ?",
                [(now, r["id"]) for r in rows])
            return "\n".join(f"- {r['content']}" for r in rows)
    except sqlite3.Error as e:
        log.debug(f"[memory] context query failed: {e}")
        return ""


# ── learning ─────────────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """From this exchange, list any DURABLE facts about the user worth remembering long-term: who they are, preferences, projects, people in their life, decisions they made. Only things that will still matter in a month — never small talk, moods, or one-off requests. Nothing about the assistant itself.

User: {user}
Assistant: {assistant}

Reply with ONLY a JSON array (no other text). Each item: {{"content": "<one self-contained sentence>", "type": "fact|preference|project|person|decision", "importance": <1-9>}}. Reply [] if nothing qualifies."""


def _parse_extraction(raw: str) -> list[dict]:
    raw = (raw or "").strip()
    if not raw:
        return []
    m = re.search(r"\[.*\]", raw, re.DOTALL)
    if not m:
        return []
    try:
        items = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    return [i for i in items if isinstance(i, dict) and i.get("content")]


def _is_known(content: str, existing: list[str]) -> bool:
    norm = re.sub(r"\W+", " ", content.lower()).strip()
    if not norm:
        return True
    for prior in existing:
        p = re.sub(r"\W+", " ", prior.lower()).strip()
        if norm == p or (len(norm) > 20 and (norm in p or p in norm)):
            return True
    return False


async def extract_memories(user_text: str, assistant_text: str, generate) -> list[str]:
    """Distill durable facts from one exchange via `generate` (an async
    one-shot completion fn, e.g. the local Ollama floor). Returns the list of
    newly stored fact sentences; [] when the turn taught us nothing."""
    user_text = (user_text or "").strip()
    if len(user_text) < 15:
        return []
    prompt = _EXTRACT_PROMPT.format(
        user=user_text[:1200], assistant=(assistant_text or "")[:1200])
    raw = await generate(prompt)
    items = _parse_extraction(raw)
    if not items:
        return []
    existing = [r["content"] for r in get_recent_memories(200)]
    stored: list[str] = []
    for item in items[:5]:
        content = str(item.get("content", "")).strip()
        if not (8 <= len(content) <= 300) or _is_known(content, existing):
            continue
        try:
            imp = max(1, min(int(item.get("importance") or 5), 9))
        except (TypeError, ValueError):
            imp = 5
        remember(content, str(item.get("type", "fact")), "conversation", imp)
        stored.append(content)
        existing.append(content)
    return stored

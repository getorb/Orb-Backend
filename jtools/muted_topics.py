"""Muted topics — a first-class suppression list the proactive layers check
BEFORE surfacing anything.

The problem this solves: the wake loop kept re-logging the same stale topics
every cycle even after the user made clear they don't land. A muted topic is
suppressed from ALL proactive output — no note re-deriving it, no Proactive-tab
item, no push — until the user raises it in conversation (at which point they
can `unmute_topic`, or just tell the assistant to).

This is durable (data/muted_topics.json) and enforced in CODE, not by asking a
model nicely: both proactive_engine._apply_plan and mind._make_dispatcher call
is_muted() and drop matching output. The model is ALSO told about the list so it
doesn't waste a slot generating something that would just be dropped.

Matching is keyword-based: each muted topic carries a set of lowercase match
terms; a candidate string is muted if it contains any term. Deliberately blunt —
over-suppression of a nagging topic is the safe direction here.
"""

import json
import time
from pathlib import Path

_PATH = Path(__file__).parent.parent / "data" / "muted_topics.json"
_PATH.parent.mkdir(parents=True, exist_ok=True)

# Seeded once, on first load if the file doesn't exist. Each entry:
#   {"label": human name, "terms": [lowercase substrings that match]}
_SEED = [
    {
        "label": "job applications / job hunt",
        "terms": ["apply to", "applying to", "apply for", "job application",
                  "job app", "job hunt", "job search", "cover letter",
                  "target role", "my resume", "resume draft"],
    },
    {
        "label": "disk usage",
        "terms": ["disk usage", "disk space", "disk is full", "storage full", "free space"],
    },
]


def _load() -> list[dict]:
    if not _PATH.exists():
        _save(_SEED)
        return [dict(t) for t in _SEED]
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(topics: list[dict]) -> None:
    try:
        _PATH.write_text(json.dumps(topics, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def list_muted() -> list[dict]:
    return _load()


def _terms() -> list[tuple[str, str]]:
    """Flatten to (term, label) pairs, lowercased."""
    out: list[tuple[str, str]] = []
    for t in _load():
        label = t.get("label", "")
        for term in t.get("terms", []):
            term = str(term).strip().lower()
            if term:
                out.append((term, label))
    return out


def block_reason(text: str) -> str:
    """Return the muted-topic label a candidate string matches, or "" if clear.
    Used by the enforcement points to suppress AND log WHY."""
    if not text:
        return ""
    low = str(text).lower()
    for term, label in _terms():
        if term in low:
            return label
    return ""


def is_muted(*texts: str) -> bool:
    """True if any of the given strings hits a muted topic. Callers pass the
    title + body (+ topic) of whatever they're about to surface."""
    return any(block_reason(t) for t in texts if t)


def add_topic(label: str, terms: list[str] | None = None) -> str:
    """Mute a topic. `terms` defaults to the label's own words (>=3 chars)."""
    label = (label or "").strip()
    if not label:
        return "No topic given."
    topics = _load()
    for t in topics:
        if t.get("label", "").strip().lower() == label.lower():
            return f"Already muted: {label}"
    if not terms:
        terms = [w.lower() for w in label.replace("/", " ").split() if len(w) >= 3]
    topics.append({"label": label, "terms": [str(x).strip().lower() for x in terms if str(x).strip()],
                   "muted_at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    _save(topics)
    return f"Muted: {label} (terms: {', '.join(terms)})"


def remove_topic(query: str) -> str:
    """Unmute — matched by substring against the label. The user does this when
    they want the topic back on the radar (or just raise it and the assistant calls this)."""
    q = (query or "").strip().lower()
    if not q:
        return "Which topic?"
    topics = _load()
    kept = [t for t in topics if q not in t.get("label", "").lower()]
    if len(kept) == len(topics):
        return f"No muted topic matches '{query}'."
    _save(kept)
    removed = [t.get("label", "") for t in topics if q in t.get("label", "").lower()]
    return f"Unmuted: {', '.join(removed)}"


def render_block() -> str:
    """A prompt block so the model doesn't waste a slot on something that would
    just be dropped in code anyway."""
    topics = _load()
    if not topics:
        return "  (none)"
    return "\n".join(f"  - {t.get('label', '')}" for t in topics)


# ── Voice/chat tools so the user can manage the list conversationally ─────────
try:
    from tool_registry import tool

    @tool(
        name="mute_topic",
        description=(
            "Stop proactively raising a topic — no notifications, notes, or Proactive-tab "
            "items about it until the user brings it up again. Use when the user says 'stop "
            "reminding me about X', 'quit bringing up X', 'I don't want to hear about X'. "
            "It's enforced in code across the whole proactive system."
        ),
        parameters={
            "topic": {"type": "string", "description": "The topic to mute (e.g. 'disk usage', 'job applications')."},
        },
        required=["topic"],
    )
    async def mute_topic(topic: str) -> str:
        return add_topic(topic)

    @tool(
        name="unmute_topic",
        description=(
            "Re-enable proactive surfacing of a previously muted topic. Use when the user "
            "raises a muted topic themselves or says 'you can bring up X again'."
        ),
        parameters={
            "topic": {"type": "string", "description": "A few words of the muted topic to unmute."},
        },
        required=["topic"],
    )
    async def unmute_topic(topic: str) -> str:
        return remove_topic(topic)

    @tool(
        name="list_muted_topics",
        description="List the topics currently muted from all proactive output.",
        parameters={},
        required=[],
    )
    async def list_muted_topics() -> str:
        topics = _load()
        if not topics:
            return "Nothing is muted."
        return "Muted topics: " + "; ".join(t.get("label", "") for t in topics) + "."
except Exception:
    pass

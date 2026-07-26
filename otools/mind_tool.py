"""Voice access to the mind — ask what it's thinking, or send it to look at
something. The conversational agent gets these; the mind itself does NOT
(they're absent from its READ_TOOLS surface — no recursive wakes)."""

import asyncio
import logging

from tool_registry import tool

log = logging.getLogger("orb.tools.mind")


@tool(
    name="mind_status",
    description=(
        "What your between-conversations mind is currently thinking: its "
        "situation model, latest note to itself, and next wake. Use when the "
        "user asks 'what are you thinking', 'what are you working on in the "
        "background', 'what's your read on things', or 'what's the situation'."
    ),
    parameters={},
    required=[],
)
async def mind_status() -> str:
    import mind
    s = mind.status()
    if not s.get("enabled"):
        return "The mind is switched off right now (ORB_MIND=0)."
    if s.get("paused"):
        return "The mind is paused."
    bits = []
    note = s.get("note_to_next_wake", "")
    if note:
        bits.append(f"My working note: {note}")
    sit = " ".join((s.get("situation") or "").split())
    if sit:
        bits.append(f"Situation model, in brief: {sit[:450]}")
    nw = s.get("next_wake_at") or ""
    if nw:
        bits.append(f"Next wake {nw[11:16]} — {s.get('next_wake_reason', '')[:100]}.")
    return " ".join(bits) or "Quiet so far — nothing on the board yet."


@tool(
    name="wake_mind",
    description=(
        "Send the background mind to look into something specific, starting "
        "now. Use when the user says 'look into X', 'keep an eye on Y', 'dig "
        "into this while I'm away', or wants background attention on a topic. "
        "It investigates within its envelope and reports through its usual "
        "channels (staged moments, paced notifications, proposals)."
    ),
    parameters={
        "directive": {"type": "string",
                      "description": "What to look into or watch, in plain words."},
    },
    required=["directive"],
)
async def wake_mind(directive: str) -> str:
    import mind
    d = (directive or "").strip()
    if not d:
        return "What should I look into?"
    asyncio.create_task(mind.wake(reason=f"his directive: {d[:160]}"))
    return ("On it — I'll work it between conversations and reach you "
            "through the usual channels.")

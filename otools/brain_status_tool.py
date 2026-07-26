import time
from tool_registry import tool


@tool(
    name="brain_status",
    description=(
        "Show which AI brain you're running on, how many times each brain has been used "
        "this session, and whether any hit a rate limit recently. Use when the user asks "
        "'what model are you on', 'how much have you used Claude / Grok / Codex', "
        "'which brains are available', or wants to know usage before switching."
    ),
    parameters={},
    required=[],
)
async def brain_status() -> str:
    import brain as _brain

    current = _brain.effective_agent_brain()
    overrode = _brain.user_overrode()
    stats = _brain.brain_stats()
    now = time.time()

    lines = [
        f"Active brain: {_brain.label(current)}"
        + (" (your choice)" if overrode else " (default)"),
        "",
        "Session usage:",
    ]

    for key, info in stats.items():
        calls = info["calls"]
        last_limit = info["last_limit"]
        available_flag = ""
        if key in _brain.MODELS and not _brain.claude_available():
            available_flag = " [unavailable]"
        elif key in _brain.GROK_MODELS and not _brain.grok_available():
            available_flag = " [unavailable]"
        elif key in _brain.CODEX_MODELS and not _brain.codex_available():
            available_flag = " [unavailable]"

        limit_note = ""
        if last_limit:
            mins_ago = (now - last_limit) / 60
            limit_note = f", hit limit {mins_ago:.0f}m ago"

        if calls > 0 or last_limit:
            lines.append(f"  {info['label']}: {calls} call{'s' if calls != 1 else ''}{limit_note}{available_flag}")

    used = [k for k, v in stats.items() if v["calls"] > 0]
    if not used:
        lines.append("  None used yet this session.")

    # Availability summary for brains with 0 calls
    unavail = []
    if not _brain.claude_available():
        unavail.append("Claude (not logged in)")
    if not _brain.grok_available():
        unavail.append("Grok (CLI not found)")
    if not _brain.codex_available():
        unavail.append("Codex (CLI not found)")
    if unavail:
        lines += ["", "Unavailable: " + ", ".join(unavail)]

    return "\n".join(lines)

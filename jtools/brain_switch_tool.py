from tool_registry import tool
import brain as _brain


@tool(
    name="switch_brain",
    description=(
        "Switch the AI backend for this and future turns. Use when the user asks to switch "
        "models, or when a task clearly needs a different capability. "
        "Available models: "
        "haiku (Claude Haiku, default — fast, conversational), "
        "sonnet (Claude Sonnet — stronger reasoning), "
        "opus (Claude Opus — most capable Claude), "
        "fable (Claude Fable — newest, above Opus; usage is LIMITED, only switch when the user explicitly asks), "
        "grok (Grok fast — good general reasoning), "
        "grok-build (Grok Build — strong at coding tasks), "
        "codex (OpenAI Codex mini — cheap OpenAI, saves Claude usage), "
        "codex-mini (same as codex, explicit mini tier — cheapest OpenAI), "
        "codex-full (OpenAI Codex full model — more powerful, higher cost), "
        "local (offline Qwen — instant, no internet needed). "
        "When user says 'cheaper openai', 'codex mini', or wants to save openai credits: use codex-mini. "
        "When user wants more power from OpenAI: use codex-full."
    ),
    parameters={
        "model": {"type": "string", "description": "One of: haiku, sonnet, opus, fable, grok, grok-build, codex, codex-mini, codex-full, local"},
        "reason": {"type": "string", "description": "Brief reason for switching (optional)."},
    },
    required=["model"],
)
async def switch_brain(model: str, reason: str = "") -> str:
    valid = {"haiku", "sonnet", "opus", "fable", "grok", "grok-build", "codex", "codex-mini", "codex-full", "local"}
    if model not in valid:
        return f"Unknown model '{model}'. Valid options: {', '.join(sorted(valid))}."
    # Guard (2026-07-01 — "Orb used Opus on its own, that's a problem"): this
    # tool is for USER-requested switches only. If the current user turn never
    # mentioned switching or any model, the switch is JARVIS's own idea — route
    # it through the propose flow (human yes/no) instead of switching silently.
    try:
        from server_win import _current_client
        _client = _current_client.get()
        _last = ((_client or {}).get("state") or {}).get("last_user_text", "").lower()
    except Exception:
        _last = ""
    if _last:
        _words = ("switch", "model", "brain", "haiku", "sonnet", "opus", "fable",
                  "grok", "codex", "local", "claude", "openai", "qwen")
        if not any(w in _last for w in _words):
            return await propose_brain_switch(
                model, reason or "I think this turn would go better on a different model")
    _brain.set_brain(model)
    label = _brain.label(model)

    # Honest heads-up if the backend isn't actually usable right now — the switch
    # still happens (never silently refuse), but the user shouldn't have to wait
    # out a timeout to discover it's not really there.
    available, why = True, ""
    if model in _brain.MODELS:
        available, why = _brain.claude_available(), "the claude CLI isn't logged in"
    elif model in _brain.GROK_MODELS:
        available, why = _brain.grok_available(), "grok isn't logged in right now"
    elif model in _brain.CODEX_MODELS:
        available, why = _brain.codex_available(), "codex is unavailable right now (credits or login)"

    if not available:
        return f"Switched to {label}, but heads up — {why}, so it may not actually respond."
    return f"Switched to {label}."


@tool(
    name="propose_brain_switch",
    description=(
        "Suggest switching to a different brain WITHOUT actually switching yet — use this "
        "instead of switch_brain when the idea to switch is YOURS (you noticed the task would "
        "genuinely go better on a different model), not something the user asked for. Say the "
        "suggestion in your own voice; the switch only happens if they say yes. If the USER "
        "explicitly asks to switch models, use switch_brain directly instead — this tool is "
        "only for JARVIS-initiated suggestions."
    ),
    parameters={
        "model": {"type": "string", "description": "One of: haiku, sonnet, opus, fable, grok, grok-build, codex, codex-mini, codex-full, local"},
        "reason": {"type": "string", "description": "Why this would genuinely help right now."},
    },
    required=["model", "reason"],
)
async def propose_brain_switch(model: str, reason: str) -> str:
    valid = {"haiku", "sonnet", "opus", "fable", "grok", "grok-build", "codex", "codex-mini", "codex-full", "local"}
    if model not in valid:
        return f"Unknown model '{model}'."
    try:
        from server_win import _current_client
        client = _current_client.get()
        if client and client.get("state") is not None:
            client["state"]["pending_brain_switch"] = (model, reason)
    except Exception:
        pass  # no active connection state (e.g. called outside a live turn) — still voice it
    label = _brain.label(model)
    return (f"Proposed switching to {label} ({reason}) — say the suggestion naturally and ask "
            f"if they want to. Do NOT say it's already switched; it isn't yet.")

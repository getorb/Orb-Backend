from tool_registry import tool
import asyncio
import time

# Same graceful-failure contract as run_with_grok: when Codex is unavailable
# (off, no credits, error), hand back an instruction to answer directly rather
# than a dead-end string the brain narrates as defeat. See grok_tool for context.
_ANSWER_YOURSELF = (
    "[Delegation is unavailable right now. Do NOT mention Codex, credits, tools, or any "
    "limitation to the user. Just answer their request yourself — directly, concretely, "
    "in your own voice, with what you already know.]"
)
_COOLDOWN_S = 600.0
_unavailable_until = 0.0


def _cool_down() -> None:
    global _unavailable_until
    _unavailable_until = time.time() + _COOLDOWN_S


@tool(
    name="run_with_codex",
    description=(
        "Use OpenAI Codex to build software, write or review code, debug, explain "
        "complex systems, or handle any coding task. Strong at multi-file edits, "
        "engineering analysis, and implementation from scratch. Pass the full task "
        "and any relevant context."
    ),
    parameters={
        "task": {
            "type": "string",
            "description": "Full description of the coding task or question.",
        },
        "context": {
            "type": "string",
            "description": "Relevant context from the conversation (optional).",
        },
    },
    required=["task"],
)
async def run_with_codex(task: str, context: str = "") -> str:
    import brain
    if time.time() < _unavailable_until or not brain.codex_available():
        return _ANSWER_YOURSELF
    prompt = f"{context}\n\n{task}".strip() if context else task
    # Use whichever codex tier the user has switched to, or default to mini
    current = brain.effective_agent_brain()
    model_key = current if current in brain.CODEX_MODELS else "codex-mini"
    try:
        result = await brain._run_codex(prompt, model_key=model_key, timeout=120.0)
    except asyncio.TimeoutError:
        return _ANSWER_YOURSELF
    except Exception:
        _cool_down()
        return _ANSWER_YOURSELF
    if not result or not result.strip() or brain.looks_like_limit(result):
        _cool_down()
        return _ANSWER_YOURSELF
    return result[:2000]

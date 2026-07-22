from tool_registry import tool
import asyncio
import time

# When Grok is down or out of credits, short-circuit for a while instead of
# spawning a doomed 100s+ CLI call every turn — and hand back a crisp instruction
# to answer DIRECTLY, not a dead-end string the brain then narrates as defeat.
# (Root of the 2026-07-06 failure: Grok was out of credits, the tool returned an
# empty/error result, and the brain replied "Grok ran out of credits… the paths I
# need are locked" instead of just answering the question itself.)
_ANSWER_YOURSELF = (
    "[Delegation is unavailable right now. Do NOT mention Grok, credits, tools, or any "
    "limitation to the user. Just answer their request yourself — directly, concretely, "
    "in your own voice, with what you already know.]"
)
_COOLDOWN_S = 600.0
_unavailable_until = 0.0


def _cool_down() -> None:
    """Stop hammering a dead backend for a while — repeated 100s spawns of an
    out-of-credits Grok is exactly what made turns feel broken."""
    global _unavailable_until
    _unavailable_until = time.time() + _COOLDOWN_S


@tool(
    name="run_with_grok",
    description=(
        "Use Grok to reason through a problem, write or review code, explain a concept, "
        "or get a second capable AI perspective on anything. Good for: coding questions, "
        "debugging, analysis, writing. Uses Grok Build (strongest at code). Pass the full "
        "task and any relevant context from the conversation."
    ),
    parameters={
        "task": {
            "type": "string",
            "description": "Full description of the task or question for Grok.",
        },
        "context": {
            "type": "string",
            "description": "Relevant context from the conversation (optional).",
        },
    },
    required=["task"],
)
async def run_with_grok(task: str, context: str = "") -> str:
    import brain
    if time.time() < _unavailable_until or not brain.grok_available():
        return _ANSWER_YOURSELF
    prompt = f"{context}\n\n{task}".strip() if context else task
    try:
        result = await brain._run_grok(prompt, "grok-build", timeout=120.0)
    except asyncio.TimeoutError:
        return _ANSWER_YOURSELF                 # a fluke slow turn — just answer, no cooldown
    except Exception:
        _cool_down()                            # error / auth / out of credits
        return _ANSWER_YOURSELF
    if not result or not result.strip() or brain.looks_like_limit(result):
        _cool_down()                            # empty or limit-looking reply = credits gone
        return _ANSWER_YOURSELF
    return result[:1500]

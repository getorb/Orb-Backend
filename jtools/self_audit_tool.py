"""Self-audit tool — lets Orb accurately report its own capabilities and state.

Solves the problem of Haiku confabulating about what tools it has or whether
they work. Instead of guessing, it calls this and gets ground truth.
"""
import asyncio
from tool_registry import tool


@tool(
    name="self_audit",
    description=(
        "Check your own current capabilities, tools, and backend status. Call this "
        "when asked 'what can you do', 'what tools do you have', 'are you able to X', "
        "'what happened last turn', 'why couldn't you do that', or any question about "
        "your own abilities, limitations, or what's currently working."
    ),
    parameters={
        "check": {
            "type": "string",
            "description": "What to check: 'tools' (list all), 'backend' (brain status), 'all' (everything). Default: all.",
        }
    },
    required=[],
)
async def self_audit(check: str = "all") -> str:
    import tool_registry
    import brain as _brain

    lines: list[str] = []

    # --- Tools ---
    if check in ("tools", "all"):
        tools = tool_registry.list_tools()
        lines.append(f"Registered tools ({len(tools)}): {', '.join(tools)}")

        # Spot-check a few tools that have had issues
        issues: list[str] = []
        try:
            import jtools.lookup_tool  # noqa
        except Exception as e:
            issues.append(f"lookup_fact import error: {e}")
        try:
            import jtools.screenshot_tool as _ss
            if _ss._dispatch_fn is None:
                issues.append("take_screenshot: dispatch not wired (backend not fully started?)")
        except Exception as e:
            issues.append(f"take_screenshot error: {e}")
        try:
            import jtools.notify_tool as _nt
            if _nt._deliver_fn is None:
                issues.append("send_notification: deliver fn not wired")
        except Exception as e:
            issues.append(f"send_notification error: {e}")

        if issues:
            lines.append("Tool issues detected: " + "; ".join(issues))
        else:
            lines.append("All spot-checked tools appear healthy.")

    # --- Backend ---
    # Full multi-brain visibility (07-02 introspection finding: this section
    # only saw Claude + Ollama — "Grok and Codex work but live outside its
    # view. The pieces work but don't all see each other.")
    if check in ("backend", "all"):
        current = _brain.effective_agent_brain()
        lines.append(f"Active brain: {current}")
        available = []
        try:
            if _brain.claude_available():
                claude_models = "/".join(sorted(_brain.MODELS))
                import os as _os
                mcp_on = _os.getenv("ORB_MCP_BRAIN", "1") != "0"
                available.append(f"{claude_models} (claude -p, "
                                 f"{'MCP brain w/ resumed session' if mcp_on else 'classic loop'})")
        except Exception:
            pass
        try:
            if _brain.grok_available():
                available.append("grok/grok-build (grok CLI, native MCP)")
            else:
                available.append("grok (CLI missing or not logged in)")
        except Exception:
            pass
        try:
            if _brain.codex_available():
                _unavail = getattr(_brain, "TIER_UNAVAILABLE", {}) or {}
                _note = f"; {', '.join(f'{k}: {v}' for k, v in _unavail.items())}" if _unavail else ""
                available.append(f"codex/codex-mini (codex CLI, native MCP{_note})")
            else:
                available.append("codex (CLI missing)")
        except Exception:
            pass
        try:
            import httpx
            async with httpx.AsyncClient(timeout=2.0) as _c:
                await _c.get("http://localhost:11434/api/tags")
            # API answering ≠ can infer (July 2026: llama-server gutted while
            # the port stayed up) — say so instead of claiming health.
            available.append("local ollama (API up — note: API up does not "
                             "guarantee inference; the floor has been dead with "
                             "the port open before)")
        except Exception:
            available.append("local ollama (unreachable)")
        lines.append(f"Available backends: {', '.join(available) or 'none detected'}")
        try:
            import mcp_http as _mh
            if _mh._exposed_fn:
                lines.append(f"Resident MCP tool surface (/mcp): {len(_mh._exposed_fn())} tools served in-process.")
        except Exception:
            pass

    # --- APNs ---
    if check in ("notifications", "all"):
        try:
            import apns
            tokens = apns.known_tokens()
            if apns.configured() and tokens:
                lines.append(f"Push notifications: configured, {len(tokens)} device(s) registered.")
            elif apns.configured():
                lines.append("Push notifications: credentials set, but no device token yet (phone needs to connect).")
            else:
                lines.append("Push notifications: APNs not configured (missing .env keys).")
        except Exception as e:
            lines.append(f"Push notifications: error checking ({e})")

    return "\n".join(lines)

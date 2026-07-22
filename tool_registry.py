"""
JARVIS Tool Registry — function-calling tools that Qwen can decide to invoke.

This replaces brittle keyword routing ("if 'weather' in text...") with the
model itself reasoning about which tool to call. Tools register themselves
via the @tool decorator and become available to Ollama's function-calling API.

Usage:
    from tool_registry import tool, dispatch, tools_schema

    @tool(
        name="get_weather",
        description="Get the current weather or forecast for a location",
        parameters={
            "location": {"type": "string", "description": "City name"},
            "tomorrow": {"type": "boolean", "description": "Forecast for tomorrow"}
        },
        required=["location"],
    )
    async def get_weather(location: str, tomorrow: bool = False) -> str:
        ...
"""

import inspect
import logging
from typing import Any, Awaitable, Callable

log = logging.getLogger("jarvis.tools")

_REGISTRY: dict[str, dict] = {}


def tool(name: str, description: str, parameters: dict[str, dict],
         required: list[str] | None = None) -> Callable:
    """Decorator to register a function as a JARVIS tool.

    parameters maps argument name -> JSONSchema-ish dict ({type, description, enum?})
    """
    def wrap(fn: Callable[..., Awaitable[str]]) -> Callable:
        if not inspect.iscoroutinefunction(fn):
            raise TypeError(f"Tool {name} must be an async function")
        _REGISTRY[name] = {
            "callable": fn,
            "description": description,
            "parameters": parameters,
            "required": required or [],
        }
        log.debug(f"registered tool: {name}")
        return fn
    return wrap


def tools_schema() -> list[dict]:
    """Return all tools in Ollama function-calling format."""
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": meta["description"],
                "parameters": {
                    "type": "object",
                    "properties": meta["parameters"],
                    "required": meta["required"],
                },
            },
        }
        for name, meta in _REGISTRY.items()
    ]


def list_tools() -> list[str]:
    return sorted(_REGISTRY.keys())


async def dispatch(name: str, arguments: dict[str, Any]) -> str:
    """Execute a tool call. Returns the tool's string output."""
    meta = _REGISTRY.get(name)
    if not meta:
        return f"[tool error] unknown tool: {name}"
    fn = meta["callable"]
    try:
        clean_args = _coerce_args(arguments, meta["parameters"])
        log.info(f"[tool] {name}({clean_args})")
        result = await fn(**clean_args)
        return str(result) if result is not None else ""
    except TypeError as e:
        log.error(f"[tool] {name} bad args: {e}")
        return f"[tool error] {name}: invalid arguments ({e})"
    except Exception as e:
        log.error(f"[tool] {name} raised: {e}")
        return f"[tool error] {name}: {e}"


def _coerce_args(args: dict, schema: dict) -> dict:
    """Drop unknown args; coerce simple JSON-string args (Qwen sometimes wraps in quotes).

    Models occasionally return stringly-typed args ('true', '5'); convert to bools/ints
    where the schema says so, so the tool doesn't crash on a type error.
    """
    out = {}
    for k, v in (args or {}).items():
        if k not in schema:
            continue
        expected = schema[k].get("type")
        if expected == "boolean" and isinstance(v, str):
            v = v.lower() in ("true", "1", "yes")
        elif expected == "integer" and isinstance(v, str):
            try:
                v = int(v)
            except ValueError:
                pass
        elif expected == "number" and isinstance(v, str):
            try:
                v = float(v)
            except ValueError:
                pass
        out[k] = v
    return out


def import_all_tools():
    """Side-effect import to populate the registry. Called once at startup."""
    # Each import registers its tools via the @tool decorator
    from jtools import (  # noqa: F401
        weather_tool, lookup_tool, scraper_tool, news_tool,
        files_tool, system_tool, notes_tool, calculator_tool,
        images_tool, finance_tool, claude_code_tool, brain_switch_tool,
        screenshot_tool, notify_tool, sessions_tool, self_audit_tool, grok_tool, codex_tool,
        brain_status_tool, process_control_tool, pong_tool,
        vision_tool, prefs_tool, pc_control_tool, terminal_tool, watch_tool,
        missions_tool, mail_tool, mind_tool, scheduler_tool, introspect_tool,
        cli_chats_tool, frustration_signal, muted_topics, session_watch,
    )
    log.info(f"[tools] loaded {len(_REGISTRY)} tools: {', '.join(list_tools())}")

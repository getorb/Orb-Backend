"""Orb tools exposed as a real MCP server — lets Codex/Grok (and eventually
Claude, per BRAIN_ARCHITECTURE.md's original spec) call Orb's tools with
their OWN native, well-trained tool-calling instead of the custom
JSON-in-text ReAct protocol agent.py uses for the local/Haiku loop.

Why this exists (found 2026-07-01): asked Codex to type into a terminal via
the normal agent loop; it replied {"say":"Done, sir."} without calling
anything — confidently fabricated, not confused. Reproduced directly against
`codex exec`. Root cause: Codex/Grok weren't trained on this repo's made-up
tool-call convention, so they don't reliably treat the TOOLS: catalog as a
real action interface. Both CLIs (`codex mcp add`, `grok mcp add`) have full
native MCP client support, so the fix is giving them the real thing instead
of asking harder in English.

Thin wrapper only — reuses tool_registry.dispatch() 1:1, same execution path
the local agent loop already uses. No tool logic duplicated here.

Excluded from the exposed set: tools that depend on state server_win.py
injects into ITS OWN process at startup (a screenshot dispatch callback, a
notification delivery callback, the current WS connection's state for
pending-offer tracking). This MCP server runs as a SEPARATE process spawned
by the codex/grok CLI, so that state doesn't exist here — calling those tools
this way would silently do the wrong thing rather than fail loudly, so they're
left out rather than exposed broken. Everything else (terminal control,
search, weather, sessions, files, PC control) is plain/self-contained and
works identically from any process.

Phone connector tools (calendar/reminders/contacts/notifications), added
2026-07-01: these live only on the user's PHONE, relayed live over the WS
connection that's talking to it right now — there's no static version of
them to register here the way the rest of this file works. Haiku already
gets these via agent.run_turn(extra_tools=..., dispatch_override=...); this
was a real capability gap for Codex/Grok until now. server_win.py fixes it
per-turn: right before spawning this process, it registers a short-lived
relay token + the phone's current tool schemas and writes them to
data/mcp_relay_active.json. THIS process reads that file at startup to
advertise those tools too and route calls for them back over a localhost-
only HTTP POST instead of tool_registry.dispatch. Absent/missing file (no
phone connected, or nothing it's authorized) — total no-op, identical to
before this feature existed.

Deliberately a FILE, not environment variables — tried env vars first,
confirmed live it doesn't work: `codex mcp get orb` shows `env: -`, i.e.
codex does NOT forward a live `codex exec` call's environment down to the
MCP server subprocess it spawns (env forwarding is a fixed, per-server
`codex mcp add --env` registration setting, made once at `codex mcp add`
time, not something a single call can inject). Reading a file at startup
sidesteps that path entirely.

Setup (one-time per CLI):
    codex mcp add orb -- <venv>\\Scripts\\python.exe orb_mcp.py
    grok mcp add orb -- <venv>\\Scripts\\python.exe orb_mcp.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import httpx
import mcp.server.stdio
import mcp.types as types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions

import agent as agent_module
import tool_registry

tool_registry.import_all_tools()

# Depend on state that only exists inside the main server_win.py process —
# see module docstring. Calling these here would silently misbehave, not
# fail loudly, so they're excluded rather than exposed broken.
_EXCLUDED = {"send_notification", "take_screenshot", "propose_brain_switch", "switch_brain"}

_EXPOSED = [n for n in agent_module.AGENT_TOOLS
           if n in tool_registry._REGISTRY and n not in _EXCLUDED]

# Per-turn phone-tool relay — see module docstring for why this is a file,
# not env vars. Empty/missing in the common case (no phone tools to relay
# this turn) — _RELAY_TOKEN stays "" and everything below is a no-op.
_RELAY_FILE = Path(__file__).parent / "data" / "mcp_relay_active.json"
_RELAY_TOKEN = ""
_RELAY_BASE = ""
_PHONE_TOOLS: list[dict] = []
try:
    _relay_data = json.loads(_RELAY_FILE.read_text(encoding="utf-8"))
    _RELAY_TOKEN = _relay_data.get("token", "")
    _RELAY_BASE = _relay_data.get("base", "")
    _PHONE_TOOLS = _relay_data.get("tools") or []
except Exception:
    pass
_PHONE_TOOL_NAMES = {t["function"]["name"] for t in _PHONE_TOOLS if t.get("function", {}).get("name")}

server = Server("orb")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    out = []
    for name in _EXPOSED:
        meta = tool_registry._REGISTRY[name]
        out.append(types.Tool(
            name=name,
            description=meta["description"],
            inputSchema={
                "type": "object",
                "properties": meta["parameters"],
                "required": meta["required"],
            },
        ))
    # Phone connector tools relayed for this turn only — same OpenAI
    # function-calling shape the phone always sends; `parameters` is already
    # a JSON Schema object, usable as inputSchema as-is.
    for t in _PHONE_TOOLS:
        fn = t.get("function") or {}
        if not fn.get("name"):
            continue
        out.append(types.Tool(
            name=fn["name"],
            description=fn.get("description", ""),
            inputSchema=fn.get("parameters") or {"type": "object", "properties": {}},
        ))
    return out


def _audit(kind: str, text: str, **extra) -> None:
    """Every MCP tool call lands in the shared activity log (plain file append —
    works from this separate process). Before 2026-07-02 these calls were
    INVISIBLE — 'what did Grok just do?' was unanswerable, which is untenable
    for a surface that includes PC control."""
    try:
        from otools.activity_log import log_activity
        log_activity("mcp", kind, text, **extra)
    except Exception:
        pass


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    try:
        import json as _json
        _audit("tool_start", f"{name}({_json.dumps(arguments or {}, ensure_ascii=False)[:180]})", tool=name)
    except Exception:
        pass
    if name in _PHONE_TOOL_NAMES:
        if not (_RELAY_TOKEN and _RELAY_BASE):
            _audit("tool_result", "no relay session", tool=name)
            return [types.TextContent(type="text", text=f"[tool error] '{name}' isn't reachable this turn (no relay session).")]
        try:
            async with httpx.AsyncClient(timeout=25.0) as client:
                resp = await client.post(
                    f"{_RELAY_BASE}/api/internal/mcp_relay/{_RELAY_TOKEN}",
                    json={"name": name, "args": arguments or {}},
                )
                data = resp.json()
        except Exception as e:
            _audit("tool_result", f"relay call failed: {e}", tool=name)
            return [types.TextContent(type="text", text=f"[tool error] relay call failed: {e}")]
        if "error" in data:
            _audit("tool_result", f"error: {data['error']}", tool=name)
            return [types.TextContent(type="text", text=f"[tool error] {data['error']}")]
        _audit("tool_result", str(data.get("result", ""))[:300], tool=name)
        return [types.TextContent(type="text", text=str(data.get("result", "")))]
    if name not in _EXPOSED:
        _audit("tool_result", "not available via MCP", tool=name)
        return [types.TextContent(type="text", text=f"[tool error] '{name}' is not available via MCP.")]
    result = await tool_registry.dispatch(name, arguments or {})
    _audit("tool_result", str(result)[:300], tool=name)
    return [types.TextContent(type="text", text=str(result))]


async def main() -> None:
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream,
            InitializationOptions(
                server_name="orb",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


if __name__ == "__main__":
    asyncio.run(main())

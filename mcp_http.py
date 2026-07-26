"""Orb's tools served over MCP *streamable HTTP* from INSIDE the live backend.

Why this exists (2026-07-02, the MCP-rebuild night): the stdio `orb_mcp.py`
server is spawned cold by each CLI turn. Measured live tonight: claude's first
turn ran to completion while the spawned server was still `"status": "pending"`
in the init event — the model genuinely had zero tools and confabulated about
the ones it wished it had. A resident HTTP MCP server flips every constraint
at once:

  • zero per-turn boot — the server IS the backend process, already warm;
  • tools execute in-process with real server state, so the stdio server's
    whole _EXCLUDED set (send_notification, take_screenshot,
    propose_brain_switch, switch_brain) simply works here — no exclusions;
  • phone-connector tools relay through the LIVE WS connection as a plain
    awaited call — no relay-token file, no localhost HTTP hop.

Transport: `StreamableHTTPSessionManager` in stateless JSON mode, mounted at
/mcp on the existing FastAPI app (:8340) and gated to LOOPBACK ONLY — :8340
is reachable over the tailnet and this surface executes tools (mouse, files,
terminals), so the only intended client is a CLI on this machine.

server_win.py owns all wiring: it injects dispatch + the exposure list at
startup (init()), and brackets each conversational turn with
set_active_turn()/clear_active_turn() so tool calls land with the SAME
per-connection context (_current_client) a normal agent-loop call would have.
Single active turn at a time — same accepted single-user simplification as
the codex relay file; turns are already serialized upstream.

The stdio `orb_mcp.py` stays as-is for Codex/Grok (registered with their
CLIs once via `mcp add`); this module is what the claude-family brain uses.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

log = logging.getLogger("orb")

# Injected by server_win.py at startup (init()).
_dispatch = None            # tool_registry.dispatch
_exposed_fn = None          # () -> list[str] of registry tool names to expose
_registry = None            # tool_registry._REGISTRY (name -> {description, parameters, required})

# The conversational turn currently in flight, if any — set/cleared by
# _run_mcp_claude_turn. Carries the phone's per-connection tools + dispatch
# and an enter() that seats this task in the connection's _current_client
# context before dispatching, so state-dependent tools behave identically to
# the classic agent loop.
_ACTIVE: dict | None = None


def init(dispatch, exposed, registry) -> None:
    global _dispatch, _exposed_fn, _registry
    _dispatch = dispatch
    _exposed_fn = exposed
    _registry = registry
    # The mcp lib narrates every stateless request at INFO ("Processing request
    # of type CallToolRequest", "Terminating session: None") — one connection
    # per claude turn makes that pure noise in backend_err.log.
    logging.getLogger("mcp").setLevel(logging.WARNING)


def set_active_turn(ctx: dict | None) -> None:
    global _ACTIVE
    _ACTIVE = ctx


def clear_active_turn() -> None:
    set_active_turn(None)


server = Server("orb")


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    out = []
    for name in (_exposed_fn() if _exposed_fn else []):
        meta = _registry[name]
        out.append(types.Tool(
            name=name,
            description=meta["description"],
            inputSchema={
                "type": "object",
                "properties": meta["parameters"],
                "required": meta["required"],
            },
        ))
    # Phone connector tools for the turn in flight — same OpenAI function-
    # calling shape the phone always sends; `parameters` is already a JSON
    # Schema object, usable as inputSchema as-is (same as orb_mcp.py).
    for t in (_ACTIVE or {}).get("extra_tools") or []:
        fn = t.get("function") or {}
        if not fn.get("name"):
            continue
        out.append(types.Tool(
            name=fn["name"],
            description=fn.get("description", ""),
            inputSchema=fn.get("parameters") or {"type": "object", "properties": {}},
        ))
    return out


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}
    active = _ACTIVE
    phone_names = {t["function"]["name"]
                   for t in (active or {}).get("extra_tools") or []
                   if t.get("function", {}).get("name")}
    try:
        if name in phone_names and active and active.get("dispatch"):
            result = await active["dispatch"](name, args)
        elif _exposed_fn and name in _exposed_fn():
            if active and active.get("enter"):
                # Seat this HTTP task in the live connection's context so
                # tools that read _current_client (cards, pending offers,
                # screenshot dispatch) behave exactly as in the agent loop.
                active["enter"]()
            result = await _dispatch(name, args)
        else:
            result = f"[tool error] '{name}' is not available."
    except Exception as e:
        result = f"[tool error] {e}"
    if not active:
        # An outside-of-turn client (not the orb conversation) called a tool —
        # audit it to the shared activity log like the stdio server does, so
        # nothing on this surface is ever invisible. Orb-turn calls skip this:
        # the turn's own stream parser already logs them as session "orb".
        try:
            from jtools.activity_log import log_activity
            log_activity("mcp", "tool_result", str(result)[:300], tool=name)
        except Exception:
            pass
    return [types.TextContent(type="text", text=str(result))]


_manager: StreamableHTTPSessionManager | None = None
_heal_lock: asyncio.Lock | None = None
_heal_task: asyncio.Task | None = None
_heal_ready: asyncio.Event | None = None


async def _self_heal_run() -> None:
    """Hold the manager's run() open from a background task — the recovery
    path when the lifespan never entered running() (seen live 2026-07-03
    17:45 boot: 'Task group is not initialized' on every tool call for the
    whole boot; the voice brain lost run_with_grok/run_claude_code/terminals
    and misreported it as 'the backend is restarting')."""
    try:
        async with _manager.run():
            _heal_ready.set()
            await asyncio.Event().wait()   # hold until process exit
    except Exception as e:
        log.error(f"[mcp-http] self-heal run() died: {e}")


def build_asgi_app():
    """Build the ASGI callable server_win mounts at /mcp. Loopback-only."""
    global _manager, _heal_lock, _heal_ready
    _manager = StreamableHTTPSessionManager(app=server, json_response=True,
                                            stateless=True)
    _heal_lock = asyncio.Lock()
    _heal_ready = asyncio.Event()

    async def _asgi(scope, receive, send):
        if scope["type"] == "http":
            client = scope.get("client")
            host = client[0] if client else ""
            if host not in ("127.0.0.1", "::1", "localhost"):
                await send({"type": "http.response.start", "status": 403,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"loopback only"})
                return
        try:
            await _manager.handle_request(scope, receive, send)
            return
        except RuntimeError as e:
            # Task group not initialized: either a request racing an
            # intentional restart drain (fine, 503 below) or the lifecycle
            # never started this boot (the 07-03 17:45 outage). Try to
            # SELF-HEAL once — start run() in a background task and retry —
            # so a bad boot degrades to a ~2s hiccup instead of a dead boot.
            log.warning(f"[mcp-http] request outside manager lifecycle: {e}")
        global _heal_task
        async with _heal_lock:
            if _heal_task is None or _heal_task.done():
                log.error("[mcp-http] manager lifecycle not running — self-healing")
                _heal_ready.clear()
                _heal_task = asyncio.create_task(_self_heal_run())
        try:
            await asyncio.wait_for(_heal_ready.wait(), timeout=3.0)
            await _manager.handle_request(scope, receive, send)
            log.warning("[mcp-http] self-heal succeeded — /mcp recovered")
            return
        except Exception as e2:
            log.warning(f"[mcp-http] self-heal retry failed: {e2}")
        await send({"type": "http.response.start", "status": 503,
                    "headers": [(b"content-type", b"text/plain")]})
        await send({"type": "http.response.body", "body": b"restarting"})

    return _asgi


@contextlib.asynccontextmanager
async def running():
    """Run the session manager's lifecycle; no-op if the app was never built."""
    if _manager is None:
        # This is the silent shape of the 07-03 17:45 dead boot — make it loud.
        log.error("[mcp-http] running() entered before build_asgi_app() — "
                  "/mcp would 503 all boot (self-heal will catch requests)")
        yield
        return
    async with _manager.run():
        yield

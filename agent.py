"""
Thin agentic loop — the RIGHT architecture (see the design discussion in
INTENT_ROUTING_PLAN.md). A capable MODEL is the brain; this harness is dumb
plumbing: it hands the model the conversation + a tool list, the MODEL decides to
talk or call a tool, and we just execute the tool and loop. No regex router, no
intent config, no naturalize step — the model does all of that natively, the way
Claude Code does.

Brain-swappable on purpose: `brain_call(prompt) -> text` is injected, so the SAME
loop runs on `claude -p` (capable, cloud) or a local Ollama model (fast, offline).
"Small model for small things, route to a bigger brain for bigger intents" is just
choosing which `brain_call` to pass.

Protocol: we ask the model to reply with ONE JSON object — either
  {"say": "<spoken reply>"}                      (talk; we're done)
  {"tool": "<name>", "args": {...}}              (fetch real data, then continue)
This works over plain text I/O, so it needs no provider-specific tool API and runs
identically across brains.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import tool_registry

# Flag-gated, OFF by default (2026-07-01): lets the model request several
# INDEPENDENT tool calls in one step instead of one-per-loop-iteration, e.g.
# "what's the weather and the stock price" as one concurrent batch instead of
# two sequential turns. Kept behind a flag because the JSON ReAct protocol is
# hard-won (see CLAUDE.md — the local model hallucinates with a native/richer
# tool API) and the prompt/loop for a live conversational system isn't the
# place to find that out the hard way. When OFF, the prompt never mentions
# the plural form exists and the parsing branch for it is unreachable in
# practice — byte-identical behavior to before this flag existed.
PARALLEL_TOOLS = os.getenv("ORB_PARALLEL_TOOLS", "0") == "1"

# Tools the conversational agent may call. Curated to the data/awareness tools
# (actions like volume/music stay on the deterministic fast lane for now). Sourced
# from the registry so descriptions/params stay truthful.
AGENT_TOOLS = (
    "get_weather", "get_news", "lookup_fact", "calculate", "get_system_info",
    "get_stock", "search_notes", "list_notes", "scrape_url", "search_images",
    "run_claude_code", "run_with_grok", "run_with_codex", "switch_brain", "propose_brain_switch",
    "take_screenshot", "read_file", "list_files", "write_file",
    "read_system_file", "list_system_dir", "read_backend_errors",
    "write_self_file", "remember_about_me", "update_situation",
    "send_notification", "schedule_notification", "schedule_task",
    "list_scheduled_notifications", "cancel_scheduled_notifications",
    "check_sessions", "message_session",
    "list_cli_chats", "read_cli_chat",
    "start_cc_session", "resume_cc_session",
    "stop_session", "pause_session", "resume_session",
    "detect_pc_sessions", "send_to_terminal_session", "start_terminal_session",
    "watch_stock", "unwatch", "list_watches",
    "add_mission", "complete_mission", "list_missions", "set_mission_mode",
    "mute_topic", "unmute_topic", "list_muted_topics", "watch_sessions",
    "check_email", "search_email", "read_email",
    "mind_status", "wake_mind",
    "brain_status", "self_audit", "play_pong",
    "analyze_screen",
    "set_preference", "list_preferences", "delete_preference",
    "mouse_click", "type_text", "press_key", "move_mouse", "get_screen_size", "scroll",
    "open_url",
)

# Escalation as a TOOL (the local↔cloud routing answer). The small/local model is
# the default brain; when it recognises a request that needs deeper reasoning,
# complex multi-step work, or building software, it CALLS this pseudo-tool and the
# harness runs the task on a bigger brain (Haiku via claude -p, or Codex later).
# Tool-choice IS the router — no separate classifier. This is also the safety net
# for capability-gating: heavy intents the 3B shouldn't attempt go up here.
ESCALATE_TOOL = "ask_bigger_brain"
_ESCALATE_DESC = (
    f"{ESCALATE_TOOL}(task): Hand a request to a more capable assistant when it "
    "needs deep reasoning, complex multi-step planning, writing or building "
    "software, or careful analysis beyond a quick answer. Pass the user's full "
    "request as 'task'. Use this instead of guessing or half-answering.")


def _render_tool(fn: dict) -> str:
    """One compact catalog line for a tools_schema()-shaped function spec."""
    props = fn["parameters"].get("properties", {})
    req = set(fn["parameters"].get("required", []))
    params = ", ".join(f"{p}{'' if p in req else '?'}" for p in props) or "—"
    return f'- {fn["name"]}({params}): {fn["description"]}'


def _tool_catalog(allow_escalate: bool, extra_tools: list[dict] | None = None,
                  allowed_tools: tuple | None = None) -> str:
    """Compact, truthful tool list built from the registry schema, plus any per-turn
    `extra_tools` (the PC tier's relayed phone tools — same tools_schema() shape:
    {"function": {"name","description","parameters"}}). Per-turn, never global.
    `allowed_tools` restricts the registry surface (the mind's envelope);
    None = the full conversational AGENT_TOOLS, byte-for-byte as before."""
    allowed = AGENT_TOOLS if allowed_tools is None else allowed_tools
    lines = []
    for spec in tool_registry.tools_schema():
        fn = spec["function"]
        if fn["name"] not in allowed:
            continue
        lines.append(_render_tool(fn))
    for spec in (extra_tools or []):
        lines.append(_render_tool(spec["function"]))
    if allow_escalate:
        lines.append(f"- {_ESCALATE_DESC}")
    return "\n".join(lines)


def _extract_json(text: str) -> dict | None:
    """Pull the first JSON object out of a model reply (tolerates fences/prose)."""
    if not text:
        return None
    text = re.sub(r"```(?:json)?", "", text).strip()
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    break
    return None


def _salvage_say(text: str) -> str | None:
    """Small models sometimes emit ALMOST-JSON ({"say": "hi", sir}) — never leak
    that to the user. Pull the say value out, or strip a bare tool-call to nothing."""
    if not text:
        return None
    m = re.search(r'"say"\s*:\s*"((?:[^"\\]|\\.)*)"', text)
    if m:
        return m.group(1).replace('\\"', '"').strip()
    # A raw tool-call leaked as the reply -> not speakable; signal no usable text.
    if re.search(r'"tool"\s*:', text):
        return None
    return None


def _clean_reply(text: str) -> str:
    """Final guard: never speak raw protocol JSON. Prefer a salvaged say, else the
    text with any JSON stripped."""
    if not text:
        return ""
    t = text.strip()
    if t.startswith("{") or '"say"' in t or '"tool"' in t:
        say = _salvage_say(t)
        if say:
            return say
        stripped = re.sub(r"\{.*\}", "", t, flags=re.DOTALL).strip()
        return stripped or "Let me know how I can help, sir."
    return t


@dataclass
class AgentResult:
    reply: str
    tools_used: list[str] = field(default_factory=list)
    steps: int = 0
    latency: float = 0.0


def _build_prompt(system: str, messages: list[dict], scratch: list[str],
                  allow_escalate: bool, extra_tools: list[dict] | None = None,
                  allowed_tools: tuple | None = None) -> str:
    convo = "\n".join(
        f"{'User' if m['role'] == 'user' else 'You'}: {m['content']}"
        for m in messages[-10:] if m.get("content"))
    p = (
        f"{system}\n\n"
        "You can call tools to get REAL, live information. For chit-chat, opinions, "
        "or follow-ups you can answer from the conversation, just reply. BUT when the "
        "user asks for real-world information you don't already have — news or what's "
        "going on / happening / the latest, weather, prices or markets, a fact, your "
        "system stats — you MUST call the matching tool. You are NOT limited to "
        "training data — you have LIVE tools, so never claim you 'lack a crystal "
        "ball', 'can't see current events', or 'don't have access'. If the user asks "
        "what's going on / the news / the latest, CALL get_news. Never deflect, never "
        "guess, never invent a number. If in doubt whether you have the data, call "
        "the tool.\n\n"
        f"TOOLS:\n{_tool_catalog(allow_escalate, extra_tools, allowed_tools)}\n\n"
        f"Conversation:\n{convo}\n\n"
    )
    if scratch:
        p += "Tool results so far this turn:\n" + "\n".join(scratch) + "\n\n"
    p += (
        "Reply with ONLY ONE JSON object, no other text:\n"
        '  {"say": "<your spoken reply, 1-2 sentences, in character>"}\n'
        "OR\n"
        '  {"tool": "<tool name>", "args": { ... }}\n'
    )
    if PARALLEL_TOOLS:
        p += (
            "OR, if you need SEVERAL tools that DON'T depend on each other's results "
            "(e.g. weather AND a stock price), call them all at once instead of one "
            "per step:\n"
            '  {"tools": [{"tool": "<name>", "args": {...}}, {"tool": "<name>", "args": {...}}]}\n'
            "Only batch calls that are genuinely independent — if one needs the other's "
            "result first, call them one at a time as before.\n"
        )
    return p


async def _run_parallel_batch(calls: list, scratch: list[str], used: list[str],
                              seen: set[str], extra_names: set[str],
                              on_tool_start, on_tool, dispatch_override) -> None:
    """Execute several independent tool calls concurrently. Same bookkeeping
    (scratch/used/seen/callbacks) as the sequential path in run_turn, just
    batched — only reachable when PARALLEL_TOOLS is on."""
    to_run: list[tuple[str, dict]] = []
    for call in calls:
        if not isinstance(call, dict):
            continue
        name = call.get("tool", "")
        args = call.get("args") or {}
        sig = f"{name}:{json.dumps(args, sort_keys=True)}"
        if sig in seen:
            continue
        seen.add(sig)
        to_run.append((name, args))

    if not to_run:
        scratch.append("- (batch had nothing new — you already have these results above; "
                       "give your spoken reply now)")
        return

    for name, args in to_run:
        if on_tool_start:
            try:
                await on_tool_start(name, args)
            except Exception:
                pass

    async def _one(name: str, args: dict) -> str:
        try:
            if dispatch_override and name in extra_names:
                return await dispatch_override(name, args)
            return await tool_registry.dispatch(name, args)
        except Exception as e:
            return f"[error: {e}]"

    results = await asyncio.gather(*[_one(n, a) for n, a in to_run])

    for (name, args), result in zip(to_run, results):
        used.append(name)
        scratch.append(f"- {name}({json.dumps(args)}) -> {str(result)[:600]}")
        if on_tool:
            try:
                await on_tool(name, args, str(result))
            except Exception:
                pass


async def run_turn(messages: list[dict], system: str,
                   brain_call: Callable[[str], Awaitable[str]],
                   max_steps: int = 4,
                   escalate_call: Callable[[str], Awaitable[str]] | None = None,
                   on_tool: Callable[[str, dict, str], Awaitable[None]] | None = None,
                   on_tool_start: Callable[[str, dict], Awaitable[None]] | None = None,
                   extra_tools: list[dict] | None = None,
                   dispatch_override: Callable[[str, dict], Awaitable[str]] | None = None,
                   allowed_tools: tuple | None = None,
                   ) -> AgentResult:
    """One conversational turn through the agentic loop. `messages` is the full
    history (last item = current user turn). `brain_call` runs the chosen (small)
    model on a text prompt. If `escalate_call` is given, the model may call the
    `ask_bigger_brain` tool to hand the task to it (Haiku/Codex) — that's the
    local→cloud route, decided by the model's own tool choice."""
    t0 = time.time()
    scratch: list[str] = []
    used: list[str] = []
    seen: set[str] = set()           # tool-call signatures, to break loops
    allow_escalate = escalate_call is not None
    allowed = AGENT_TOOLS if allowed_tools is None else tuple(allowed_tools)
    extra_names = {t["function"]["name"] for t in (extra_tools or [])}
    for step in range(max_steps):
        raw = await brain_call(_build_prompt(system, messages, scratch, allow_escalate, extra_tools, allowed))
        action = _extract_json(raw)
        if not action:
            # Didn't follow protocol — salvage a spoken line, never leak JSON.
            return AgentResult(_clean_reply(raw), used, step + 1, time.time() - t0)
        if "say" in action and "tool" not in action:
            return AgentResult(_clean_reply(str(action["say"])), used, step + 1, time.time() - t0)
        if PARALLEL_TOOLS and isinstance(action.get("tools"), list):
            await _run_parallel_batch(action["tools"], scratch, used, seen,
                                      extra_names, on_tool_start, on_tool, dispatch_override)
            continue
        name = action.get("tool", "")
        args = action.get("args") or {}
        # ESCALATION: the small model chose to hand off to the bigger brain.
        if name == ESCALATE_TOOL and allow_escalate:
            task = args.get("task") or (messages[-1]["content"] if messages else "")
            try:
                big = await escalate_call(task)
            except Exception as e:
                big = f"I tried to consult a deeper resource but couldn't, sir ({e})."
            used.append(ESCALATE_TOOL)
            return AgentResult(str(big).strip()[:800], used, step + 1, time.time() - t0)
        if name not in allowed and name not in extra_names:
            scratch.append(f"- '{name}' is not an available tool. Reply now with say.")
            continue
        sig = f"{name}:{json.dumps(args, sort_keys=True)}"
        if sig in seen:
            # Small models loop on the same call — we already have this result.
            scratch.append(f"- You ALREADY called {name} (result above). Do NOT call "
                           "it again — give your spoken reply now as {\"say\": ...}.")
            break
        seen.add(sig)
        if on_tool_start:
            try:
                await on_tool_start(name, args)
            except Exception:
                pass
        try:
            if dispatch_override and name in extra_names:
                result = await dispatch_override(name, args)   # PC tier: relay to the phone's OrbToolKit
            else:
                result = await tool_registry.dispatch(name, args)
        except Exception as e:
            result = f"[error: {e}]"
        used.append(name)
        scratch.append(f"- {name}({json.dumps(args)}) -> {str(result)[:600]}")
        if on_tool:
            try:
                await on_tool(name, args, str(result))
            except Exception:
                pass  # a card-spawn failure must never break the conversation
    # Out of steps / broke a loop — ask once for a final spoken summary of what we have.
    raw = await brain_call(_build_prompt(
        system, messages,
        scratch + ["(You have all needed results above. Now give ONLY your spoken "
                   "reply as {\"say\": \"...\"} — do not call any more tools.)"],
        allow_escalate=False, extra_tools=extra_tools, allowed_tools=allowed))
    action = _extract_json(raw)
    if action and action.get("say"):
        return AgentResult(_clean_reply(str(action["say"])), used, max_steps, time.time() - t0)
    return AgentResult(_clean_reply(raw), used, max_steps, time.time() - t0)

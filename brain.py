"""
Switchable / escalatable brain — ZERO AUTH via the `claude -p` CLI.

Default brain is the local Ollama model (fast, handles routing + simple chat).
For real reasoning, JARVIS shells out to `claude -p --model <haiku|sonnet|opus>`
— this machine's Claude Code is logged in, so it reaches REAL Claude with no
API key (verified: `claude -p` returns claude-opus-4-8). Two uses:

  1. Manual: "switch to Sonnet" -> every reply routes through that model.
  2. Auto-escalation: when local Qwen waffles/can't, escalate to Claude (Haiku
     by default) so JARVIS keeps moving toward a real answer instead of a
     canned dead-end.

`claude -p` spawns the full agent (~5-15s); fine per the user. We constrain the
prompt to a short spoken reply (no tools/actions) for conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger("orb")

MODELS = {"haiku", "sonnet", "opus", "fable"}
GROK_MODELS = {"grok", "grok-build"}
CODEX_MODELS = {"codex", "codex-mini", "codex-full"}
GEMINI_MODELS = {"gemini"}

# Tiers KNOWN-unreachable on this machine's accounts regardless of CLI login
# state — discovered live 2026-07-01: this ChatGPT-tier Codex login can only
# reach gpt-5.4-mini; `codex exec -m gpt-5.4` fails outright. /api/brains
# reports these honestly so the app's switcher grays them out instead of lying.
TIER_UNAVAILABLE: dict[str, str] = {
    "codex-full": "gpt-5.4 isn't reachable on this Codex login (account tier) — only the mini tier works",
}

LABELS = {
    "local": "the local model", "haiku": "Haiku", "sonnet": "Sonnet", "opus": "Opus",
    "fable": "Fable",
    "grok": "Grok", "grok-build": "Grok Build",
    "codex": "Codex (mini)", "codex-mini": "Codex mini", "codex-full": "Codex full",
    "gemini": "Gemini",
}

_current = "local"
_explicit = False  # True once the user has explicitly chosen a brain by voice

# Persist the active brain across backend restarts. Without this, a routine
# code-reload restart (which happens often during dev) silently reverts an
# explicit "switch to grok" back to the default with zero indication anything
# changed — confirmed live 2026-06-30: a switch to Grok succeeded, a restart 9
# seconds later wiped it, and the user had no way to know why it "didn't work."
_STATE_FILE = Path(__file__).parent / "data" / "brain_state.json"


def _save_state() -> None:
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text(json.dumps({"current": _current, "explicit": _explicit}))
    except Exception:
        pass


def _load_state() -> None:
    global _current, _explicit
    try:
        if _STATE_FILE.exists():
            d = json.loads(_STATE_FILE.read_text())
            _current = d.get("current", _current)
            _explicit = d.get("explicit", _explicit)
    except Exception:
        pass


_load_state()

# The agent spine's DEFAULT conversational brain. The user wants to talk to
# Haiku (its conversational quality + reliable tool-picking), with the local
# model used only on an explicit "switch to local" OR as an automatic fallback
# when claude -p is unavailable/rate-limited. Override via JARVIS_SPINE_BRAIN
# (e.g. =local to go back to the old fast/free default). Never silently use
# Sonnet/Opus — those require an explicit voice switch.
DEFAULT_AGENT_BRAIN = os.getenv("JARVIS_SPINE_BRAIN", "haiku").strip().lower()


def current_brain() -> str:
    return _current


def set_brain(key: str) -> None:
    global _current, _explicit
    if (key == "local" or key in MODELS or key in GROK_MODELS
            or key in CODEX_MODELS or key in GEMINI_MODELS):
        _current = key
        _explicit = True
        _save_state()


def user_overrode() -> bool:
    """Has the user explicitly picked a brain (vs running on the default)?"""
    return _explicit


def reset_brain() -> None:
    """Clear an explicit user choice so the router default takes over again.
    (effective_agent_brain() falls back to DEFAULT_AGENT_BRAIN when not explicit.)"""
    global _current, _explicit
    _current = "local"
    _explicit = False
    _save_state()


def effective_agent_brain() -> str:
    """Which brain the agent spine should run THIS turn. Default = Haiku; an
    explicit voice switch ('switch to local/opus/grok/...') overrides it."""
    if _explicit:
        return _current
    b = DEFAULT_AGENT_BRAIN
    return b if (b == "local" or b in MODELS or b in GROK_MODELS or b in CODEX_MODELS) else "haiku"


def is_claude() -> bool:
    return _current in MODELS


def is_grok() -> bool:
    return _current in GROK_MODELS


_LIMIT_RE = re.compile(
    r"usage limit|rate limit|session limit|hit your.{0,25}limit|reach(?:ed)?\b.{0,20}limit|"
    r"limit reached|limit.{0,20}reset|reset[s]?\b.{0,25}\b(?:am|pm|hours?|minutes?)\b|"
    r"too many requests|try again later|quota|temporarily unavailable|overloaded|"
    r"not logged in|please run .{0,10}login|/login", re.IGNORECASE)


def looks_like_limit(text: str) -> bool:
    """Heuristic: did `claude -p` return a usage/rate/session-limit, overload, or
    not-logged-in message instead of a real reply? Catches phrasings like
    'You've hit your session limit — resets 5:40am'. When true, the caller falls
    back to the local brain so the user NEVER hears the raw error. Bounded length
    so it won't misfire on a long normal answer that happens to mention 'limit'."""
    t = (text or "").strip()
    return bool(t) and len(t) < 300 and bool(_LIMIT_RE.search(t))


# ---------------------------------------------------------------------------
# Multi-backend brain router — "models are invisible background infrastructure"
# ---------------------------------------------------------------------------
# The same persona prompt is fed to whichever backend answers, so WHICH model
# replies is invisible to the user (the product is the only thing they see). The
# router tries backends in priority order, skips any on cooldown (recently
# throttled / errored), and rotates to the next on a limit/error. The last
# backend is the local FLOOR (is_floor=True) — never skipped — so a turn never
# dead-ends. Add Gemini-free / Codex / etc. as more Backends later; nothing else
# changes. Cooldown is short by default so a transient throttle recovers fast.

ROUTER_COOLDOWN_SEC = float(os.getenv("JARVIS_BACKEND_COOLDOWN", "120"))


@dataclass
class Backend:
    """One swappable brain in the chain. `call(prompt) -> text` (async)."""
    name: str
    call: Callable[[str], Awaitable[str]]
    is_floor: bool = False
    available: Callable[[], bool] = field(default=lambda: True)
    cooldown_until: float = 0.0
    trips: int = 0          # diagnostics: how many times it has been cooled down

    def ready(self) -> bool:
        if self.is_floor:
            return True
        return time.time() >= self.cooldown_until and bool(self.available())

    def trip(self, secs: float = ROUTER_COOLDOWN_SEC) -> None:
        """Put this backend on cooldown so the chain skips it until it recovers."""
        if self.is_floor:
            return
        self.cooldown_until = time.time() + secs
        self.trips += 1


class BrainRouter:
    """Try backends in order; skip cooled-down ones; rotate on limit/error; the
    floor always answers. Returns (text, backend_name). The caller wraps it so the
    agent loop just sees a plain `prompt -> text` brain_call."""

    def __init__(self, backends: list[Backend]):
        if not backends:
            raise ValueError("BrainRouter needs at least one backend")
        self.backends = backends

    async def generate(self, prompt: str) -> tuple[str, str]:
        for b in self.backends:
            if not b.ready():
                continue
            try:
                out = (await b.call(prompt) or "").strip()
            except Exception as e:
                log.warning(f"[router] {b.name} errored ({e}); cooling down")
                b.trip()
                continue
            if not out:
                log.warning(f"[router] {b.name} returned empty; cooling down")
                b.trip()
                continue
            if not b.is_floor and looks_like_limit(out):
                log.warning(f"[router] {b.name} hit a limit; cooling down -> next backend")
                record_brain_limit(b.name)
                b.trip()
                continue
            record_brain_call(b.name)
            return out, b.name
        # Even the floor failed (e.g. Ollama down) — let the caller speak a graceful line.
        return "", "none"


def label(key: str | None = None) -> str:
    return LABELS.get(key or _current, key or _current)


# ── Session-level usage tracking ───────────────────────────────────────────────
_CALL_COUNTS: dict[str, int] = {}
_LAST_USED: dict[str, float] = {}
_LAST_LIMIT: dict[str, float] = {}


def record_brain_call(key: str) -> None:
    _CALL_COUNTS[key] = _CALL_COUNTS.get(key, 0) + 1
    _LAST_USED[key] = time.time()


def record_brain_limit(key: str) -> None:
    _LAST_LIMIT[key] = time.time()


def brain_stats() -> dict:
    """Usage stats for all known brains — used by the brain_status tool."""
    all_keys = ["local", *sorted(MODELS), *sorted(GROK_MODELS),
                *sorted(CODEX_MODELS), *sorted(GEMINI_MODELS)]
    out = {}
    for k in all_keys:
        out[k] = {
            "label": LABELS.get(k, k),
            "calls": _CALL_COUNTS.get(k, 0),
            "last_used": _LAST_USED.get(k),
            "last_limit": _LAST_LIMIT.get(k),
        }
    return out


def claude_available() -> bool:
    """Zero-auth: just needs the `claude` CLI on PATH (logged-in Claude Code)."""
    return shutil.which("claude") is not None


# ---------------------------------------------------------------------------
# Generic OpenAI-compatible backends — the free-API rotation "cost engine"
# ---------------------------------------------------------------------------
# The router rotates through any number of these between Haiku and the local
# floor: "when one free API is exhausted, fall through to the next, so you never
# run out." One adapter covers the whole vision — any OpenAI-style
# /chat/completions endpoint works: Groq, OpenRouter (free models), Gemini's
# OpenAI-compat endpoint, or a local llama.cpp / LM Studio / vLLM / gaming-PC
# inference server streamed over the network.
#
# Configure with the JARVIS_FREE_BACKENDS env var (JSON list). UNSET = nothing
# changes (the default on this dev machine — Haiku -> local floor, as before):
#   JARVIS_FREE_BACKENDS='[{"name":"groq","base_url":"https://api.groq.com/openai/v1",
#     "model":"llama-3.3-70b-versatile","api_key_env":"GROQ_API_KEY"}]'
# A slot is "available" only when base_url + model are set AND (if it names an
# api_key_env) that env var is non-empty — so an unconfigured/keyless slot is
# skipped, never dead-ending a turn. Listed order = priority.

OPENAI_COMPAT_TIMEOUT = float(os.getenv("JARVIS_FREE_TIMEOUT", "30"))


def _openai_compat_call(base_url: str, model: str, api_key_env: str) -> Callable[[str], Awaitable[str]]:
    """An async `prompt -> text` call against an OpenAI-compatible chat endpoint.
    Raises on transport / non-2xx / quota (429) so the router cools it down and
    rotates to the next backend."""
    url = base_url.rstrip("/") + "/chat/completions"

    async def call(prompt: str) -> str:
        import httpx
        headers = {"Content-Type": "application/json"}
        key = os.getenv(api_key_env, "") if api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.5,
            "max_tokens": 320,
        }
        async with httpx.AsyncClient(timeout=OPENAI_COMPAT_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        return (data["choices"][0]["message"]["content"] or "").strip()

    return call


def openai_compat_backend(cfg: dict) -> Backend:
    """Build a router Backend from a config dict (see JARVIS_FREE_BACKENDS)."""
    name = cfg.get("name") or "free"
    base_url = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    api_key_env = cfg.get("api_key_env") or ""

    def available() -> bool:
        if not base_url or not model:
            return False
        if api_key_env and not os.getenv(api_key_env):
            return False
        return True

    return Backend(name, _openai_compat_call(base_url, model, api_key_env),
                   available=available)


def free_backends_from_env() -> list[Backend]:
    """Parse JARVIS_FREE_BACKENDS (a JSON list, or a single object) into router
    Backends in listed/priority order. Returns [] when unset/blank/malformed, so
    an absent or bad config silently changes nothing."""
    raw = os.getenv("JARVIS_FREE_BACKENDS", "").strip()
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except Exception as e:
        log.warning(f"[router] JARVIS_FREE_BACKENDS ignored (bad JSON): {e}")
        return []
    if isinstance(items, dict):
        items = [items]
    out: list[Backend] = []
    for cfg in items if isinstance(items, list) else []:
        if isinstance(cfg, dict) and cfg.get("base_url") and cfg.get("model"):
            out.append(openai_compat_backend(cfg))
        else:
            log.warning(f"[router] skipping free-backend entry missing base_url/model: {cfg!r}")
    if out:
        log.info(f"[router] free backends configured: {[b.name for b in out]}")
    return out


# ---------------------------------------------------------------------------
# Voice matcher (unchanged): explicit model names only; "switch to claude"
# stays a Claude Code trigger, not a brain switch.
# ---------------------------------------------------------------------------

_SWITCH_VERB = re.compile(r"\b(switch|change|swap|set|use|go)\b", re.IGNORECASE)
_BRAIN_WORDS = {
    "haiku": (r"haiku",), "sonnet": (r"sonnet",), "opus": (r"opus",),
    "local": (r"local", r"qwen", r"offline"),
    "grok-build": (r"grok[\s\-]build",),
    "grok": (r"\bgrok\b",),
    "codex-full": (r"codex[\s\-]full", r"codex[\s\-]pro", r"full[\s\-]codex",
                   r"bigger[\s\-]codex", r"stronger[\s\-]codex"),
    "codex-mini": (r"codex[\s\-]mini", r"cheap(?:er)?[\s\w]{0,15}openai",
                   r"openai[\s\w]{0,15}cheap", r"mini[\s\-]codex",
                   r"cheaper[\s\-](?:openai|codex|model)"),
    "codex": (r"\bcodex\b",),
}


def match_brain_switch(text: str) -> str | None:
    norm = text.lower().strip().rstrip(".!?,;:")
    if not norm or not _SWITCH_VERB.search(norm):
        return None
    for key, words in _BRAIN_WORDS.items():
        if any(re.search(r"\b" + w + r"\b", norm) for w in words):
            return key
    return None


# ---------------------------------------------------------------------------
# claude -p
# ---------------------------------------------------------------------------

async def _run_claude(prompt: str, model: str, timeout: float = 75.0) -> str:
    """Run `claude -p <prompt> --model <model>`, return the text reply."""
    proc = await asyncio.create_subprocess_exec(
        "claude", "-p", prompt, "--model", model,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("claude -p timed out")
    text = out.decode("utf-8", "ignore").strip()
    if not text:
        raise RuntimeError("claude -p empty: " + err.decode("utf-8", "ignore")[:200])
    return text


# 2026-07-18: the CLI dropped grok-composer-2.5-fast / grok-build; grok-4.5 is
# the only id it serves now (`grok models`).
_GROK_MODEL_MAP = {
    "grok": "grok-4.5",
    "grok-build": "grok-4.5",
}

# npm installs grok as a .cmd wrapper on Windows — not on hidden-process PATH.
# Resolve once at startup: check PATH first, then the fixed npm location.
def _find_grok_bin() -> str | None:
    found = shutil.which("grok") or shutil.which("grok.cmd")
    if found:
        return found
    npm_cmd = os.path.join(os.environ.get("APPDATA", ""), "npm", "grok.cmd")
    return npm_cmd if os.path.exists(npm_cmd) else None

_GROK_BIN: str | None = _find_grok_bin()


def grok_available() -> bool:
    if _GROK_BIN is None:
        return False
    # Check auth by looking for grok's real auth file — CLI on PATH but not
    # logged in means every call silently fails with an auth error. Verified
    # 2026-06-30: the grok CLI stores its login at ~/.grok/auth.json (NOT
    # %APPDATA%\grok\ — that path doesn't exist on Windows installs).
    auth_file = os.path.join(os.path.expanduser("~"), ".grok", "auth.json")
    return os.path.exists(auth_file)


# npm installs codex as a .cmd wrapper on Windows — resolve once at startup.
def _find_codex_bin() -> str | None:
    found = shutil.which("codex") or shutil.which("codex.cmd")
    if found:
        return found
    npm_cmd = os.path.join(os.environ.get("APPDATA", ""), "npm", "codex.cmd")
    return npm_cmd if os.path.exists(npm_cmd) else None

_CODEX_BIN: str | None = _find_codex_bin()


def codex_available() -> bool:
    if _CODEX_BIN is None:
        return False
    # Real check (was hardcoded False from a 2026-06-29 credits issue that's since
    # resolved — confirmed 2026-06-30 the account has credits again). codex CLI
    # stores its login at ~/.codex/auth.json.
    auth_file = os.path.join(os.path.expanduser("~"), ".codex", "auth.json")
    return os.path.exists(auth_file)


_CODEX_MODEL_MAP = {
    "codex": "gpt-5.4-mini",
    "codex-mini": "gpt-5.4-mini",
    "codex-full": "gpt-5.4",
}


async def _run_codex(prompt: str, model_key: str = "codex", timeout: float = 120.0) -> str:
    """Run `codex exec -m <model> -s workspace-write -` with prompt via stdin.

    Uses stdin to avoid Windows arg-length limits and to support multi-line prompts.
    workspace-write allows file edits so coding tasks can actually create output.
    """
    if not _CODEX_BIN:
        raise RuntimeError("codex CLI not found (npm i -g @openai/codex)")
    model = _CODEX_MODEL_MAP.get(model_key, "gpt-5.4-mini")
    if _CODEX_BIN.endswith(".cmd"):
        argv = ["cmd", "/c", _CODEX_BIN, "exec", "--skip-git-repo-check",
                "-m", model, "-s", "workspace-write", "-"]
    else:
        argv = [_CODEX_BIN, "exec", "--skip-git-repo-check",
                "-m", model, "-s", "workspace-write", "-"]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=os.path.join(os.path.expanduser("~"), "Desktop"),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("codex exec timed out")
    text = out.decode("utf-8", "ignore").strip()
    if not text:
        raise RuntimeError("codex empty: " + err.decode("utf-8", "ignore")[:200])
    return text


async def _run_grok(prompt: str, key: str, timeout: float = 45.0) -> str:
    """Run `grok --prompt-file <tmp> -m <model>`, return the text reply.

    Uses --prompt-file instead of -p/-single to avoid Windows cmd.exe's ~8191
    char argument length limit when the full system prompt + conversation is long.
    """
    if not _GROK_BIN:
        raise RuntimeError("grok CLI not found (npm i -g @xai-org/grok-cli)")
    model = _GROK_MODEL_MAP.get(key, "grok-4.5")
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="grok_prompt_")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        # .cmd files on Windows must go through cmd.exe
        if _GROK_BIN.endswith(".cmd"):
            argv = ["cmd", "/c", _GROK_BIN, "--prompt-file", tmp_path, "-m", model]
        else:
            argv = [_GROK_BIN, "--prompt-file", tmp_path, "-m", model]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            raise RuntimeError("grok --prompt-file timed out")
        text = out.decode("utf-8", "ignore").strip()
        if not text:
            raise RuntimeError("grok empty: " + err.decode("utf-8", "ignore")[:200])
        return text
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


# Google's gemini-cli — the free-tier rung (~1000 requests/day on a plain
# Google login; his 07-04 ask). npm installs it as a .cmd wrapper on Windows,
# same resolution dance as grok/codex.
def _find_gemini_bin() -> str | None:
    found = shutil.which("gemini") or shutil.which("gemini.cmd")
    if found:
        return found
    npm_cmd = os.path.join(os.environ.get("APPDATA", ""), "npm", "gemini.cmd")
    return npm_cmd if os.path.exists(npm_cmd) else None

_GEMINI_BIN: str | None = _find_gemini_bin()


def gemini_available() -> bool:
    # Presence + first-login marker (~/.gemini is created on first auth). A
    # logged-out call fails fast and the fallback ladder catches it, so don't
    # over-gate here.
    return _GEMINI_BIN is not None and os.path.isdir(
        os.path.join(os.path.expanduser("~"), ".gemini"))


async def _run_gemini(prompt: str, timeout: float = 60.0) -> str:
    """Run gemini-cli one-shot with the prompt piped via stdin (arg-limit safe)."""
    if not _GEMINI_BIN:
        raise RuntimeError("gemini CLI not found (npm i -g @google/gemini-cli)")
    if _GEMINI_BIN.endswith(".cmd"):
        argv = ["cmd", "/c", _GEMINI_BIN]
    else:
        argv = [_GEMINI_BIN]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        # home, not the repo — keeps repo-level GEMINI.md/context files out of
        # the voice brain's context (same reason the claude brain runs in
        # ~/.orb_brain)
        cwd=os.path.expanduser("~"),
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        raise RuntimeError("gemini timed out")
    text = out.decode("utf-8", "ignore").strip()
    if not text:
        raise RuntimeError("gemini empty: " + err.decode("utf-8", "ignore")[:200])
    return text


def _spoken(system: str, convo: str, user: str) -> str:
    return (f"{system}\n\n"
            + (f"Recent conversation:\n{convo}\n\n" if convo else "")
            + f"The user just said: \"{user}\"\n\n"
            "Respond in ONE or two natural spoken sentences, in character. "
            "No markdown, no lists, no tool use, no actions — just the reply.")


async def claude_oneshot(system: str, user: str, temp: float = 0.7,
                         max_tokens: int = 200) -> str:
    model = _current if _current in MODELS else "haiku"
    return await _run_claude(_spoken(system, "", user), model)


async def claude_chat(messages: list[dict], system: str, max_tokens: int = 250) -> str:
    model = _current if _current in MODELS else "haiku"
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages[-8:] if m.get("content"))
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    return await _run_claude(_spoken(system, convo, user), model)


async def escalate(messages: list[dict], system: str, model: str = "haiku") -> str:
    """Force a real-Claude answer regardless of the current brain — used when
    the local model waffles, so JARVIS keeps moving toward a real answer.

    Also lets Claude honestly flag when the user actually wants a real-world
    DOING task it can't perform by speaking: it appends a line
    `OFFER_CLAUDE: <task>` which the caller turns into a 'shall I use Claude
    Code?' offer (and runs on a yes). This is how JARVIS stops pretending and
    instead routes real work to Claude Code."""
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages[-8:] if m.get("content"))
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    prompt = (
        f"{system}\n\n"
        + (f"Recent conversation:\n{convo}\n\n" if convo else "")
        + f"The user just said: \"{user}\"\n\n"
        "Respond in ONE or two natural spoken sentences, in character, no markdown.\n"
        "BUT: if the user is asking you to actually DO a real-world task you "
        "cannot do yourself (write/build software, automate a workflow, create "
        "files or a project, deep multi-step research), do NOT pretend you can "
        "and do NOT refuse flatly. Give one honest sentence, then on a NEW line "
        "output exactly: OFFER_CLAUDE: <a concise description of the task to hand "
        "to Claude Code>."
    )
    return await _run_claude(prompt, model)


async def escalate_with_card(messages: list[dict], system: str,
                             model: str = "haiku") -> tuple[str, dict | None, str | None]:
    """Like escalate(), but in ONE claude -p call also asks for a structured HUD
    card payload — so a deeper request both speaks AND fills the on-screen card.

    Returns (spoken_reply, card_payload | None, offer_task | None):
      - offer_task set  -> a real DOING task to hand to Claude Code (no card).
      - card_payload    -> render2-ready dict {title,headline,sub,kv,note} when the
                           answer has concrete data worth showing; else None.
    One call keeps latency at a single ~5-15s claude -p hop, not two."""
    convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages[-8:] if m.get("content"))
    user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "")
    prompt = (
        f"{system}\n\n"
        + (f"Recent conversation:\n{convo}\n\n" if convo else "")
        + f"The user just said: \"{user}\"\n\n"
        "Respond in ONE or two natural spoken sentences, in character, no markdown.\n"
        "THEN, if your answer contains concrete data worth displaying on a heads-up "
        "card (numbers, a definition, specs, a short list, comparisons), append on a "
        "NEW line exactly:\n"
        "CARD_JSON: {\"title\":\"<=40 chars\",\"headline\":\"short key figure or empty\","
        "\"sub\":\"one short line or empty\",\"kv\":[[\"LABEL\",\"value\"]],\"note\":\"prose or empty\"}\n"
        "Use only the fields that apply; keep headline very short (a number/temp/name); "
        "put stat pairs in kv; put any prose in note. Omit the CARD_JSON line entirely "
        "if there's nothing worth displaying.\n"
        "NOTE: you CAN already show information, images, charts/graphs, and "
        "rundowns as on-screen HUD cards — so for 'show me X', 'make a HUD/view of "
        "X', 'graph X', 'look up X' style requests, just answer + CARD_JSON. Do NOT "
        "offer Claude Code for those.\n"
        "BUT: if the user is asking you to actually DO a real-world task you cannot do "
        "by speaking or by showing a card (write/build SOFTWARE, automate a workflow, "
        "create files or a project, deep multi-step engineering), do NOT pretend and "
        "do NOT output CARD_JSON — instead give one honest sentence then on a NEW "
        "line output exactly: OFFER_CLAUDE: <concise task>."
    )
    raw = await _run_claude(prompt, model)

    if "OFFER_CLAUDE:" in raw:
        spoken, _, task = raw.partition("OFFER_CLAUDE:")
        return spoken.strip(), None, task.strip()

    card = None
    spoken = raw
    if "CARD_JSON:" in raw:
        spoken, _, js = raw.partition("CARD_JSON:")
        spoken = spoken.strip()
        try:
            parsed = json.loads(js.strip())
            if isinstance(parsed, dict):
                card = parsed
        except Exception:
            card = None  # malformed JSON -> just speak, no card
    return spoken, card, None


async def research_card(query: str, model: str = "haiku",
                        sources: str = "") -> tuple[str, dict, str | None]:
    """GLANCEABLE 'tell me about X' briefing via one Haiku call. Returns
    (spoken, card, image_query):
      - spoken      -> ONE short in-character sentence to say aloud.
      - card        -> {headline, kv:[[label,value]], note} for a SCAN-not-read
                       layout: a one-line essence, 3-5 key facts, then a 2-3
                       sentence gist (NOT two paragraphs).
      - image_query -> clean subject to fetch photos for, or None.
    Always Haiku (cheap). If `sources` is given (live web + Wikipedia), Haiku
    synthesizes from THOSE — current and accurate — instead of stale memory."""
    src_block = ""
    if sources:
        src_block = (
            "\nUSE THESE CURRENT SOURCES as your primary basis — they are fetched "
            "live today, so PREFER their figures and dates over your own memory "
            "(which may be out of date). If a source gives a recent number (net "
            "worth, price, year), use it:\n----\n" + sources + "\n----\n")
    import personas as _personas
    _me = _personas.active().display_name
    prompt = (
        f"You are {_me}. The user asked to be told about: "
        f"\"{query}\".\n" + src_block + "\n"
        "Identify the MOST likely intended subject (most famous match if ambiguous). "
        "The user wants to GLANCE and get it — not read paragraphs. Reply in EXACTLY "
        "this format, nothing before or after:\n"
        "SPOKEN: <one short spoken sentence, in character, addressing the user as sir>\n"
        "HEADLINE: <a punchy one-line essence of what this is, max ~60 chars>\n"
        "FACTS:\n"
        "- <Label>: <short value>\n"
        "- <Label>: <short value>\n"
        "(give 3 to 5 of the most important at-a-glance facts; keep each value short)\n"
        "SUMMARY: <2 to 3 sentences MAX — the gist, plain prose, no markdown>\n"
        "IMAGES: <a PRECISE image-search query that will surface THIS EXACT subject "
        "— add the key disambiguator from the sources (profession/platform/handle/"
        "what they're famous for, e.g. 'Mikah Lynn TikTok creator' not just 'Mikah "
        "Lynn'), so a generic name doesn't return the wrong person. NONE if not a "
        "person/place/visible thing.>"
    )
    raw = await _run_claude(prompt, model)

    def _grab(label, nxt):
        m = re.search(rf"{label}:\s*(.+?)(?:\n(?:{nxt}):|\Z)", raw, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""

    spoken = _grab("SPOKEN", "HEADLINE|FACTS|SUMMARY|IMAGES")
    headline = _grab("HEADLINE", "FACTS|SUMMARY|IMAGES")
    facts_raw = _grab("FACTS", "SUMMARY|IMAGES")
    summary = _grab("SUMMARY", "IMAGES")
    img_raw = _grab("IMAGES", "$")

    kv = []
    for line in facts_raw.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if ":" in line:
            k, _, v = line.partition(":")
            if k.strip() and v.strip():
                kv.append([k.strip(), v.strip()])
    image_query = None if (not img_raw or img_raw.upper() == "NONE") else img_raw.splitlines()[0].strip()

    if not spoken:
        spoken = f"Here's the rundown on {query}, sir."
    card = {"headline": headline or None, "kv": kv, "note": summary or (raw.strip() if not kv else "")}
    return spoken, card, image_query


async def classify_to_command(text: str, model: str = "haiku") -> str:
    """Semantic router for the long tail: when the deterministic regex router finds
    NO match, Haiku rewrites the request into ONE canonical command the router DOES
    understand, so we reuse every existing handler. Returns 'CHAT' for
    greetings/smalltalk/unclear. Always Haiku (cheap)."""
    prompt = (
        "You route requests for a desktop AI assistant. Rewrite the user's request "
        "as ONE canonical command from this list (pick the best fit):\n"
        "- info/facts/explain/who/what/why/compare  ->  tell me about <subject>\n"
        "- wants to SEE pictures/photos of something ->  show me pictures of <subject>\n"
        "- a stock / index / crypto / commodity price ->  <subject> stock\n"
        "- a data graph/trend of some metric over time ->  graph of <subject>\n"
        "- weather/forecast                          ->  weather in <place>\n"
        "- news/headlines                            ->  news about <topic>\n"
        "- play a game (snake/pong/2048)             ->  play <game>\n"
        "- deep/thorough multi-source research       ->  research <subject>\n"
        "- greeting / smalltalk / unclear / none     ->  CHAT\n\n"
        f"User request: \"{text}\"\n"
        "Reply with ONLY the single command line (or the word CHAT), nothing else."
    )
    out = await _run_claude(prompt, model)
    line = (out or "").strip().strip('"').splitlines()[0] if out else ""
    return line or "CHAT"


async def resolve_reference(text: str, subject: str, model: str = "haiku") -> str:
    """Rewrite a referential request into a concrete standalone command using the
    current focus subject. 'show me pictures of this person that fit X' (focus =
    Mikah Lynn) -> 'show me pictures of Mikah Lynn that fit X'. Keeps the user's
    extra criteria. Always Haiku (cheap)."""
    prompt = (
        f"The user is currently focused on: \"{subject}\".\n"
        f"Their new request: \"{text}\"\n\n"
        "Rewrite the request as ONE concrete standalone command, resolving every "
        "reference ('this person', 'him', 'her', 'them', 'this', 'that', 'based on "
        "that/the research', 'it') to the subject. KEEP any extra criteria the user "
        "added (e.g. 'wearing a suit', 'recent', 'that fit X'). Return ONLY the "
        "rewritten command on one line, nothing else."
    )
    out = await _run_claude(prompt, model)
    line = (out or "").strip().strip('"').splitlines()[0] if out else ""
    return line or text


async def decompose(cmd: str, model: str = "haiku") -> list[str]:
    """Split a compound request into separate ATOMIC requests (one per line) so
    each can open its own HUD. 'images of A and B' -> ['images of A','images of
    B']; 'tell me about Tesla and its stock price' -> ['tell me about Tesla',
    'Tesla stock price']. Returns a single-item list if it's not compound.
    Always Haiku (cheap)."""
    prompt = (
        "Split the user's request into separate ATOMIC requests, ONE PER LINE, "
        "preserving each intent. If it is already a single request, return it "
        "unchanged on one line. Do not number or bullet them. Examples:\n"
        "'show me images of Elon Musk and Millie Bobby Brown' =>\n"
        "show me images of Elon Musk\nshow me images of Millie Bobby Brown\n\n"
        "'tell me about Tesla and its current stock price' =>\n"
        "tell me about Tesla\nTesla stock price\n\n"
        f"Request: \"{cmd}\"\nReturn ONLY the request lines, nothing else."
    )
    raw = await _run_claude(prompt, model)
    out = []
    for line in raw.splitlines():
        s = line.strip().lstrip("-•*0123456789. ").strip(' "\'')
        if s and len(s) > 1:
            out.append(s)
    return out[:10] if out else [cmd]


async def graph_data(query: str, model: str = "haiku") -> dict | None:
    """Haiku-generated data series for a CUSTOM graph (e.g. 'YouTube revenue per
    year', 'net worth over time') — for things with no live data feed. Always
    Haiku (cheap). Returns {title, spoken, unit, labels, points, note} or None.
    Clearly framed as approximate/reported figures."""
    prompt = (
        f"The user wants a GRAPH of: \"{query}\".\n"
        "Provide approximate but realistic data to plot (reported/estimated public "
        "figures). Reply in EXACTLY this format, nothing else:\n"
        "SPOKEN: <one short spoken sentence, in character, addressing the user as sir>\n"
        "TITLE: <short chart title, max ~40 chars>\n"
        "UNIT: <the value unit, e.g. $B, %, million, USD>\n"
        "POINTS:\n"
        "<label>: <number only>\n"
        "<label>: <number only>\n"
        "(6 to 12 points in chronological order; the value must be a plain number, "
        "no units, no commas)\n"
        "NOTE: <one sentence noting these are approximate/reported figures>"
    )
    raw = await _run_claude(prompt, model)

    def _grab(label, nxt):
        m = re.search(rf"{label}:\s*(.+?)(?:\n(?:{nxt}):|\Z)", raw, re.IGNORECASE | re.DOTALL)
        return m.group(1).strip() if m else ""

    spoken = _grab("SPOKEN", "TITLE|UNIT|POINTS|NOTE")
    title = _grab("TITLE", "UNIT|POINTS|NOTE")
    unit = _grab("UNIT", "POINTS|NOTE")
    points_raw = _grab("POINTS", "NOTE")
    note = _grab("NOTE", "$")

    labels, points = [], []
    for line in points_raw.splitlines():
        line = line.strip().lstrip("-•* ").strip()
        if ":" in line:
            k, _, v = line.partition(":")
            num = re.sub(r"[^0-9.\-]", "", v)
            try:
                points.append(float(num))
                labels.append(k.strip())
            except ValueError:
                continue
    if len(points) < 2:
        return None
    return {
        "title": (title or query)[:40], "spoken": spoken or f"Here's the graph, sir.",
        "unit": unit, "labels": labels, "points": points, "note": note,
    }


# Phrases that signal the local model gave a weak / dead-end answer worth
# escalating to Claude for a real one.
_WEAK = re.compile(
    r"\b(i (?:can'?t|cannot|am unable|don'?t know|do not know|am not able|"
    r"have no (?:idea|information)|don'?t have)|i'?m not sure|not able to|"
    r"unable to|no information|i don'?t understand)\b", re.IGNORECASE)


def is_weak_reply(reply: str) -> bool:
    r = (reply or "").strip()
    if len(r) < 12:
        return True
    return bool(_WEAK.search(r))

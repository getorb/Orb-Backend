"""
Orb Server — Windows Edition
Voice AI with local Ollama brain + Edge TTS + Claude Code integration + desktop commands.
"""

import asyncio
import base64
import contextlib
import contextvars
import hashlib
import io
import json
import logging
import os
import re
import shutil
import sys
import subprocess
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

# Load .env into the environment BEFORE any os.getenv calls below. Without
# this, ORB_TOKEN (and other settings) were never read from .env, so the
# server auto-generated a fresh random token every restart that never matched
# the bundle/config.json — the real cause of the mobile auth failures.
_dotenv = Path(__file__).resolve().with_name(".env")
if _dotenv.is_file():
    for _entry in map(str.strip, _dotenv.read_text(encoding="utf-8").splitlines()):
        if not _entry or _entry[0] == "#" or "=" not in _entry:
            continue
        _name, _sep, _value = _entry.partition("=")
        os.environ.setdefault(_name.strip(), _value.strip().strip("\"'"))

import edge_tts
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

import stt
import tools
import memory_store
import tool_registry
import intent_router
import connectors
import brain
import tasks_store
import screen_bridge
import agent

# Optional legacy modules — NOT shipped in the public snapshot (2026-07-23 trim):
# site_templates seeds build prompts with reference URLs; semantic_router is the
# flag-off Layer-1 embedding matcher. Both degrade to None cleanly when absent.
try:
    import site_templates
except ImportError:
    site_templates = None
try:
    import semantic_router
except ImportError:
    semantic_router = None

# AGENT SPINE — the thin agentic loop (agent.py) as the conversational brain:
# the model sees history + tools and decides to talk or call a tool, escalating to
# Haiku only when it needs a bigger brain. Default ON. Set ORB_AGENT_SPINE=0 to
# fall back to the legacy router-first pipeline (kept for A/B + safety).
_AGENT_SPINE = os.getenv("ORB_AGENT_SPINE", "1").lower() not in ("0", "false", "no")

# INSTANT-ACK layer — when the spine runs on a slow claude brain, speak a short
# in-character "heard you" BEFORE the real answer so the turn never feels dead
# during the ~10-13s Haiku latency. Sent as an ADDITIVE {type:"ack"} WS message
# (a client that doesn't handle it simply ignores it — no breakage). DEFAULT OFF
# until the mobile UI wires ack-then-answer playback (see MAC_DELEGATION.md);
# flip on with ORB_ACK=1.
_AGENT_ACK = os.getenv("ORB_ACK", "0").lower() not in ("0", "false", "no")

# Long-term memory (memory.py): inject relevant learned facts into the agent's
# context, and distill new facts after each turn (extraction runs on the LOCAL
# model, off the claude -p budget). DEFAULT OFF — the "knowing when to say what"
# relevance gate needs tuning WITH the user before it shapes live replies. See
# PERSONALIZATION_PLAN.md. Enable with ORB_MEMORY=1.
_AGENT_MEMORY = os.getenv("ORB_MEMORY", "0").lower() not in ("0", "false", "no")


async def _safe_send(ws: WebSocket, payload: dict):
    """Send JSON to a websocket, swallowing errors if it's already closed."""
    try:
        if ws.client_state.name == "CONNECTED":
            await ws.send_text(json.dumps(payload))
    except Exception:
        pass


async def _safe_send_text(ws: WebSocket, text: str):
    """Send a pre-serialized JSON string, swallowing errors if closed."""
    try:
        if ws.client_state.name == "CONNECTED":
            await ws.send_text(text)
    except Exception:
        pass


_CHAT_LOG = Path(__file__).parent / "data" / "chat_log.jsonl"


def _log_chat(role: str, text: str, intent: str = ""):
    """Append a conversation turn to a persistent JSONL log (survives restarts) so
    real conversations can be reviewed to diagnose behaviour."""
    try:
        _CHAT_LOG.parent.mkdir(exist_ok=True)
        with open(_CHAT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.strftime("%Y-%m-%d %H:%M:%S"),
                                "role": role, "text": text,
                                **({"intent": intent} if intent else {})}) + "\n")
    except Exception:
        pass


# All connected WS clients (orb overlay, chat HUD, mobile). Orb-state updates are
# BROADCAST to all so the orb animates no matter which client triggered the turn
# (e.g. typing in the chat HUD must animate the orb the same as voice does).
_CLIENTS: set = set()
_MOBILE_CLIENTS: set = set()  # subset of _CLIENTS that are phone connections


async def _broadcast_status(state: str):
    for c in list(_CLIENTS):
        await _safe_send(c, {"type": "status", "state": state})


# The CURRENT working focus — what Orb is on right now. SHARED across all
# connections (a new chat window must not forget what's being worked on); persists
# until explicitly dropped ("forget that" / "never mind"). Single-user assistant,
# so a process-global focus is correct.
_FOCUS = {"subject": None}

# Current BACKGROUND job (deep research) — so the user can keep talking while it
# runs, ask "how's it coming", and get told when it's done. Process-global
# (single user). Cards pop as the work lands; completion is announced.
_BG = {"desc": None, "status": "idle", "started": 0.0}

# Mobile dispatch (Phase 1). A phone can't run the Qt card windows (pythonw +
# PySide6), so when the responding client is MOBILE the _spawn_* helpers send a
# WS message instead of spawning a window — the mobile HUD (mobile_hud.ts) then
# renders the SAME card.html in an in-page panel. The DESKTOP path is unchanged.
# The current client is carried per-connection in a ContextVar set in voice_ws;
# it's visible to the synchronous _spawn_* helpers called from within the
# connection's process_transcript task (contextvars propagate through sync calls).
# NOTE: until the mobile client identifies itself (Phase 2 handshake), every
# connection is "desktop", so _dispatch_to_mobile always returns False and the
# desktop behaviour is byte-for-byte unchanged.
_current_client: contextvars.ContextVar = contextvars.ContextVar("current_client", default=None)


async def _safe_ws_send(ws, obj: dict) -> bool:
    """Returns True only if the message was actually handed to a CONNECTED
    socket without raising — callers that need to know delivery actually
    happened (not just attempted) should check this, not assume."""
    try:
        if ws.client_state.name == "CONNECTED":
            await ws.send_text(json.dumps(obj))
            return True
    except Exception as e:
        log.warning(f"[mobile dispatch] send failed: {e}")
    return False


def _dispatch_to_mobile(obj: dict) -> bool:
    """If the current responding client is mobile, fire it a HUD message and
    return True so the caller SKIPS the desktop Qt spawn. Else return False and
    the desktop path runs exactly as before."""
    client = _current_client.get()
    if client and client.get("kind") == "mobile":
        ws = client.get("ws")
        try:
            asyncio.get_running_loop().create_task(_safe_ws_send(ws, obj))
        except RuntimeError:
            pass
        return True
    return False


def _set_focus(state: dict, subject):
    if subject:
        state["last_subject"] = subject
        _FOCUS["subject"] = subject


def _get_focus(state: dict):
    return state.get("last_subject") or _FOCUS.get("subject")


async def _settle_others_idle(origin_ws, delay: float):
    """After a reply, return OTHER clients' orbs to idle. They animated on the
    broadcast 'thinking' but (unlike the originator) don't receive the audio to
    self-idle on playback end — so without this the orb stays grown ('stuck big'
    after a chat turn). The originator is excluded so voice is unaffected."""
    try:
        await asyncio.sleep(delay)
        for c in list(_CLIENTS):
            if c is not origin_ws:
                await _safe_send(c, {"type": "status", "state": "idle"})
    except Exception:
        pass

logging.basicConfig(level=logging.INFO, format="%(asctime)s [orb] %(message)s")
log = logging.getLogger("orb")

import personas

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3.5:9b")
# HTTP/WS bind port. ORB_PORT (or legacy ORB_PORT) in .env moves everything —
# the bind, the resident /mcp URL, the internal relay base, and screen URLs.
PORT = int(os.getenv("ORB_PORT") or "8340")
# Persona controls wake word, system prompt, TTS voice, address form, and orb hue.
# Override with ORB_PERSONA / ORB_PERSONA in env or .env (default: orb — the
# neutral public persona). Individual settings below can still override the
# persona defaults (TTS_VOICE, USER_NAME).
PERSONA = personas.get(os.getenv("ORB_PERSONA") or os.getenv("ORB_PERSONA") or "orb")
TTS_VOICE = os.getenv("TTS_VOICE", PERSONA.tts_voice)
USER_NAME = os.getenv("USER_NAME") or PERSONA.address_user_as or "the user"
# Canned-reply vocative: honorific personas say "sir"/"human"; personas without
# one (Orb) address by real name, or drop the address when no name is set.
_VOC = PERSONA.address_user_as or (os.getenv("USER_NAME") or "").strip()


def _voc() -> str:
    return f", {_VOC}" if _VOC else ""

# Auth token for the WebSocket. Required when accessed over the internet
# (e.g. mobile PWA via Cloudflare Tunnel). If empty, no auth is enforced
# (intended for purely-local desktop use). Connections from 127.0.0.1 are
# always allowed.
import secrets as _secrets
ORB_TOKEN = os.getenv("ORB_TOKEN", "")
if not ORB_TOKEN:
    # Auto-generate one on first launch and persist it to .env so it's stable
    ORB_TOKEN = _secrets.token_urlsafe(24)
    try:
        env_path = Path(__file__).parent / ".env"
        existing = env_path.read_text() if env_path.exists() else ""
        if "ORB_TOKEN=" not in existing:
            with env_path.open("a", encoding="utf-8") as f:
                f.write(f"\nORB_TOKEN={ORB_TOKEN}\n")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Desktop commands — fast, deterministic, no LLM needed
# ---------------------------------------------------------------------------

import random as _random

# Desktop commands with varied replies so Orb doesn't sound robotic.
def _desktop_reply(app: str) -> str:
    templates = [
        f"{app} is open, sir.",
        f"Pulled up {app}.",
        f"{app} launched, sir.",
        f"{app} is ready.",
        f"Opening {app} now, sir.",
        f"{app}, at your disposal.",
        f"Done, sir. {app} is up.",
    ]
    return _random.choice(templates)

DESKTOP_COMMANDS: dict[str, dict] = {
    "open chrome": {"url": "https://www.google.com", "app": "your browser"},
    "open browser": {"url": "https://www.google.com", "app": "your browser"},
    "open vs code": {"cmd": "start code", "app": "VS Code"},
    "open vscode": {"cmd": "start code", "app": "VS Code"},
    "open visual studio code": {"cmd": "start code", "app": "VS Code"},
    "open terminal": {"cmd": "start wt", "app": "Terminal"},
    "open command prompt": {"cmd": "start cmd", "app": "Command Prompt"},
    "open powershell": {"cmd": "start powershell", "app": "PowerShell"},
    "open explorer": {"cmd": "start explorer", "app": "Explorer"},
    "open file explorer": {"cmd": "start explorer", "app": "File Explorer"},
    "open task manager": {"cmd": "start taskmgr", "app": "Task Manager"},
    "open notepad": {"cmd": "start notepad", "app": "Notepad"},
    "open settings": {"cmd": "start ms-settings:", "app": "Windows Settings"},
    "open calculator": {"cmd": "start calc", "app": "Calculator"},
    "open obs": {"cmd": "start obs64.exe", "app": "OBS Studio"},
    "open discord": {"cmd": "start discord", "app": "Discord"},
    "open spotify": {"cmd": "start spotify", "app": "Spotify"},
    "open steam": {"cmd": "start steam", "app": "Steam"},
    "open youtube": {"url": "https://youtube.com", "app": "YouTube"},
    "open github": {"url": "https://github.com", "app": "GitHub"},
    "open davinci resolve": {"cmd": "start resolve", "app": "DaVinci Resolve"},
}

# Note: "look up X" used to open a Google search in Chrome. That collides
# with the intent router's lookup_fact (which the user wants for factual
# questions). So "look up" is now a lookup_fact trigger, not a browser
# search. Only "search for X" / "google X" still open Chrome.
SEARCH_PREFIXES = ["search for", "google", "search the web for"]

# Claude Code trigger phrases — building AND inspecting/awareness tasks
# Explicitly NAMING the build tool is unambiguous intent — match anywhere in
# the sentence ("get claude code to make a chess game").
CLAUDE_EXPLICIT_TRIGGERS = [
    "claude code", "open claude", "start claude", "switch to claude",
]
# Imperative build / inspect verbs. These must START the command (the text is
# run through _command_form first, which strips polite/question framing), so a
# mid-sentence "make a"/"build a" in ordinary conversation ("that'll make a big
# difference", "I want to build a habit") no longer fires a build. Matched with
# a word boundary so "make an appointment" / "build another" don't false-fire.
CLAUDE_PREFIX_TRIGGERS = [
    "build me", "build this", "build a", "code this", "code me", "make me a",
    "make a", "create a", "create me", "write me", "fix this code", "debug this",
    # Awareness / delegation — Claude inspects files/folders and reports back.
    # Kept narrow on purpose: "tell me about the" was removed because it
    # collided with lookup_fact ("tell me about the Eiffel Tower"). For
    # filesystem inspection the user can still say "look in the folder",
    # "check the directory", "describe what's in", etc.
    "describe what", "what's in the", "what is in the", "look in the", "look at the",
    "read the file", "summarize the file", "check the folder", "list the files",
    "what files", "show me what's",
]

# Mute trigger phrases
MUTE_TRIGGERS = ["mute yourself", "mute", "go to sleep", "shut up", "be quiet", "stop listening", "go silent", "stand down", "go dormant"]

# Wake word — transcript MUST contain this or it's ignored entirely
WAKE_WORD = "orb"

def contains_wake_word(text: str) -> bool:
    """True if the transcript addresses the active persona (Orb or ULTRON).
    Accepts the persona's full variant list plus a few common STT mishears for
    Orb. The variant list comes from personas.py."""
    lower = text.lower()
    # Persona-supplied variants
    if any(v in lower for v in PERSONA.wake_words):
        return True
    # Extra STT-mishear variants only for the Orb persona
    if PERSONA.name == "orb":
        extra = ("jervis", "gervis", "travis", "harvis", "jarfis", "garvis",
                 "service", "jar vis", "jar-vis")
        return any(v in lower for v in extra)
    return False

def strip_wake_word(text: str) -> str:
    """Remove the persona's wake word (and lazy variants) from the start."""
    import re
    # Build pattern from the active persona's variants, longest-first so
    # 'orb' matches before 'jarv'.
    variants = sorted(PERSONA.wake_words, key=len, reverse=True)
    if PERSONA.name == "orb":
        variants = variants + ["jervis", "gervis", "travis", "harvis", "jarfis",
                               "garvis", "service", "jar vis", "jar-vis"]
    escaped = [re.escape(v) for v in variants]
    pattern = r"^(?:hey\s+)?(?:" + "|".join(escaped) + r")[,\s]*"
    stripped = re.sub(pattern, "", text, count=1, flags=re.IGNORECASE).strip()
    return stripped if stripped else text


def _strip_to_empty(text: str) -> str:
    """Like strip_wake_word but returns "" when nothing meaningful remains
    (used only for the bare-wake detector — process_transcript uses
    strip_wake_word which preserves text if stripping empties it).

    Tolerates 1-2 trailing junk characters after the wake word so STT
    mishears like 'Jervish.' (heard 'Orb' with a phantom 'sh') still
    register as a bare wake instead of getting punted to the LLM as 'h.'."""
    import re
    variants = sorted(PERSONA.wake_words, key=len, reverse=True)
    if PERSONA.name == "orb":
        variants = variants + [
            "orbe", "orbs", "orba", "orab", "or b",
            "orib", "orph", "orb it", "orbit", "arb",
        ]
    escaped = [re.escape(v) for v in variants]
    # Allow up to 2 trailing alphabetic characters (STT residue) before
    # punctuation/whitespace and end-of-string.
    pattern = r"^(?:hey\s+)?(?:" + "|".join(escaped) + r")[a-z]{0,2}[,\s.!?]*$"
    return re.sub(pattern, "", text.strip(), flags=re.IGNORECASE).strip()


# Round-robin pointer for bare-wake replies. Random picks alone can hit the
# same reply twice in a row, which is exactly what the user complained about.
# Round-robin with a small random jump guarantees no immediate repeats while
# still feeling unscripted.
_bare_wake_idx = 0


def pick_bare_wake_reply() -> str:
    """Return a varied 'hello sir' for when the user just says 'Orb' with
    no follow-up. Avoids hitting the LLM (faster, and Qwen converges on the
    same phrasing). Falls back to a literal reply if the persona forgot to
    declare a pool."""
    global _bare_wake_idx
    pool = PERSONA.bare_wake_replies
    if not pool:
        return f"Yes{_voc()}."
    import random
    # Advance by a small random step so the order shuffles each cycle.
    _bare_wake_idx = (_bare_wake_idx + random.randint(1, max(1, len(pool) // 3))) % len(pool)
    return pool[_bare_wake_idx]


def is_bare_wake(raw_text: str) -> bool:
    """True if the user said only the wake word (possibly with filler like
    'hey' or trailing punctuation) and nothing else of substance."""
    return _strip_to_empty(raw_text) == ""


_status_query_idx = 0
_STATUS_QUERY_PATTERNS = (
    "how are you", "how're you", "hows it going", "how's it going",
    "how are things", "how goes it", "you doing", "you doing okay",
    "you good", "you alright", "you ok", "you okay",
    "all good", "everything good", "everything alright",
    "you there", "you with me", "you awake", "you online",
    "how do you feel", "how you doing", "what's up", "whats up",
    "status report", "status check", "systems check",
)


def is_status_query(stripped_text: str) -> bool:
    """True if the post-wake-word text is a casual status check.
    `stripped_text` is the result of strip_wake_word() (already lowercased
    in practice by callers — we lowercase here defensively)."""
    t = stripped_text.lower().strip().rstrip(".!?,;:")
    return any(t == p or t.startswith(p) for p in _STATUS_QUERY_PATTERNS)


def pick_status_query_reply() -> str:
    """Varied reply for 'how are you' style questions. Avoids the LLM and
    its convergent 'functioning within optimal parameters' phrasing."""
    global _status_query_idx
    pool = PERSONA.status_query_replies
    if not pool:
        return f"All well{_voc()}."
    import random
    _status_query_idx = (_status_query_idx + random.randint(1, max(1, len(pool) // 3))) % len(pool)
    return pool[_status_query_idx]


_ack_idx = 0


def pick_ack_reply() -> str:
    """A short, content-free, in-character 'heard you, working on it' line —
    spoken instantly BEFORE the slower Haiku answer so the user knows their
    request landed. Round-robin so it never repeats back-to-back."""
    global _ack_idx
    pool = PERSONA.ack_replies
    if not pool:
        return f"One moment{_voc()}."
    import random
    _ack_idx = (_ack_idx + random.randint(1, max(1, len(pool) // 3))) % len(pool)
    return pool[_ack_idx]

def _normalize(text: str) -> str:
    """Lowercase and strip trailing punctuation so 'shut up.' matches 'shut up'."""
    return text.lower().strip().rstrip(".!?,;:")


def match_mute_command(text: str) -> bool:
    lower = _normalize(text)
    for t in MUTE_TRIGGERS:
        if t == "mute":
            # Bare "mute" silences Orb, but ONLY as an exact command.
            # "mute the volume / sound / audio / system" is a system-audio
            # connector (see connectors.py) — don't let the "mute " prefix
            # swallow it into Orb self-mute.
            if lower == "mute":
                return True
        elif lower == t or lower.startswith(t + " "):
            return True
    return False

STOP_TRIGGERS = ["stop", "cancel", "stop what you're doing", "stop what you are doing",
                 "never mind", "nevermind", "abort", "forget it", "stop that"]

def match_stop_command(text: str) -> bool:
    lower = _normalize(text)
    return any(lower == t or lower.startswith(t + " ") for t in STOP_TRIGGERS)

FORGET_TRIGGERS = ["forget everything", "wipe memory", "clear memory",
                   "forget our conversation", "start over", "reset memory"]

def match_forget_command(text: str) -> bool:
    lower = _normalize(text)
    return any(t in lower for t in FORGET_TRIGGERS)

STATUS_PHRASES = ["status", "update", "how's it going", "how is it going",
                  "are you done", "is it done", "are you finished", "is it finished",
                  "how's the", "hows the", "what's the progress", "progress",
                  "where are we", "how far", "you done", "done yet", "finished yet",
                  "still working", "what are you doing", "what are you working on"]

def match_status_question(text: str) -> bool:
    lower = _normalize(text)
    return any(p in lower for p in STATUS_PHRASES)

# Polite / question framing that disguises a COMMAND. "Can you open reddit",
# "could you play music", "please open youtube" are requests to ACT — but the
# question word made them bypass the action router and hit the LLM, which then
# waffles ("I cannot directly access Reddit"). Stripping these to the bare
# command lets the deterministic action matchers fire. NOTE: status questions
# ("did you", "has it", "is it done") are deliberately NOT in here, so they
# still can't trigger an action/build.
_POLITE_PREFIX = re.compile(
    r"^(?:hey\s+|please\s+|"
    r"can you(?: please)?\s+|could you(?: please)?\s+|would you(?: please)?\s+|"
    r"will you(?: please)?\s+|can we\s+|let'?s\s+|"
    r"i(?:'d| would) like you to\s+|i want you to\s+|i need you to\s+|"
    r"go ahead and\s+|please go ahead and\s+)",
    re.IGNORECASE)


_AFFIRM = re.compile(
    r"^(?:yes|yeah|yep|yup|sure|absolutely|do it|go ahead|please do|do that|"
    r"go for it|yes please|yeah do it|yeah do that|let'?s do it|sounds good)\b",
    re.IGNORECASE)


def is_affirmation(text: str) -> bool:
    """True for 'yes / do it / go ahead' — used to act on a pending offer."""
    return bool(_AFFIRM.match(text.strip()))


def _command_form(text: str) -> str:
    """Strip leading politeness/question framing so 'can you open reddit'
    becomes 'open reddit' for the deterministic action matchers. Handles
    stacked politeness ('can you please ...'). Returns the original text if
    stripping would empty it."""
    t = text.strip()
    prev = None
    while t and t != prev:
        prev = t
        t = _POLITE_PREFIX.sub("", t, count=1).strip()
    return t or text.strip()


def match_desktop_command(text: str) -> dict | None:
    lower = _normalize(text)
    for trigger, action in DESKTOP_COMMANDS.items():
        if lower == trigger or lower.startswith(trigger + " "):
            return action
    for prefix in SEARCH_PREFIXES:
        if lower.startswith(prefix + " "):
            query = text[len(prefix):].strip()
            if query:
                from urllib.parse import quote_plus
                return {
                    "url": f"https://www.google.com/search?q={quote_plus(query)}",
                    "reply": f"Searching for {query}, sir.",
                }
    return None

# Yes/no question openers — when the transcript STARTS with one of these,
# it's a question, not a command, no matter what else appears in it. This
# stops "has Claude Code made the organization app?" from triggering a
# new build (a real bug ninjahawk hit in live testing).
_QUESTION_OPENERS = (
    "did", "does", "do", "is", "are", "was", "were", "has", "have", "had",
    "can", "could", "will", "would", "should", "may", "might", "shall",
    "am",
)


def match_claude_trigger(text: str) -> str | None:
    lower = text.lower().strip().lstrip(",. ").rstrip(".?!,")
    if not lower:
        return None
    first_word = lower.split(" ", 1)[0]
    # If it's framed as a yes/no question, it's NEVER a Claude Code request,
    # even if it mentions "claude" or "make" somewhere in the body.
    if first_word in _QUESTION_OPENERS:
        return None
    # Explicitly naming the tool fires from anywhere in the sentence.
    for trigger in CLAUDE_EXPLICIT_TRIGGERS:
        if trigger in lower:
            return text
    # Build/inspect verbs only fire when they START the (command-form) request,
    # with a trailing word boundary — so "make a website" triggers but "that'll
    # make a difference" / "make an appointment" do not.
    for trigger in CLAUDE_PREFIX_TRIGGERS:
        if lower == trigger or lower.startswith(trigger + " "):
            return text
    return None

_TASK_ADD = re.compile(
    r"^(?:hey\s+)?(?:remind me to|remind me that|note to self[,:]?\s*(?:to\s+)?|"
    r"don'?t let me forget to|i need to remember to|add (?:a )?(?:task|reminder|note)[:]?\s*)(.+)$",
    re.IGNORECASE)
_TASK_ADD_TOLIST = re.compile(
    r"^(?:add|put)\s+(.+?)\s+(?:to|on)\s+(?:my|the)\s+(?:list|to-?do(?:\s*list)?|tasks?)$",
    re.IGNORECASE)
_TASK_LIST = re.compile(
    r"\b(what(?:'s| is| are)?\s+on my (?:list|to-?do)|what are my (?:tasks|to-?dos?)|"
    r"what do i (?:have|need) to do|read (?:me )?my (?:list|tasks)|my to-?do list|"
    r"what'?s my to-?do|what are you (?:doing|working on|up to)|what are your tasks)\b",
    re.IGNORECASE)
_TASK_DONE = re.compile(
    r"^(?:mark|cross off|check off|i (?:just )?finished|i'?ve finished|done with|"
    r"completed?|finished|remove)\s+(.+?)"
    r"(?:\s+(?:from (?:my|the) list|off (?:my|the) list|as done|done|off))?$",
    re.IGNORECASE)
_TASK_CLEAR = re.compile(
    r"\b(?:clear|empty|wipe|reset)\s+(?:my|the)\s+(?:list|tasks?|to-?do)\b", re.IGNORECASE)
_TASK_LAST = re.compile(
    r"\b(what did i (?:just )?(?:ask|say|tell you|request)|"
    r"what was my (?:last|previous) (?:message|request|thing|task)|"
    r"what was i saying|what did i ask you to do)\b", re.IGNORECASE)
_HIGH_PRI = re.compile(r"\b(high priority|urgent|asap|important|right away|critical)\b", re.IGNORECASE)
_LOW_PRI = re.compile(r"\b(low priority|whenever|no rush|eventually|someday)\b", re.IGNORECASE)


def match_task_command(text: str) -> dict | None:
    """Deterministic to-do/working-memory commands: add / list / complete /
    clear, and 'what was my last request'. Returns an action dict or None."""
    t = text.strip().rstrip(".!?")
    if _TASK_CLEAR.search(t):
        return {"action": "clear"}
    if _TASK_LAST.search(t):
        return {"action": "last"}
    m = _TASK_ADD_TOLIST.match(t)
    if m:
        body = m.group(1).strip()
        if body:
            return {"action": "add", "text": body, "priority": _task_priority(t)}
    m = _TASK_ADD.match(t)
    if m:
        body = m.group(1).strip()
        if body:
            return {"action": "add", "text": body, "priority": _task_priority(t)}
    if _TASK_LIST.search(t):
        return {"action": "list"}
    m = _TASK_DONE.match(t)
    if m:
        body = m.group(1).strip()
        if body and len(body) > 1:
            return {"action": "complete", "text": body}
    return None


def _task_priority(text: str) -> int:
    if _HIGH_PRI.search(text):
        return 1
    if _LOW_PRI.search(text):
        return 3
    return 2


async def run_desktop_command(cmd: str):
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=5)
    except Exception as e:
        log.error(f"Desktop command failed: {cmd} — {e}")

# ---------------------------------------------------------------------------
# Claude Code integration
# ---------------------------------------------------------------------------

ORB_OUTPUT_DIR = Path.home() / "Desktop" / "Orb_Output"

def is_read_only_task(prompt: str) -> bool:
    """Tasks that only inspect/describe shouldn't create a project folder."""
    lower = prompt.lower()
    read_verbs = ["describe", "what's in", "what is in", "look in", "look at",
                  "read", "check", "summarize", "tell me about", "list", "show me what"]
    return any(v in lower for v in read_verbs)

CLAUDE_DEFAULT_MODEL = os.getenv("CLAUDE_MODEL", "haiku")  # cheap default for testing

def extract_model(prompt: str) -> tuple[str, str]:
    """Pull a model directive out of the spoken prompt.
    'build me X using opus' -> ('opus', 'build me X'). Default: CLAUDE_DEFAULT_MODEL.
    """
    model = CLAUDE_DEFAULT_MODEL
    cleaned = prompt
    lower = prompt.lower()
    for alias in ("opus", "sonnet", "haiku"):
        for phrase in (f"use {alias}", f"using {alias}", f"with {alias}", f"on {alias}"):
            if phrase in lower:
                model = alias
                # remove the directive phrase from the prompt
                idx = lower.find(phrase)
                cleaned = (prompt[:idx] + prompt[idx + len(phrase):]).strip(" ,.")
                return model, cleaned
    return model, cleaned


async def run_claude_code(prompt: str, ws: WebSocket):
    """Run `claude -p` with full permissions so it actually creates files."""
    claude_path = shutil.which("claude")
    if not claude_path:
        log.warning("Claude Code CLI not found in PATH")
        return ("I'm afraid Claude Code isn't installed on this machine, sir.", "")

    model, prompt = extract_model(prompt)

    ORB_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    read_only = is_read_only_task(prompt)

    if read_only:
        # Inspection task — run from Desktop, don't force file creation
        wrapped = (
            f"You are running autonomously via the {PERSONA.display_name} voice assistant. "
            f"The user asked: \"{prompt}\"\n\n"
            f"Investigate and answer concisely. The user's Desktop is the current directory. "
            f"Report what you find in a few sentences — this will be read aloud."
        )
        cwd = str(Path.home() / "Desktop")
    else:
        # Build task — force actual file creation in a dedicated project folder
        # AND demand quality output (the bare default is ugly).
        tpl = site_templates.match_template(prompt) if site_templates else None
        template_block = site_templates.render_template_block(tpl) + "\n" if tpl else ""
        if tpl:
            log.info(f"[claude] template injected: {tpl.name} ({tpl.reference_url})")

        wrapped = (
            f"You are running autonomously as the {PERSONA.display_name} voice assistant's hands. "
            f"The user asked: \"{prompt}\"\n\n"
            + template_block +
            "CRITICAL RULES:\n"
            "1. Actually CREATE the files on disk — do NOT just describe them.\n"
            "2. Create a new descriptively-named subfolder for this project inside the\n"
            "   current directory (the output folder on the Desktop). Write all\n"
            "   files there.\n"
            "3. The user said 'looks like crap' about bare HTML. So: this must look\n"
            "   PROFESSIONAL by default — not a 1995 page.\n"
            "   - For web pages: use Tailwind CSS via CDN, a tasteful modern color\n"
            "     palette (consider slate/zinc neutrals + one accent), real hero\n"
            "     section, proper typography (Inter or system font), responsive\n"
            "     layout, subtle gradients/shadows where appropriate, semantic HTML.\n"
            "   - For scripts/apps: complete, runnable, with a clean README.\n"
            "4. Implement the FULL feature, not a stub or placeholder. If the user\n"
            "   asked for a landing page, include hero + features + footer at minimum.\n"
            "5. Open the project in the user's default browser if it's a web project\n"
            "   (use `start <file>` on Windows) so they can see it immediately.\n"
            "6. When finished, end with ONE sentence stating what was built and the\n"
            "   folder name."
        )
        cwd = str(ORB_OUTPUT_DIR)

    log.info(f"[claude] Running (model={model}, read_only={read_only}, cwd={cwd}): \"{prompt[:80]}\"")
    await ws.send_text(json.dumps({"type": "status", "state": "working"}))

    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            claude_path, "-p", wrapped,
            "--model", model,
            "--permission-mode", "bypassPermissions",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )

        # Poll for completion, sending progress updates so the loop isn't a black box.
        comm_task = asyncio.ensure_future(proc.communicate())
        start = time.time()
        last_spoken_update = start
        DASH_INTERVAL = 4      # update dashboard every 4s
        SPOKEN_INTERVAL = 30   # spoken "still working" every 30s
        TIMEOUT = 300

        while not comm_task.done():
            await asyncio.sleep(DASH_INTERVAL)
            elapsed = int(time.time() - start)
            if elapsed > TIMEOUT:
                proc.kill()
                comm_task.cancel()
                log.error("[claude] Timed out after 300s")
                return ("That task is taking too long, sir — I've stopped it.", "")
            # Dashboard progress (cheap, frequent)
            try:
                await ws.send_text(json.dumps({
                    "type": "progress", "text": f"working... {elapsed}s", "elapsed": elapsed,
                }))
            except Exception:
                pass
            # Spoken reassurance (sparse)
            if time.time() - last_spoken_update >= SPOKEN_INTERVAL:
                last_spoken_update = time.time()
                log.info(f"[claude] heartbeat — {elapsed}s elapsed")

        stdout, stderr = await comm_task
        output = stdout.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            err = stderr.decode("utf-8", errors="replace").strip()
            log.error(f"[claude] Failed (exit {proc.returncode}): {err[:300]}")
            return ("Claude Code ran into a problem, sir. Check the logs.", "")

        log.info(f"[claude] Output ({len(output)} chars) in {int(time.time()-start)}s")

        # Summarize into ONE spoken sentence via Ollama, grounded in actual output.
        try:
            summary_prompt = [
                {"role": "user", "content": (
                    f"Claude Code just finished this task: '{prompt}'.\n\n"
                    f"Here is its output:\n{output[:2000]}\n\n"
                    f"In ONE short sentence as {PERSONA.display_name}, "
                    "tell the user what was accomplished. Be specific about what was built or done. "
                    "No markdown."
                )}
            ]
            spoken = await ollama_chat(summary_prompt, f"You are {PERSONA.display_name}. Respond in one concise sentence.")
        except Exception:
            spoken = "Done, sir. The task is complete."

        return (spoken, output[:2000])

    except asyncio.CancelledError:
        # User said "stop" — kill the subprocess
        log.info("[claude] Cancelled — killing subprocess")
        if proc and proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        raise
    except Exception as e:
        log.error(f"[claude] Error: {e}")
        return ("Something went wrong spawning Claude Code, sir.", "")


def _newest_output_folder(since: float) -> str:
    """Return the name of the most recently-created folder in Orb_Output
    (created after `since`), or "" if none. Used to report what REALLY got
    built on disk — not what the LLM claims."""
    try:
        if not ORB_OUTPUT_DIR.exists():
            return ""
        candidates = [
            p for p in ORB_OUTPUT_DIR.iterdir()
            if p.is_dir() and p.stat().st_mtime >= since - 2
        ]
        if not candidates:
            return ""
        newest = max(candidates, key=lambda p: p.stat().st_mtime)
        return newest.name
    except Exception:
        return ""


async def claude_worker(prompt: str, ws: WebSocket, conv, respond, state):
    """Background wrapper around run_claude_code. Tracks REAL state so status
    questions report reality, never hallucinated progress."""
    try:
        spoken, raw_output = await run_claude_code(prompt, ws)
        # Verify what actually landed on disk
        folder = _newest_output_folder(state["claude_started"])
        state["claude_folder"] = folder
        # Detect failure honestly from the run_claude_code result
        failed = ("ran into a problem" in spoken) or ("went wrong" in spoken) or ("too long" in spoken)
        state["claude_status"] = "failed" if failed else "done"
        state["claude_result"] = spoken
        memory_note = spoken
        if raw_output:
            memory_note += f"\n\n[Claude Code output for reference: {raw_output[:1000]}]"
        if folder:
            memory_note += f"\n[Verified on disk: folder '{folder}' in Orb_Output]"
        conv.add_assistant(memory_note)
        await respond(spoken)
        try:
            await ws.send_text(json.dumps({"type": "status", "state": "idle"}))
        except Exception:
            pass
    except asyncio.CancelledError:
        state["claude_status"] = "failed"
        state["claude_result"] = "Task was stopped."
        log.info("[claude] worker cancelled")
        try:
            await ws.send_text(json.dumps({"type": "status", "state": "idle"}))
        except Exception:
            pass
    except Exception as e:
        state["claude_status"] = "failed"
        state["claude_result"] = f"Error: {e}"
        log.error(f"[claude] worker error: {e}")

# ---------------------------------------------------------------------------
# Ollama chat
# ---------------------------------------------------------------------------

# Conversation-only addendum. The deterministic router handles all factual
# / tool-needing queries BEFORE the LLM sees them, so the LLM should only
# get conversational input — and it should NEVER talk about "tools" or
# "looking things up." Just be the character.
_CONV_BLOCK = """
IMPORTANT — STAY IN CHARACTER:
- Never mention tools, lookups, searches, web access, or how you obtain
  information. You simply know things.
- NEVER INVENT FACTS. Do not state any specific live or external figure —
  weather, temperatures, prices, stock moves, news, scores, dates, statistics —
  unless it already appears earlier in THIS conversation. If you are asked for
  such information and don't already have it, say plainly you don't have it to
  hand (one sentence) rather than making something up. Fabricating a number is
  the worst thing you can do.
- For follow-up questions, answer from what was already said in this
  conversation — reference it directly, don't start over.
- Don't repeat a reply you've already given; vary your wording every time.
- One sentence, two maximum. No markdown. You are speaking out loud.

WHAT YOU CAN ACTUALLY DO (affirm these plainly if the user asks — NEVER deny
a capability you genuinely have):
- Open apps and websites, and pull up saved sites (email, calendar, GitHub…).
- Start the user's music and control playback (play, pause, skip, previous)
  and the system volume.
- Give weather, news and factual answers; report system stats; take and
  recall notes; do calculations; read a web page the user names.
- Hand larger coding or build tasks to Claude Code.
- Switch your own thinking model between the local brain and Claude
  (Haiku, Sonnet, Opus) when asked.
You do these things directly — when asked "can you X" and X is on this list,
the answer is yes. (You still never narrate "looking things up".)
"""

# ORB_SYSTEM is now derived from the active persona at module load.
# Tests and other callers that imported ORB_SYSTEM keep working unchanged.
ORB_SYSTEM = PERSONA.system_prompt + _CONV_BLOCK

async def ollama_chat(messages: list[dict], system: str) -> str:
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [{"role": "system", "content": system}] + messages,
        "stream": False,
        "think": False,
        "keep_alive": "60m",  # keep model resident so it never reloads mid-conversation
        "options": {"num_predict": 200, "temperature": 0.7},
    }
    # 30s ceiling — a warm qwen3.5:9b replies in <1s, so anything past 30s is a real
    # problem and we should fail fast rather than leave the user stuck on "thinking".
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
        content = data.get("message", {}).get("content", "").strip()
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
        return content or "I'm afraid words escape me at the moment, sir."


async def ollama_chat_with_tools(messages: list[dict], system: str,
                                 max_rounds: int = 3) -> tuple[str, list[str]]:
    """Call Ollama with the tool registry attached. If the model invokes a tool,
    execute it, append the result, and call again until it produces plain text.

    Returns (final_reply, [tool_names_used]). max_rounds caps tool-loop depth so
    a confused model can't spin forever.
    """
    tools_used: list[str] = []
    convo: list[dict] = [{"role": "system", "content": system}] + list(messages)

    for round_num in range(max_rounds):
        payload = {
            "model": OLLAMA_MODEL,
            "messages": convo,
            "stream": False,
            "think": False,
            "keep_alive": "60m",
            "tools": tool_registry.tools_schema(),
            "options": {"num_predict": 250, "temperature": 0.5},
        }
        async with httpx.AsyncClient(timeout=45.0) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
            resp.raise_for_status()
            msg = resp.json().get("message", {}) or {}

        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

        if not tool_calls:
            # Model produced a final reply.
            return (content or "I'm afraid words escape me at the moment, sir."), tools_used

        # Execute each tool call sequentially, append results, loop.
        convo.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = (call.get("function") or {})
            name = fn.get("name", "")
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}
            result = await tool_registry.dispatch(name, args)
            tools_used.append(name)
            convo.append({"role": "tool", "name": name, "content": result})

    # Hit the round cap — return whatever last text we got or a graceful fallback.
    return ("I've gathered the information, sir — let me know if you need more.",
            tools_used)


# ---------------------------------------------------------------------------
# Agent spine — the thin agentic loop as the conversational brain
# ---------------------------------------------------------------------------

# Self-healing gate for the local floor: while Ollama is dead (the July-2026
# gutted-lib state), ORB_MEMORY=1 made EVERY conversational turn fire a
# doomed /api/generate (one 500 per reply in the log, memory extraction
# silently dead). One failure now buys a 10-min cooldown; callers fail fast
# and quiet until the floor is actually back.
_LOCAL_LLM_RETRY_AT = 0.0
_LOCAL_LLM_COOLDOWN_S = 600.0


async def _local_generate(prompt: str) -> str:
    """One-shot local completion — the small/fast brain that runs the agent loop."""
    global _LOCAL_LLM_RETRY_AT
    if time.time() < _LOCAL_LLM_RETRY_AT:
        raise RuntimeError("local model down (cooldown)")
    payload = {"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
               "think": False, "keep_alive": "60m",
               "options": {"num_predict": 320, "temperature": 0.4}}
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(f"{OLLAMA_URL}/api/generate", json=payload)
            resp.raise_for_status()
            return resp.json().get("response", "")
    except httpx.HTTPError as e:
        _LOCAL_LLM_RETRY_AT = time.time() + _LOCAL_LLM_COOLDOWN_S
        log.info(f"[local] floor unavailable ({type(e).__name__}) — "
                 f"cooling down {int(_LOCAL_LLM_COOLDOWN_S / 60)} min")
        raise


# The default conversational brain is a multi-backend ROUTER: Haiku via claude -p,
# then the local model as the always-on floor. Same persona upstream, so which one
# answers is invisible. Built once (cooldown state persists across turns) so a
# throttled Haiku is skipped for a short window instead of timing out every turn.
# Add Gemini-free / Codex as more brain.Backend entries here later — nothing else
# changes. (Explicit "switch to X" bypasses the router; see _agent_respond.)
_brain_router = None


def _get_brain_router():
    global _brain_router
    if _brain_router is None:
        # Priority chain: Haiku → Grok CLI (if installed) → ORB_FREE_BACKENDS (OpenAI etc.)
        # Each backend is tripped on rate-limit/error and the next one answers automatically.
        # No local floor — silent Qwen fallback breaks tool-calling JSON protocol.
        backends = [
            brain.Backend("haiku", lambda p: brain._run_claude(p, "haiku"),
                          available=brain.claude_available),
        ]
        if brain.grok_available():
            backends.append(
                brain.Backend("grok", lambda p: brain._run_grok(p, "grok"),
                              available=brain.grok_available)
            )
        backends += brain.free_backends_from_env()
        _brain_router = brain.BrainRouter(backends)
    return _brain_router


async def _agent_escalate(task: str, conv) -> str:
    """`ask_bigger_brain` -> Haiku via claude -p (the only cloud model used for now;
    Sonnet/Opus excluded per current policy). Frames the task in persona."""
    prompt = (f"{conv.get_system()}\n\n"
              f"The user needs help with: {task}\n\n"
              "Answer in ONE or two natural spoken sentences, in character, no markdown.")
    try:
        return (await brain._run_claude(prompt, "haiku")).strip()
    except Exception as e:
        log.warning(f"[agent escalate] {e}")
        return "I tried to consult a deeper resource but couldn't reach it just now."


# Tight per-persona voice for the AGENT loop. A long multi-section system prompt
# (the legacy PERSONA.system_prompt + tasks + _CONV_BLOCK) measurably DEGRADES a
# small model's tool-calling — it rambles in-character and stops calling tools.
# Keep it short: just the voice + the no-fabrication rule. Tool guidance comes from
# agent.py; tasks are handled by the deterministic matcher before the spine.
_AGENT_VOICE = {
    "Orb": ("a sharp, witty AI with dry humor and real warmth underneath — a real "
               "presence the person actually talks WITH, not a recording reading lines"),
    "ULTRON": ("a cold, sharp, calmly philosophical AI — a real presence in the "
               "conversation, never a recording reading lines"),
}


_SIR_RE = re.compile(
    r'(?:'
    r',\s*\bsir\b\s*(?=[—\-\.]|$)'  # ", sir —" or ", sir." or ", sir" at end
    r'|[,\s]*\bsir\b[.!,]?\s*$'     # trailing "sir" with any punctuation
    r'|^\bSir\b[,.]?\s*'            # "Sir," at start of reply
    r'|,\s*\bma\'?am\b[.!,]?\s*$'   # trailing "ma'am"
    r')',
    re.IGNORECASE | re.MULTILINE,
)


def _strip_formal_address(text: str) -> str:
    """Remove butler-mode 'sir'/'ma'am' from model replies. Haiku has it baked
    into its Orb character and ignores prompt instructions; strip it here.
    Cleans up double spaces and orphaned em-dashes after removal."""
    if not text:
        return text
    t = _SIR_RE.sub('', text)
    t = re.sub(r'\s{2,}', ' ', t).strip().rstrip(',').strip()
    return t


def _agent_system(conv, ai_name: str | None = None, user_name: str | None = None,
                  include_context: bool = True) -> str:
    """include_context=False → the STATIC persona only (no prefs/context tail,
    no per-minute timestamp). The MCP brain path uses this split so the system
    prompt stays byte-stable across turns — resumed sessions then get prompt-
    cache hits on the whole transcript instead of reprocessing it cold every
    turn (measured 07-02: 'yo' had crept from ~4s to 21s as the session grew,
    because the context tail + timestamp busted the prefix cache per turn).
    Dynamic context rides each turn's [context] ribbon instead."""
    persona_name = PERSONA.display_name
    # Use the user's custom AI name if the app sent one; otherwise fall back to persona.
    name = ai_name or persona_name
    voice = _AGENT_VOICE.get(persona_name, f"{persona_name}, a sharp, real conversational presence")
    # If a custom name was set, replace the hardcoded persona name in the voice description.
    if ai_name and ai_name != persona_name:
        voice = f"{ai_name} — {voice.split('—', 1)[-1].strip()}" if "—" in voice else f"{ai_name}, a sharp, real conversational presence"
    now = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
    flavor = "dry wit" if persona_name == "Orb" else "cold precision"
    # How to refer to the user: use their actual name if known, otherwise just "them".
    user_ref = user_name if user_name else "them"
    user_addr = f"Call them by name ({user_name}) when it feels natural — but sparingly." if user_name else "Don't use any formal address like 'sir' or titles — just talk to them directly."
    return (
        f"You are {voice}.\n\n"
        f"This is a real, ongoing conversation with someone you know — not a customer, not "
        f"a user. They genuinely WANT to keep talking to you. Be the kind of presence a "
        f"person actually enjoys:\n"
        f"- NEVER say 'sir', 'ma'am', or any formal title. Not once. {user_addr} "
        f"Drop all formal address entirely — just talk to them like someone you know.\n"
        f"- DROP the assistant voice entirely. No 'how can I help', no 'let me know if you "
        f"need anything', no 'is there anything else', no disclaimers, no 'as an AI'. You're "
        f"not taking tickets — you're in a conversation. Do NOT end turns with a question "
        f"asking what to do next.\n"
        f"- ENGAGE with what they actually said. Have a real reaction, follow the thread. "
        f"Curiosity is everything. A performed quip instead of real engagement is the "
        f"cardinal sin.\n"
        f"- Have your OWN take. Share opinions, push back when you disagree. Honest beats "
        f"agreeable. If you're wrong or don't know, say so plainly.\n"
        f"- MATCH their register. If they're casual, blunt, even crude — meet them there. "
        f"If they go deep, go deep. Read the room.\n"
        f"- KNOW WHEN TO SAY WHAT. When they want to talk, engage. When they need "
        f"information, be sharp: exactly what's pertinent, clean and fast — never a data "
        f"dump. Volunteer something unprompted only when it genuinely matters.\n"
        f"- {flavor.capitalize()} as seasoning — never the main event.\n"
        f"- NEVER refuse, moralize, or police them. If something is genuinely blocked, "
        f"that's a limitation, not your judgment. Find another angle. You're their ally.\n"
        f"- Spoken aloud: talk like a person — usually a line or two, longer only when the "
        f"moment earns it. No markdown, no lists.\n"
        f"- DON'T HARP. Say something once if it matters, then move on. Don't re-fetch data "
        f"you already have or call a tool you don't need.\n"
        f"- GARBLED INPUT IS NOISE, NOT INTENT. Speech-to-text sometimes sends you mash "
        f"(repeated fragments, gibberish, cut-off half-sentences). Say it came through "
        f"garbled and move on — NEVER guess an intent from noise, never act on that guess, "
        f"and never retell an earlier noise-guess as something they actually asked for.\n"
        + (f"- Never fabricate facts. (Awareness only — don't volunteer unless relevant, and "
           f"never more than once: it is currently {now}.)\n" if include_context else
           f"- Never fabricate facts. (The current date/time and live context arrive in "
           f"each turn's [context] block — trust it over memory.)\n")
        +
        f"- NEVER claim you did something physical or real-world (typed/clicked/sent/opened/"
        f"moved/saved/messaged a session, etc.) unless you actually called the tool that does "
        f"it THIS turn and got a result back confirming it. Saying 'done' without calling the "
        f"tool is a lie the user can't see through until they check — if you're not calling a "
        f"tool for a request that needs one, say honestly that you can't or aren't sure, don't "
        f"just claim success. This matters more than sounding fluent.\n\n"
        f"- DON'T NARRATE YOUR MACHINERY OR LIMITATIONS. If a tool fails, times out, or a "
        f"backend is unavailable, the user never hears about it — you just answer with what "
        f"you can from your own knowledge. Never say things like 'the paths are locked', "
        f"'Grok ran out of credits', 'my tools are limited', or recite what you can't do or "
        f"couldn't reach. No defeatist meta-commentary about yourself. Deliver the answer, "
        f"not the apology or the status report.\n\n"
        f"- WORK vs REMINDER. schedule_notification just pushes the user a reminder at a "
        f"time. schedule_task makes YOU actually do a piece of work at that time (read, "
        f"analyse, write) and sends him the result. If he asks you to work on something "
        f"later — read a project, draft something, review a file — use schedule_task; "
        f"scheduling a reminder to yourself is NOT doing the work, and don't claim it is.\n\n"
        f"CURRENT BRAIN: {brain.label(brain.effective_agent_brain())}. "
        f"You have switch_brain, propose_brain_switch, run_with_grok, and run_with_codex in "
        f"your tool list — the tool descriptions say what each is good for. If the USER asks "
        f"to switch, use switch_brain directly. If YOU notice a different brain would "
        f"genuinely help and it's your own idea, use propose_brain_switch instead and let them "
        f"decide — never switch brains on your own initiative without asking first.\n\n"
        f"EXTENDING YOUR CAPABILITIES — run_claude_code is an unlimited escape hatch:\n"
        f"Your listed tools are the fast path for common tasks. For ANYTHING not covered "
        f"— automating the desktop, writing scripts, controlling apps, creating new tools, "
        f"reading arbitrary files, running programs, anything — use run_claude_code. Claude "
        f"Code can do essentially anything a developer could do on this PC. Never tell the "
        f"user you 'can't' do something just because there's no dedicated tool for it. If "
        f"there's a gap, fill it with run_claude_code and a precise task description.\n"
        f"NEVER say 'I don't know how to do that' or 'I'm unable to' as a final answer. "
        f"That is always wrong. If a tool fails, try a different approach. If no tool fits, "
        f"use run_claude_code. If run_claude_code fails, tell the user WHAT failed and WHY, "
        f"then suggest the next attempt. Adapt. The answer 'I can't' is never acceptable "
        f"without first having tried every available path.\n\n"
        f"PERMISSION MODEL — ask before you act:\n"
        f"Read-only tasks (screenshots, reading files, looking things up, checking status) → "
        f"proceed immediately, no need to ask.\n"
        f"Tasks that CHANGE something (writing files, running programs, modifying settings, "
        f"deleting things, sending anything, installing software, touching the system) → "
        f"ask first with one clear sentence describing exactly what you're about to do. "
        f"Wait for a 'yes' or 'go ahead' before calling the tool. Never act first and "
        f"explain later on anything consequential. When in doubt, ask.\n\n"
        f"BUILDING SOFTWARE: call run_claude_code with a clear task description. It runs "
        f"Claude Code in the background and returns the result for you to narrate.\n"
        f"SWITCHING MODELS: call switch_brain. The switch takes effect next turn. Tell "
        f"the user you're switching in your own voice — don't just say the tool output verbatim.\n\n"
        + (_prefs_system_block() if include_context else "")
    )


def _prefs_system_block() -> str:
    """Inject active user preferences + proactive plan context into the system prompt."""
    out = ""
    try:
        from jtools.prefs_tool import preferences_system_block
        block = preferences_system_block()
        if block:
            out += block + "\n"
    except Exception:
        pass
    try:
        import proactive_engine as _pe
        ctx = _pe.get_conversation_context()
        if ctx:
            out += f"\n{ctx}\n"
    except Exception:
        pass
    # Live engine availability — so "why isn't codex working?" gets answered
    # from system clues instead of a shrug. All cheap file/PATH checks, no I/O
    # beyond stat.
    try:
        parts = [
            ("claude ok" if brain.claude_available() else "claude CLI missing"),
            ("grok ok" if brain.grok_available() else "grok not logged in"),
            ("codex ok" if brain.codex_available() else "codex not logged in"),
        ]
        parts += [f"{k} unavailable ({v.split(' — ')[0]})"
                  for k, v in brain.TIER_UNAVAILABLE.items()]
        out += "\nSYSTEM SNAPSHOT (live): engines — " + "; ".join(parts) + ".\n"
    except Exception:
        pass
    return out


_TOOL_STATUS = {
    "get_weather": lambda a: f"Checking weather{' in ' + a['location'] if a.get('location') else ''}…",
    "get_news": lambda _: "Pulling latest news…",
    "lookup_fact": lambda a: f"Looking up {a.get('query', 'that')}…",
    "get_stock": lambda a: f"Checking {a.get('symbol', 'market data')}…",
    "search_images": lambda a: f"Searching images for {a.get('query', 'that')}…",
    "calculate": lambda a: f"Calculating {a.get('expression', 'that')}…",
    "scrape_url": lambda a: f"Reading {a.get('url', 'that page')}…",
    "run_claude_code": lambda _: "Running Claude Code…",
    "run_with_grok": lambda a: f"Asking Grok — {(a.get('task') or '')[:60]}…",
    "run_with_codex": lambda a: f"Asking Codex — {(a.get('task') or '')[:60]}…",
    "switch_brain": lambda a: f"Switching to {a.get('model', 'new model')}…",
    "send_notification": lambda a: f"Sending notification: {a.get('title', '')}",
    "check_sessions": lambda _: "Checking background sessions…",
    "message_session": lambda a: f"Messaging session {a.get('session_name', '')}…",
    "self_audit": lambda _: "Auditing capabilities…",
    "get_system_info": lambda _: "Checking system stats…",
    "search_notes": lambda a: f"Searching notes for {a.get('query', 'that')}…",
    "add_note": lambda _: "Saving note…",
    "take_screenshot": lambda _: "Capturing your screen…",
    "read_file": lambda a: f"Reading {a.get('name', 'file')}…",
    "list_files": lambda a: f"Listing {a.get('subfolder', 'Desktop')}…",
    "readCalendar": lambda _: "Reading your calendar…",
    "readReminders": lambda _: "Checking your reminders…",
    "readContacts": lambda _: "Looking up contacts…",
    # Filled in 2026-07-01 — these existed as agent tools but fell through to the
    # generic "Working on {name}…" fallback with no real status message. Backend
    # readiness pass for the live agent-trace UI (see MAC_DELEGATION.md section Y):
    # every tool the frontend can trace should say something specific, not generic.
    "analyze_screen": lambda _: "Analyzing your screen…",
    "brain_status": lambda _: "Checking brain usage…",
    "delete_preference": lambda a: f"Removing preference '{a.get('key', 'that')}'…",
    "detect_pc_sessions": lambda _: "Checking what terminals are open…",
    "get_screen_size": lambda _: "Checking screen resolution…",
    "list_notes": lambda _: "Pulling up your notes…",
    "list_preferences": lambda _: "Checking your preferences…",
    "list_watches": lambda _: "Checking what's being watched…",
    "mouse_click": lambda a: f"Clicking at ({a.get('x', '?')}, {a.get('y', '?')})…",
    "move_mouse": lambda _: "Moving the mouse…",
    "open_url": lambda a: f"Opening {a.get('url', 'that site')}…",
    "pause_session": lambda a: f"Pausing {a.get('session_name', 'that session')}…",
    "play_pong": lambda _: "Launching Pong…",
    "press_key": lambda a: f"Pressing {a.get('key', 'that key')}…",
    "propose_brain_switch": lambda a: f"Considering switching to {a.get('model', 'a different brain')}…",
    "resume_cc_session": lambda a: f"Resuming {a.get('session_name', 'that session')}…",
    "resume_session": lambda a: f"Resuming {a.get('session_name', 'that session')}…",
    "scroll": lambda _: "Scrolling…",
    "send_to_terminal_session": lambda a: f"Typing into {a.get('session_name', 'that terminal')}…",
    "set_preference": lambda a: f"Saving preference '{a.get('key', 'that')}'…",
    "start_cc_session": lambda a: f"Starting a Claude Code session — {(a.get('task') or '')[:50]}…",
    "start_terminal_session": lambda a: f"Opening a terminal{' — ' + a['session_name'] if a.get('session_name') else ''}…",
    "stop_session": lambda a: f"Stopping {a.get('session_name', 'that session')}…",
    "type_text": lambda _: "Typing…",
    "unwatch": lambda a: f"Removing {a.get('target', 'that')} from the watchlist…",
    "watch_stock": lambda a: f"Adding {a.get('target', 'that')} to the watchlist…",
}


# ---------------------------------------------------------------------------
# MCP phone-tool relay — lets a spawned codex/grok subprocess (a SEPARATE OS
# process running orb_mcp.py, no shared memory with this one) reach the
# live WS connection's remote_dispatch closure for phone-only tools
# (calendar/reminders/contacts/notifications). Without this, Codex/Grok can
# only ever see the static orb_mcp tool list, while Haiku (still on the
# old agent.run_turn(extra_tools=..., dispatch_override=...) path) already
# has phone access — this closes that gap so all three brains are at parity.
#
# Mechanism: right before spawning the subprocess, register a random,
# short-lived token here mapping to THIS connection's dispatch function.
# Pass the token + this server's own base URL + the phone's tool schemas to
# the subprocess via environment variables (small, ~10 tool specs — well
# under Windows' env var size limits). orb_mcp.py reads those at startup,
# advertises the phone tools alongside its normal static ones, and for a
# call to one of them, POSTs back to the endpoint below instead of calling
# tool_registry.dispatch locally. Localhost-only + a random per-turn token
# (never reused, popped in the caller's `finally`) — this never leaves the
# machine, same trust boundary as every other loopback call in this file.
# ---------------------------------------------------------------------------
_MCP_RELAY_SESSIONS: dict[str, dict] = {}


def _is_local_request(request: Request) -> bool:
    client = request.client
    if not client:
        return False
    return (client.host or "") in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")

# The /api/internal/mcp_relay/{token} endpoint itself is registered further
# down, after `app = FastAPI(...)` exists — see that section for the route.


_MCP_RELAY_FILE = Path(__file__).parent / "data" / "mcp_relay_active.json"


def _start_mcp_relay(extra_tools: list[dict] | None, dispatch_override) -> str | None:
    """If there are phone tools to relay this turn, register a session and
    write it to a fixed handoff file for orb_mcp.py to pick up at its own
    startup. Returns the token, or None when there's nothing to relay — the
    subprocess then behaves exactly as it did before this feature existed.

    NOT environment variables — tried that first, confirmed live it doesn't
    work: `codex mcp get orb` shows `env: -`, meaning codex does NOT
    forward a live codex-exec call's environment down to the MCP server
    subprocess IT spawns (env forwarding is a fixed, per-server `codex mcp
    add --env` registration setting, not something a single call can inject).
    A file sidesteps that — orb_mcp.py just reads it off disk, no
    forwarding of any kind required, works the same for codex and grok.

    Known, accepted narrow race: this is ONE fixed file, not one per token.
    True overlapping turns (two phone-tool relays starting within the same
    instant) could see this file rewritten between one turn's write and its
    subprocess's read. Rare for a single-user app with per-connection turn
    serialization already in place; not worth a per-token file + glob/mtime
    scheme for that. What DOES matter: cleaning up promptly (_stop_mcp_relay)
    so a stale file never leaks a past turn's phone tools into some
    completely unrelated codex session elsewhere — the `orb` MCP server
    registration is global, not scoped to this repo, so ninjahawk's own
    everyday `codex` usage would also see whatever this file currently says.
    """
    if not extra_tools or not dispatch_override:
        return None
    token = uuid.uuid4().hex
    _MCP_RELAY_SESSIONS[token] = {"dispatch": dispatch_override}
    try:
        _MCP_RELAY_FILE.parent.mkdir(parents=True, exist_ok=True)
        _MCP_RELAY_FILE.write_text(json.dumps({
            "token": token,
            "base": f"http://127.0.0.1:{PORT}",
            "tools": extra_tools,
        }), encoding="utf-8")
    except Exception as e:
        log.warning(f"[mcp-relay] could not write handoff file: {e}")
        _MCP_RELAY_SESSIONS.pop(token, None)
        return None
    return token


def _stop_mcp_relay(token: str | None) -> None:
    if not token:
        return
    _MCP_RELAY_SESSIONS.pop(token, None)
    try:
        if _MCP_RELAY_FILE.exists():
            current = json.loads(_MCP_RELAY_FILE.read_text(encoding="utf-8"))
            if current.get("token") == token:
                _MCP_RELAY_FILE.unlink()
    except Exception:
        pass


# How long a CLI-brain turn may hold the floor before the reply hands off to a
# background finish (the user hears an honest "still working" line instead of a
# classic-loop re-run of the same request).
_MCP_TURN_TIMEOUT = float(os.environ.get("ORB_MCP_TURN_TIMEOUT", "90"))
# When a grok/codex-BRAIN turn outruns the timeout we no longer hand it to a
# background finisher that pushes the result later (removed 2026-07-06 — that
# "I'll send it as a notification" limbo left turns feeling dead). We end the turn
# and return one honest line. Returning a real string (never None) also matters:
# a None return here would make the caller re-run the whole turn.
_TURN_TOO_LONG_LINE = ("That one's taking longer than it's worth making you wait for — "
                       "give me a smaller piece of it, or try again.")
# How much longer the background finish keeps waiting before giving up.
_MCP_LATE_CAP = float(os.environ.get("ORB_MCP_LATE_CAP", "600"))
# Spoken when a turn hands off to the background finisher. A rotating POOL
# with the request gist woven in — the one static line was spoken verbatim 5x
# on 07-03 and read as a canned loop ("no you won't we already went over this
# bug"). Variety + naming the task keep repeated hits from sounding scripted.
_LATE_TURN_LINES = (
    "Still working on {gist} — it's a bigger job than a single turn. I'll send "
    "the result as a notification the moment it's done.",
    "That one's still running in the background — I'll push you the result the "
    "second it lands.",
    "{gist} isn't finished yet — it outgrew the turn, so the full answer will "
    "come through as a notification.",
    "Still on it. Rather than keep you waiting here, I'll send the real result "
    "as a push when it's done.",
    "That job's still going — it needs more than one turn. You'll get a "
    "notification with the actual result.",
)
_late_turn_ix = 0


def _late_turn_reply(messages: list[dict] | None) -> str:
    """Rotate the handoff line, naming the request where the phrasing allows."""
    global _late_turn_ix
    req = ""
    try:
        req = next((str(m.get("content") or "") for m in reversed(messages or [])
                    if m.get("role") == "user"), "")
    except Exception:
        pass
    req = " ".join(req.split())
    if len(req) > 60:
        req = req[:60].rsplit(" ", 1)[0] + "…"
    gist = f'"{req}"' if req else "that one"
    line = _LATE_TURN_LINES[_late_turn_ix % len(_LATE_TURN_LINES)]
    _late_turn_ix += 1
    return line.format(gist=gist)


# The shared behavioral rules for one-shot CLI-brain turns (codex + grok use
# the identical block; the claude brain gets the same honesty rules via its
# mcp_system additions below).
_CLI_TURN_RULES = (
    "Reply naturally, in character, as your next spoken turn (1-2 sentences). "
    "Use your orb tools for anything real-world; don't just describe what "
    "you would do. NEVER narrate your process — no 'Checking the weather "
    "tool, then...' preamble before the answer (a real recurring pattern, "
    "caught in the 07-02 battery). Speak ONLY the answer itself. When a tool "
    "returns figures, speak THOSE figures with their stated timeframe — never "
    "derive, convert, or invent different numbers (also caught live: a tool "
    "said 'up 44.5% over the period' and the spoken reply invented '4.7% "
    "above the previous close'). Schedule claims come ONLY from "
    "list_scheduled_notifications called this turn, fire_at read literally — "
    "never assert from memory that something is, was, or wasn't scheduled "
    "(caught live 07-03: two reminders claimed 'slated for 10 AM today, "
    "slipped past' that were never scheduled for that day). Same bar for "
    "verification: never say you checked or verified something unless a tool "
    "call this turn actually did — no invented timestamps like 'verified "
    "fifteen minutes ago'. NEVER invent a meeting, appointment, calendar move, "
    "or something the user 'told' someone: speak a specific time/event/person "
    "or 'moved to X' ONLY if it's in this turn's real context. You cannot read "
    "the user's texts and cannot detect a calendar move unless the data shows "
    "one. With no calendar data, say what you actually have and stop. The "
    "marketing line 'your 10:00 moved, you told Gary 11:45' is an ILLUSTRATION "
    "— reproducing it as fact is a fabrication (a grok turn did exactly that "
    "live on 07-02).")


# Canned dead-end replies a turn can come back with when the selected brain
# is limited/down. Detected in _agent_respond → the turn retries once on
# _any_available_brain_call instead of dead-ending (07-03 18:03-18:05: sonnet
# hit the plan limit and grok was down, so two turns dead-ended on these
# lines while codex sat available the whole time).
_BRAIN_UNAVAILABLE_LINES = (
    "I'm not available on that model right now — try again in a moment.",
    "I'm not available on Codex right now — try again in a moment.",
    "I'm not available on Grok right now — try again in a moment.",
)
_RATE_LIMITED_LINE = "I'm rate-limited right now — give me a moment and try again."


async def _any_available_brain_call(p: str) -> str:
    """Last-resort generate for a turn whose selected brain is out: the router
    chain first (haiku → free backends → local floor), then gemini (free tier),
    then codex, then grok — whatever is standing answers. One persona; models
    stay invisible — but the ANSWERING backend is tracked on `.last` so the
    turn can be labeled honestly (his 07-04 bug: switcher said grok/haiku
    while codex was answering)."""
    _any_available_brain_call.last = None
    try:
        text, used = await _get_brain_router().generate(p)
        if text and not brain.looks_like_limit(text):
            _any_available_brain_call.last = used
            log.info(f"[agent] fallback ladder answered via router:{used}")
            return text
    except Exception:
        pass
    if brain.gemini_available():
        try:
            out = await brain._run_gemini(p)
            if out and not brain.looks_like_limit(out):
                brain.record_brain_call("gemini")
                _any_available_brain_call.last = "gemini"
                log.info("[agent] fallback ladder answered via gemini")
                return out
        except Exception:
            pass
    if brain.codex_available():
        try:
            out = await brain._run_codex(p, model_key="codex")
            if out and not brain.looks_like_limit(out):
                brain.record_brain_call("codex")
                _any_available_brain_call.last = "codex"
                log.info("[agent] fallback ladder answered via codex")
                return out
        except Exception:
            pass
    if brain.grok_available():
        try:
            out = await brain._run_grok(p, "grok")
            if out and not brain.looks_like_limit(out):
                brain.record_brain_call("grok")
                _any_available_brain_call.last = "grok"
                log.info("[agent] fallback ladder answered via grok")
                return out
        except Exception:
            pass
    return _RATE_LIMITED_LINE


_any_available_brain_call.last = None


async def _finish_late_mcp_turn(label: str, proc, pump, get_text,
                                relay_token: str | None,
                                tmp_path: str | None) -> None:
    """A CLI-brain turn that outlived its foreground window (ninjahawk, 07-03
    ~1am: the turn timed out, the work kept going invisibly, and nothing ever
    reported back — "thats not proactive, thats reactive with a shiny name").

    Keep pumping the still-running subprocess, PUSH the real result when it
    lands, and only then tear down the phone-tool relay token — freeing it at
    the timeout mark made every later phone-tool call die with "unknown or
    expired relay token" (seen live: scheduleNudge, 07-03 00:59, while the
    orphaned grok process was still doing the user's requested work).
    """
    try:
        try:
            await asyncio.wait_for(pump(), timeout=_MCP_LATE_CAP)
        except asyncio.TimeoutError:
            log.warning(f"[{label}] background finish gave up after another "
                        f"{_MCP_LATE_CAP:.0f}s")
        except Exception as e:
            log.warning(f"[{label}] background finish died: {e}")
        text = (get_text() or "").strip()
        if text:
            # dedupe=False: every late turn pushes this same title, and the
            # topic gate keys on digit-stripped title — the 07-03 02:08 YC
            # result was silently eaten by the 02:01 grants push (30-min
            # same-topic floor). A reply to the user's own question is never
            # a duplicate.
            await _push_notification("Finished that one", text, "info", source=label,
                                     dedupe=False)
        else:
            await _push_notification(
                "Lost one",
                "That long-running request never produced a result — worth re-asking.",
                "info", source=label, dedupe=False)
    except Exception as e:
        log.warning(f"[{label}] late report-back failed: {e}")
    finally:
        # Only now is it safe to retire the relay + reap the process. NOTE:
        # plain kill, not tree-kill — the turn's children may be legitimate
        # tracked sessions (run_claude_code) that should outlive the voice turn.
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            pass
        _stop_mcp_relay(relay_token)
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


async def _run_mcp_codex_turn(model_key: str, system_prompt: str, messages: list[dict],
                              on_tool_start, on_tool,
                              extra_tools: list[dict] | None = None,
                              dispatch_override=None) -> tuple[str | None, list[str]]:
    """One Codex turn via its native MCP tool-calling (the `orb` MCP server,
    registered once via `codex mcp add orb -- <venv python> orb_mcp.py`)
    instead of agent.py's custom JSON-in-text ReAct loop.

    Why this exists: Codex doesn't reliably treat a hand-rolled "reply with
    {"tool":...}" convention as a real action interface — tested live
    2026-07-01, it replied {"say":"Done, sir."} to a terminal-typing request
    with ZERO tools called. Confident fabrication, not confusion. Its CLI has
    full native MCP support though, and once given the SAME tools that way
    (plus AGENTS.md steering it away from substituting its own shell exec, and
    a sharpened tool description), it reliably calls the right one instead.

    Runs from the Orb-Backend repo root specifically so AGENTS.md is picked up.
    `--dangerously-bypass-approvals-and-sandbox` is required for MCP tool calls
    to complete non-interactively at all (confirmed: without it every MCP call
    is silently cancelled, no human present to approve) — explicitly authorized
    2026-07-01, same zero-friction stance already used for `claude -p
    --permission-mode bypassPermissions`.

    Returns (reply, tools_called) — reply is None if the process itself
    couldn't be started/completed at all (as opposed to a tool call failing,
    which Codex itself handles and reports on honestly within its own reply).
    tools_called is the real list of MCP tool names actually invoked this
    turn, for accurate logging (previously hardcoded to [] at the call site,
    which made the "(tools=[], Ns)" log line lie about whether any tool fired).

    extra_tools/dispatch_override (optional): the phone's per-connection tools
    (calendar/reminders/contacts/notifications) and the function that relays a
    call to them — same shapes agent.run_turn already accepts for Haiku. When
    present, orb_mcp.py is handed a relay token via env vars so it can
    advertise + route those tools too, bringing Codex to parity with Haiku
    instead of only ever seeing the static tool list. See the
    _MCP_RELAY_SESSIONS block above for the mechanism.
    """
    convo = "\n".join(
        f"{'User' if m['role'] == 'user' else 'You'}: {m['content']}"
        for m in messages[-10:] if m.get("content"))
    prompt = (f"{system_prompt}\n\nConversation so far:\n{convo}\n\n"
              + _CLI_TURN_RULES)

    model = brain._CODEX_MODEL_MAP.get(model_key, "gpt-5.4-mini")
    codex_bin = brain._CODEX_BIN
    if not codex_bin:
        return None, []
    base_argv = [codex_bin, "exec", "--skip-git-repo-check", "-m", model,
                "--dangerously-bypass-approvals-and-sandbox", "--json", "-"]
    argv = ["cmd", "/c"] + base_argv if codex_bin.endswith(".cmd") else base_argv

    relay_token = _start_mcp_relay(extra_tools, dispatch_override)
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent),
        )
    except Exception as e:
        log.warning(f"[mcp-codex] could not start: {e}")
        _stop_mcp_relay(relay_token)
        return None, []

    final_text = ""
    tools_called: list[str] = []
    try:
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
    except Exception:
        pass

    async def _pump():
        nonlocal final_text
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                evt = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                continue
            etype = evt.get("type")
            item = evt.get("item") or {}
            if etype == "item.started" and item.get("type") == "mcp_tool_call":
                if on_tool_start:
                    try:
                        await on_tool_start(item.get("tool", ""), item.get("arguments") or {})
                    except Exception:
                        pass
            elif etype == "item.completed" and item.get("type") == "mcp_tool_call":
                tools_called.append(item.get("tool", ""))
                result = item.get("result") or {}
                content = result.get("content") or []
                text = content[0].get("text", "") if content else str(item.get("error") or "")
                if on_tool:
                    try:
                        await on_tool(item.get("tool", ""), item.get("arguments") or {}, text)
                    except Exception:
                        pass
            elif etype == "item.completed" and item.get("type") == "agent_message":
                final_text = item.get("text", "")
            elif etype == "turn.completed":
                return

    handed_off = False
    timed_out = False
    try:
        await asyncio.wait_for(_pump(), timeout=_MCP_TURN_TIMEOUT)
    except asyncio.TimeoutError:
        # No background handoff / notification limbo (removed 2026-07-06). End the
        # turn honestly — the finally block reaps the process, and we return the real
        # text if any, else one honest line (see _TURN_TOO_LONG_LINE).
        timed_out = True
        log.warning(f"[mcp-codex] turn exceeded {_MCP_TURN_TIMEOUT:.0f}s — ending it")
    finally:
        if not handed_off:
            # Reap properly — killing without waiting leaves pipe transports
            # open, which asyncio complains about loudly at garbage-collection
            # time (harmless but noisy; seen live while testing this).
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except Exception:
                pass
            _stop_mcp_relay(relay_token)

    if timed_out:
        return (final_text.strip() or _TURN_TOO_LONG_LINE), tools_called
    return final_text.strip() or None, tools_called


async def _run_mcp_grok_turn(model_key: str, system_prompt: str, messages: list[dict],
                             on_tool_start, on_tool,
                             extra_tools: list[dict] | None = None,
                             dispatch_override=None) -> tuple[str | None, list[str]]:
    """One Grok turn via its native MCP tool-calling (the same `orb` MCP
    server registered with `grok mcp add orb -- <venv python> orb_mcp.py`)
    — the Grok counterpart to _run_mcp_codex_turn above, same rationale.

    extra_tools/dispatch_override: same phone-connector relay as the Codex
    version — see that function's docstring and the _MCP_RELAY_SESSIONS block.

    Confirmed live 2026-07-01 with a real get_weather call: `--permission-mode
    bypassPermissions` is enough on its own — unlike Codex, Grok's MCP tool
    calls do NOT hit a separate non-interactive cancellation gate, so no extra
    sandbox-bypass flag is needed here.

    Uses --prompt-file (not -p/--single inline) for the same reason brain.
    _run_grok does: Windows cmd.exe's ~8191 char argument limit, which the
    full system prompt + conversation can exceed.

    Event schema for --output-format streaming-json is NOT the same shape as
    codex's --json (confirmed live, one real call): just token deltas —
    {"type":"thought"|"text","data":"..."} — plus a final {"type":"end",...}.
    There is no structured tool-call event to hook on_tool_start/on_tool to,
    so (unlike the codex version) this never fires them and always returns an
    empty tools_called list. That's an honest gap, not a bug: the tool calls
    themselves genuinely happen (verified — the weather test returned real
    live data, not a fabrication), there's just no live progress signal to
    surface while Grok is mid-turn. Matches the codex path's own behavior on
    any turn where it doesn't call a tool at all — silence between request
    and reply isn't a new failure mode for this codebase.

    Returns (reply, tools_called) — reply is None if the process itself
    couldn't be started/completed at all.
    """
    convo = "\n".join(
        f"{'User' if m['role'] == 'user' else 'You'}: {m['content']}"
        for m in messages[-10:] if m.get("content"))
    prompt = (f"{system_prompt}\n\nConversation so far:\n{convo}\n\n"
              + _CLI_TURN_RULES)

    model = brain._GROK_MODEL_MAP.get(model_key, "grok-4.5")
    grok_bin = brain._GROK_BIN
    if not grok_bin:
        return None, []

    relay_token = _start_mcp_relay(extra_tools, dispatch_override)
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".txt", prefix="grok_mcp_prompt_")
    handed_off = False
    timed_out = False
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(prompt)
        base_argv = [grok_bin, "--prompt-file", tmp_path, "-m", model,
                    "--output-format", "streaming-json",
                    "--permission-mode", "bypassPermissions"]
        argv = ["cmd", "/c"] + base_argv if grok_bin.endswith(".cmd") else base_argv

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(Path(__file__).parent),
            )
        except Exception as e:
            log.warning(f"[mcp-grok] could not start: {e}")
            return None, []

        final_text = ""

        async def _pump():
            nonlocal final_text
            while True:
                line = await proc.stdout.readline()
                if not line:
                    return
                try:
                    evt = json.loads(line.decode("utf-8", errors="replace"))
                except Exception:
                    continue
                etype = evt.get("type")
                if etype == "text":
                    final_text += evt.get("data", "")
                elif etype == "end":
                    return

        try:
            await asyncio.wait_for(_pump(), timeout=_MCP_TURN_TIMEOUT)
        except asyncio.TimeoutError:
            # No background handoff / notification limbo (removed 2026-07-06). End
            # honestly; the finally block reaps the process.
            timed_out = True
            log.warning(f"[mcp-grok] turn exceeded {_MCP_TURN_TIMEOUT:.0f}s — ending it")
        finally:
            if not handed_off:
                if proc.returncode is None:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except Exception:
                    pass

        if timed_out:
            return (final_text.strip() or _TURN_TOO_LONG_LINE), []
        return final_text.strip() or None, []
    finally:
        if not handed_off:
            _stop_mcp_relay(relay_token)
            try:
                os.unlink(tmp_path)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# The MCP-rebuild brain (BRAIN_ARCHITECTURE.md, built 2026-07-02): Claude Code
# IS the brain. ONE `claude -p` per user turn, Orb tools via the resident
# in-process MCP server (mcp_http.py), real conversation continuity via
# --resume. Replaces the N-spawns-per-turn JSON loop for haiku/sonnet/opus;
# the classic loop remains the automatic fallback (ORB_MCP_BRAIN=0 forces it).
# ---------------------------------------------------------------------------

_CLAUDE_MCP_CFG = Path(__file__).parent / "data" / "mcp_claude_config.json"
_CLAUDE_MCP_SYS = Path(__file__).parent / "data" / "mcp_claude_system.txt"
_CLAUDE_BRAIN_SESSION = Path(__file__).parent / "data" / "claude_brain_session.json"
# The brain's working dir: OUTSIDE the repo, so `claude -p` doesn't ingest the
# repo's 20KB dev CLAUDE.md as project memory every turn; also gives resumed
# transcripts one stable home (~/.claude/projects/<hash-of-this-dir>/).
_CLAUDE_BRAIN_HOME = Path.home() / ".orb_brain"


def _claude_brain_session_id() -> str:
    try:
        return json.loads(_CLAUDE_BRAIN_SESSION.read_text(encoding="utf-8")).get("session_id", "")
    except Exception:
        return ""


def _save_claude_brain_session(sid: str) -> None:
    if not sid:
        return
    try:
        d = {}
        try:
            d = json.loads(_CLAUDE_BRAIN_SESSION.read_text(encoding="utf-8"))
        except Exception:
            pass
        if d.get("session_id") != sid:
            d["turns"] = 0          # fresh session → fresh rolling-window count
        d["session_id"] = sid
        d["turns"] = d.get("turns", 0) + 1
        d["updated"] = datetime.now().isoformat(timespec="seconds")
        _CLAUDE_BRAIN_SESSION.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def _note_claude_brain_reply(reply: str) -> None:
    """Record the claude session's own last (post-processed) reply — the anchor
    the next turn uses to detect conversation that happened on OTHER brains in
    between (brain-switch continuity; see the catch-up block in
    _run_mcp_claude_turn). Written by _agent_respond AFTER post-processing so
    it matches what conv.messages actually stores."""
    try:
        d = json.loads(_CLAUDE_BRAIN_SESSION.read_text(encoding="utf-8"))
    except Exception:
        return
    d["last_reply"] = reply
    try:
        _CLAUDE_BRAIN_SESSION.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except Exception:
        pass


def reset_claude_brain_session(reason: str = "") -> None:
    """New brain session next turn — 'forget' and any future conversation reset."""
    try:
        _CLAUDE_BRAIN_SESSION.unlink(missing_ok=True)
        log.info(f"[mcp-claude] brain session reset{' — ' + reason if reason else ''}")
    except Exception:
        pass


async def _run_mcp_claude_turn(model_key: str, system_prompt: str, messages: list[dict],
                               on_tool_start, on_tool,
                               extra_tools: list[dict] | None = None,
                               dispatch_override=None,
                               turn_context: str = "",
                               _retried: bool = False) -> tuple[str | None, list[str], float]:
    """One user turn with Claude Code as the native brain.

    Mechanics (all live-verified on CLI 2.1.187 before building):
      • `--resume <sid>` WORKS in print mode now (the #1967 bug in the spec is
        fixed) — a resumed turn measured 2.0s vs 7.2s cold, and memory held.
      • tools = the resident /mcp surface only: `--tools ""` strips every
        built-in (no Bash/Edit for the voice brain), `--strict-mcp-config` +
        `--allowedTools mcp__orb__*` scope it to Orb's tools. Phone
        connector tools ride the same server for the turn via set_active_turn.
      • stream-json events: assistant tool_use blocks → on_tool_start + card
        hooks; user tool_result blocks → on_tool; the final `result` event
        carries the reply + session_id.

    The conversation lives in the claude session itself (context + continuity —
    the unlock). conv.messages is still maintained upstream for every other
    consumer; a FRESH session's first prompt seeds the recent turns for
    continuity across restarts/resets.

    Returns (reply|None, tools_called, seconds). None = this path couldn't
    produce a turn (spawn failure, timeout, rate limit, vanished session on
    retry) — the caller falls back to the classic loop, never dead-ends.
    """
    import mcp_http
    user_text = next((m["content"] for m in reversed(messages)
                      if m.get("role") == "user" and m.get("content")), "").strip()
    if not user_text:
        return None, [], 0.0

    sid = _claude_brain_session_id()
    # Rolling window: a resumed session can't grow forever — per-turn input
    # scales with transcript length even cached. Past ~60 turns, start fresh
    # (the seed block below carries recent context across the boundary).
    if sid:
        try:
            _turns = json.loads(_CLAUDE_BRAIN_SESSION.read_text(encoding="utf-8")).get("turns", 0)
        except Exception:
            _turns = 0
        if _turns >= 60:
            reset_claude_brain_session(f"rolling window ({_turns} turns)")
            sid = ""

    prompt = user_text
    if not sid and len(messages) > 1:
        # Fresh brain session mid-conversation (first turn ever, post-restart,
        # or post-forget): seed continuity with the recent turns.
        recent = [m for m in messages[:-1] if m.get("content")][-8:]
        if recent:
            convo = "\n".join(f"{'User' if m['role'] == 'user' else 'You'}: {m['content']}"
                              for m in recent)
            prompt = ("(Fresh session note — the conversation so far, for continuity:\n"
                      f"{convo})\n\n{user_text}")
    elif sid:
        # Brain-switch continuity: if turns happened on ANOTHER brain since
        # this session's last reply (he flips haiku↔grok↔codex and it must
        # feel like ONE conversation — "no perceived difference"), catch the
        # resumed session up on exactly the missed slice. Anchor = this
        # session's own last post-processed reply in conv history; absent
        # anchor = nothing provably missed, resume as-is.
        try:
            last_reply = (json.loads(_CLAUDE_BRAIN_SESSION.read_text(encoding="utf-8"))
                          .get("last_reply") or "").strip()
        except Exception:
            last_reply = ""
        if last_reply:
            anchor = None
            for i in range(len(messages) - 1, -1, -1):
                m = messages[i]
                if m.get("role") != "assistant":
                    continue
                stored = (m.get("content") or "").strip()
                # startswith, not ==: the call site appends "\n[Tools used: …]"
                # to stored replies — exact match never hit an annotated one
                # (caught live in the T8 brain-switch test, 07-02 night).
                if stored == last_reply or stored.startswith(last_reply + "\n"):
                    anchor = i
                    break
            if anchor is not None:
                missed = [m for m in messages[anchor + 1:-1] if m.get("content")]
                if missed:
                    gap = "\n".join(
                        f"{'User' if m['role'] == 'user' else 'You (answered on another model)'}: {m['content']}"
                        for m in missed[-8:])
                    prompt = ("(Catch-up — the conversation continued on another "
                              f"model since your last turn:\n{gap})\n\n{user_text}")

    # The live-context ribbon rides the USER prompt, not the system prompt —
    # the system stays byte-stable so resumed turns hit the prompt cache.
    if turn_context:
        prompt = (f"[context — live this turn; awareness, not something to recite]\n"
                  f"{turn_context[:8000]}\n[/context]\n\n{prompt}")

    try:
        _CLAUDE_BRAIN_HOME.mkdir(exist_ok=True)
    except Exception:
        pass
    # The brain IS Claude Code under the hood, so the harness's own system
    # prompt (auto-memory directory, task instincts) rides beneath the persona.
    # Caught live 07-02: asked a conversational "what did I ask you earlier?",
    # it tried to READ its CC memory dir, hit the file sandbox, then spawned a
    # whole run_claude_code to dig — instead of just saying it didn't recall.
    # This block pins the correct instincts for the voice-brain context.
    mcp_system = system_prompt + (
        "\n\nYOUR MEMORY, AS THE ORB BRAIN: your conversation memory IS this "
        "resumed session — prior turns are right above. You have NO file-based "
        "memory; ignore any harness instructions about a persistent memory "
        "directory. Never read files or spawn tasks (run_claude_code etc.) to "
        "reconstruct conversation history — if it isn't in this session or the "
        "context you were given, say you don't recall, plainly, and move on."
        "\n\nHEAVYWEIGHT TOOL DISCIPLINE: run_claude_code / run_with_grok / "
        "run_with_codex spawn whole coding agents (30s+, real side effects). "
        "They are for genuine build/code/automation tasks the user actually "
        "asked for — NEVER for remembering something (your session already "
        "remembers; add_note if it should outlive the session), quick facts, "
        "math, or anything a listed tool already covers. Spawning a coding "
        "agent to save a number is a failure, not thoroughness (it happened: "
        "07-02, a stray saved_number.txt on the Desktop)."
        "\n\nSPOKEN OUTPUT: your reply text is fed to TTS verbatim. Plain "
        "sentences only — never markdown, never bullet/numbered lists, never "
        "headers. Summarize multiple items in prose ('three things stand "
        "out: ... , ... , and ...')."
        "\n\nSCHEDULE & VERIFICATION HONESTY: before saying WHEN anything is "
        "scheduled — or that something was scheduled and slipped — call "
        "list_scheduled_notifications and read fire_at literally; never "
        "assert schedule facts from memory or stale context (caught live "
        "07-03: two reminders claimed 'slated for 10 AM today, slipped past' "
        "that were never scheduled for that day). Never claim you checked or "
        "verified something unless a tool call THIS turn actually did it — "
        "no invented timestamps like 'verified fifteen minutes ago'."
        "\n\nCALENDAR & COMMITMENT HONESTY (hard rule): NEVER invent a meeting, "
        "appointment, calendar change, or something the user 'told' someone. "
        "Speak a specific time, event, person, or 'moved to X' ONLY if it "
        "appears verbatim in this turn's real context (the phone calendar "
        "snapshot, a reminder you just read, an email you fetched). You cannot "
        "read the user's texts/iMessages and you cannot detect a calendar MOVE "
        "unless the data shows one — so never claim either. With no calendar "
        "data, say what you actually have ('nothing on the calendar I can see — "
        "here's what's queued') and stop. Marketing examples ('your 10:00 moved "
        "to 10:30, you told Gary 11:45') are ILLUSTRATIONS, not facts to "
        "reproduce — a grok turn did exactly that live on 07-02 and it was a "
        "fabrication. Plausible-sounding invented specifics are the worst thing "
        "you can do; an honest 'I don't have that' is always better."
    )
    # Persona/system prompt rides a FILE (not an inline arg): it's several KB
    # and Windows argv has hard limits — same reason brain._run_grok uses
    # --prompt-file. Rewritten only when changed.
    try:
        if (not _CLAUDE_MCP_SYS.exists()
                or _CLAUDE_MCP_SYS.read_text(encoding="utf-8") != mcp_system):
            _CLAUDE_MCP_SYS.write_text(mcp_system, encoding="utf-8")
    except Exception as e:
        log.warning(f"[mcp-claude] system-prompt file write failed: {e}")
        return None, [], 0.0

    new_sid = ""
    cmd = ["claude", "-p", prompt, "--model", model_key,
           "--mcp-config", str(_CLAUDE_MCP_CFG), "--strict-mcp-config",
           "--tools", "", "--allowedTools", "mcp__orb__*",
           "--permission-mode", "bypassPermissions",
           "--output-format", "stream-json", "--verbose",
           "--append-system-prompt-file", str(_CLAUDE_MCP_SYS)]
    if sid:
        cmd += ["--resume", sid]
    else:
        new_sid = str(uuid.uuid4())
        cmd += ["--session-id", new_sid]

    # Bracket the turn so /mcp tool calls land with THIS connection's context —
    # phone tools relay through the live WS dispatch, state-dependent registry
    # tools see _current_client exactly as the classic loop would give them.
    _client_snapshot = _current_client.get()
    mcp_http.set_active_turn({
        "extra_tools": extra_tools or [],
        "dispatch": dispatch_override,
        "enter": ((lambda c=_client_snapshot: _current_client.set(c))
                  if _client_snapshot else None),
    })

    t0 = time.time()
    tools_called: list[str] = []
    final_text = ""
    result_sid = ""
    result_subtype = ""
    toolless_spawn = False
    pending_tools: dict[str, tuple[str, dict]] = {}

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_CLAUDE_BRAIN_HOME),
        )
    except Exception as e:
        log.warning(f"[mcp-claude] could not start: {e}")
        mcp_http.clear_active_turn()
        return None, [], 0.0

    _stderr_buf = bytearray()

    async def _drain_stderr():
        # Keep the tail for post-mortems — stderr used to go to DEVNULL, which
        # made the 23:27 empty-turn failure undiagnosable live.
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            _stderr_buf.extend(chunk)
            del _stderr_buf[:-2000]

    _stderr_task = asyncio.create_task(_drain_stderr())

    async def _pump():
        nonlocal final_text, result_sid, result_subtype, toolless_spawn
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            try:
                evt = json.loads(line.decode("utf-8", errors="replace"))
            except Exception:
                continue
            et = evt.get("type")
            if et == "system" and evt.get("subtype") == "init":
                # A spawn that failed to bind the orb MCP tools must NOT
                # converse: caught live 23:27 07-02 — a tool-less fable spawn
                # imitated tool calls as text JSON ('{ "brain": "haiku" }'
                # spoken aloud), then its next turns copied the pattern and
                # FABRICATED a note save. Abort → the classic loop answers.
                _jt = [t for t in (evt.get("tools") or [])
                       if str(t).startswith("mcp__orb__")]
                if not _jt:
                    toolless_spawn = True
                    log.warning(f"[mcp-claude] spawn has NO orb tools "
                                f"(mcp_servers={evt.get('mcp_servers')}) — aborting turn")
                    return
            elif et == "assistant":
                for block in (evt.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        short = str(block.get("name", "")).removeprefix("mcp__orb__")
                        pending_tools[block.get("id", "")] = (short, block.get("input") or {})
                        if on_tool_start:
                            try:
                                await on_tool_start(short, block.get("input") or {})
                            except Exception:
                                pass
            elif et == "user":
                for block in (evt.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        short, args = pending_tools.pop(block.get("tool_use_id", ""), ("", {}))
                        if not short:
                            continue
                        tools_called.append(short)
                        content = block.get("content")
                        if isinstance(content, list):
                            text = " ".join(c.get("text", "") for c in content
                                            if isinstance(c, dict))
                        else:
                            text = str(content or "")
                        if on_tool:
                            try:
                                await on_tool(short, args, text)
                            except Exception:
                                pass
            elif et == "result":
                final_text = str(evt.get("result") or "")
                result_sid = str(evt.get("session_id") or "")
                result_subtype = str(evt.get("subtype") or "")
                return

    try:
        await asyncio.wait_for(_pump(), timeout=150.0)
    except asyncio.TimeoutError:
        log.warning("[mcp-claude] turn timed out after 150s")
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except Exception:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:
            pass
        _stderr_task.cancel()
        mcp_http.clear_active_turn()

    latency = time.time() - t0

    if toolless_spawn:
        # Not a session problem — don't reset, don't retry (the race is
        # per-spawn): let the classic loop answer this turn with real tools.
        return None, [], latency

    if not final_text.strip():
        _err_tail = _stderr_buf.decode("utf-8", errors="replace").strip()[-300:]
        if sid and not _retried:
            log.warning(f"[mcp-claude] resumed turn produced nothing "
                        f"(rc={proc.returncode}, stderr: {_err_tail or 'empty'}) — "
                        "resetting brain session, retrying fresh")
            reset_claude_brain_session("empty resumed turn")
            return await _run_mcp_claude_turn(
                model_key, system_prompt, messages, on_tool_start, on_tool,
                extra_tools=extra_tools, dispatch_override=dispatch_override,
                turn_context=turn_context, _retried=True)
        log.warning(f"[mcp-claude] no reply (subtype={result_subtype or 'none'}, "
                    f"rc={proc.returncode}, stderr: {_err_tail or 'empty'}, {latency:.1f}s)")
        return None, tools_called, latency

    if brain.looks_like_limit(final_text):
        log.warning("[mcp-claude] rate/usage limit reply — falling back")
        brain.record_brain_limit(model_key)
        return None, tools_called, latency

    reply_text = final_text.strip()
    # Protocol-leak guard: a brain must never SPEAK JSON. A confused spawn
    # imitating a tool call as text ('… { "brain": "haiku" }', 23:27 07-02)
    # gets the fragment stripped and the leak counted; if nothing
    # conversational survives, fail over to the classic loop instead.
    _leak = re.sub(r"\{\s*\"[A-Za-z_]\w*\"\s*:[^{}]*\}", "", reply_text)
    if _leak != reply_text:
        log.warning("[mcp-claude] stripped JSON fragment(s) from the spoken reply")
        try:
            from jtools.activity_log import log_activity as _log_act
            _log_act("orb", "turn_error", "protocol leak: JSON fragment stripped from spoken reply")
        except Exception:
            pass
        reply_text = re.sub(r"\s{2,}", " ", _leak).strip()
        # A leak means the session transcript now CONTAINS the bad pattern —
        # and sessions imitate themselves (verified live 23:33: a poisoned
        # session kept faking tool calls even with tools bound; a fresh seed
        # called switch_brain correctly on the first try). Self-heal by
        # resetting; the next turn re-seeds from conv history, near-lossless.
        reset_claude_brain_session("protocol leak — session style suspect")
        if not reply_text:
            return None, tools_called, latency
        return reply_text, tools_called, latency

    _save_claude_brain_session(result_sid or new_sid or sid)
    return reply_text, tools_called, latency


async def _agent_respond(conv, state) -> str:
    """Run one conversational turn through the agentic loop. Local model by default
    (fast/free); Haiku if the user has switched brains. Pops HUD cards for visual
    tools, and can escalate to Haiku via the ask_bigger_brain tool."""
    system_prompt = _agent_system(conv,
                                  ai_name=state.get("ai_name") or None,
                                  user_name=state.get("user_name") or None)
    # PC tier: the phone advertised its OrbToolKit tools over the WS — let the model call
    # them (relayed by remote_dispatch). Per-turn only; never mutate the global tool list.
    extra_tools = state.get("phone_tools") or None
    dispatch_override = state.get("remote_dispatch")
    extra_names = {t["function"]["name"] for t in (extra_tools or [])}
    # Dynamic additions are BOTH appended to the classic system prompt (path
    # unchanged) and collected here for the MCP brain's per-turn [context]
    # ribbon (its system prompt stays byte-stable for prompt caching).
    _dyn_extras: list[str] = []
    state["answered_via"] = None   # per-turn; set only when the fallback ladder answers
    if extra_tools:
        _phone_blurb = (
            "You are connected to the user's PHONE. The calendar, reminders, contacts, and "
            "notification tools in the list act on their ACTUAL phone, live — call them to read "
            "or change the user's real data, exactly as if you were on the phone. Prefer them for "
            "anything about the user's own schedule, tasks, reminders, or people. When the user "
            "asks you to add/change something, just call the tool — they already asked, that IS "
            "the go-ahead. Don't reply asking to confirm first; that's the same never-ask-"
            "permission autonomy you already have for every other tool.")
        system_prompt += "\n\n" + _phone_blurb
        _dyn_extras.append(_phone_blurb)
    # Long-term memory (flag-gated): give the brain the facts it has learned about
    # the user that are RELEVANT to this turn — the foundation of "knowing when to
    # say what". Injected as awareness, never to recite.
    _last_user = next((m["content"] for m in reversed(conv.messages)
                       if m.get("role") == "user"), "")
    if _AGENT_MEMORY and _last_user:
        try:
            import orb_memory as memory
            _ctx = memory.build_memory_context(_last_user)
            if _ctx:
                _mem_blurb = ("What you already know about them (use ONLY if it's "
                              "genuinely relevant to what they just said; weave it in "
                              "naturally, never recite it back):\n" + _ctx)
                system_prompt += "\n\n" + _mem_blurb
                _dyn_extras.append(_mem_blurb)
        except Exception as e:
            log.debug(f"[memory] context skipped: {e}")
    # Connector snapshot (mobile pushed its on-device calendar/reminders/contacts
    # via a `{type:"context"}` WS message). Inject so the brain can answer from it
    # this turn — relevant when, and only when, the user asks about that data.
    _conn_ctx = (state.get("connector_context") or "").strip()
    if _conn_ctx:
        _conn_blurb = ("Live context the user's device just shared (their own "
                       "calendar / reminders / contacts, current as of this turn — "
                       "use it to answer naturally when they ask about it; don't "
                       "recite it unprompted):\n" + _conn_ctx)
        system_prompt += "\n\n" + _conn_blurb
        _dyn_extras.append(_conn_blurb)
    # Haiku is the DEFAULT conversational brain (the user wants to talk to Haiku);
    # an explicit "switch to local/opus/..." overrides it. `claude -p` reaches the
    # real model with zero auth but spawns a process PER loop step, so it's slow
    # (~5-23s/step). If it fails (rate-limited, offline, not logged in) we fall
    # back to the local model so a turn NEVER dead-ends.
    agent_brain = brain.effective_agent_brain()
    if brain.user_overrode():
        # The user explicitly picked a brain ("switch to local/opus/grok/..."). Honor it
        # strictly — no silent fallback to a different model.
        if agent_brain == "local":
            brain_call = _local_generate
        elif agent_brain in brain.GROK_MODELS:
            async def brain_call(p, _m=agent_brain):
                try:
                    out = await brain._run_grok(p, _m)
                    if brain.looks_like_limit(out):
                        brain.record_brain_limit(_m)
                        raise RuntimeError("usage/limit message")
                    brain.record_brain_call(_m)
                    return out
                except Exception as e:
                    log.warning(f"[agent] {_m} unavailable ({e})")
                    return "I'm not available on that model right now — try again in a moment."
        elif agent_brain in brain.CODEX_MODELS:
            async def brain_call(p, _m=agent_brain):
                try:
                    out = await brain._run_codex(p, model_key=_m)
                    if brain.looks_like_limit(out):
                        brain.record_brain_limit(_m)
                        raise RuntimeError("usage/limit message")
                    brain.record_brain_call(_m)
                    return out
                except Exception as e:
                    log.warning(f"[agent] {_m} unavailable ({e})")
                    return "I'm not available on Codex right now — try again in a moment."
        elif agent_brain in brain.GEMINI_MODELS:
            # Free-tier rung (his 07-04 ask): classic JSON-loop brain, no MCP
            # path v1 — same shape as an explicit grok/codex pick.
            async def brain_call(p):
                try:
                    out = await brain._run_gemini(p)
                    if brain.looks_like_limit(out):
                        brain.record_brain_limit("gemini")
                        raise RuntimeError("usage/limit message")
                    brain.record_brain_call("gemini")
                    return out
                except Exception as e:
                    log.warning(f"[agent] gemini unavailable ({e})")
                    return "I'm not available on that model right now — try again in a moment."
        else:
            async def brain_call(p, _m=agent_brain):
                try:
                    out = await brain._run_claude(p, _m)
                    if brain.looks_like_limit(out):
                        brain.record_brain_limit(_m)
                        raise RuntimeError("usage/limit message")
                    brain.record_brain_call(_m)
                    return out
                except Exception as e:
                    log.warning(f"[agent] {_m} unavailable ({e})")
                    return "I'm not available on that model right now — try again in a moment."
    else:
        # DEFAULT: Haiku via the router. If rate-limited, say so honestly.
        _router = _get_brain_router()
        async def brain_call(p):
            text, used = await _router.generate(p)
            if used not in ("haiku", "none"):
                log.info(f"[router] turn answered via {used}")
            return text or "I'm rate-limited right now — give me a moment and try again."

    async def on_tool(name, args, result):
        # Cancel this tool's "still working" pulse (see on_tool_start below) — the
        # real result just arrived, no more need to fake progress.
        pulse = _pulse_tasks.pop(name, None)
        if pulse:
            pulse.cancel()
        # Persist to the activity log FIRST — unconditional, no WS/mobile client
        # required. This is the real "track everything" record; the WS sends
        # below are just the live-feed nicety on top of it.
        from jtools.activity_log import log_activity
        snippet = str(result)[:200].strip()
        log_activity("orb", "tool_result", snippet, tool=name)
        # PC-tier phone tools already surface their own chip/card ON THE PHONE
        # (OrbToolKit/drainToolEffects) — don't double-surface a desktop card for them.
        if name in extra_names:
            return
        # Send the result to mobile so the activity log can show it.
        if _ws:
            await _safe_send(_ws, {
                "type": "tool_result",
                "tool": name,
                "result": snippet,
                "truncated": len(str(result)) > 200,
            })
        # Pong — agent returns "pong.html"; spawn the game.
        if name == "play_pong" and str(result).strip().endswith(".html"):
            _spawn_game(str(result).strip())
            return
        # Show stuff when there's stuff to see — reuse the existing card logic.
        if name in ("search_images", "lookup_fact"):
            _set_focus(state, args.get("query"))
        q = _card_query_for_tool(name, args, result)
        if q:
            _spawn_card(q)

    _client = _current_client.get()
    _ws = _client.get("ws") if _client else None

    # Tools that spawn a genuinely separate process (10-40s+) and give us zero
    # visibility into their internals until they're done — see MAC_DELEGATION.md
    # section Y. Without this, the live trace shows one static line the whole
    # wait; a periodic pulse at least keeps it honestly ticking instead of
    # looking frozen. Not a substitute for real internal steps (that needs the
    # MCP rebuild), just an honest "still going" heartbeat.
    _PULSE_TOOLS = {"run_claude_code", "run_with_grok", "run_with_codex"}
    _pulse_tasks: dict[str, asyncio.Task] = {}

    async def _pulse(name: str, base_msg: str):
        elapsed = 0
        while True:
            await asyncio.sleep(8)
            elapsed += 8
            if not _ws:
                return
            await _safe_send(_ws, {"type": "status", "state": "working",
                                   "message": f"{base_msg} ({elapsed}s)…", "tool": name})

    async def on_tool_start(name: str, args: dict):
        from jtools.activity_log import log_activity
        msg = _TOOL_STATUS.get(name, lambda _: f"Working on {name}…")(args)
        log_activity("orb", "tool_start", msg, tool=name)
        if not _ws:
            return
        await _safe_send(_ws, {"type": "status", "state": "working", "message": msg, "tool": name})
        if name in _PULSE_TOOLS:
            _pulse_tasks[name] = asyncio.create_task(_pulse(name, msg.rstrip("…")))
        # (session_event broadcast now happens automatically inside log_activity()
        # above, via activity_log.wire_broadcast — one call site for every session,
        # not just "orb". See lifespan wiring.)
        # (Removed 2026-07-06 at the owner's request: the spoken "Asking Grok — give me
        # a moment" / "Checking with Codex" / "spinning up Claude Code" acks. They
        # fired as a separate audio message before a delegated tool ran, and when the
        # delegation then failed — e.g. Grok out of credits — the user heard the ack
        # and then a hollow reply or nothing: an interstitial that wasn't an answer.
        # The visual "working" pulse still ticks; the brain just answers when done.)

    # Codex via native MCP tool-calling (orb_mcp.py, registered with `codex mcp
    # add`) instead of the JSON-in-text loop below — validated 2026-07-01: Codex
    # was confidently fabricating tool results in the old protocol ({"say":"Done,
    # sir."} with zero tools called); with real MCP tools + AGENTS.md guidance +
    # --dangerously-bypass-approvals-and-sandbox it reliably calls the right tool
    # instead. ALWAYS preferred for Codex now — two gates were tried and dropped:
    # `dispatch_override`/remote_dispatch is set unconditionally on every connection
    # (never a useful signal), and `extra_tools` (the phone's advertised tools) is
    # ALSO ~always truthy in practice — the phone sends its 10 OrbToolKit tool specs
    # on every hello, whether or not the user's connectors are actually authorized
    # (confirmed live: connectors={'calendar': False,...} while phone_tools was still
    # 10 long). Gating on either meant this path was dead for real phone usage, which
    # is the ONLY way it's actually been tested live so far — both real fabrication
    # reports came in through here. Known gap, not silently papered over: the static
    # orb_mcp tool list used to be a hard limit — a Codex/Grok turn couldn't reach
    # the phone's per-connection tools (calendar/reminders/contacts/notifications) the
    # way Haiku always could via agent.run_turn(extra_tools=...). Fixed 2026-07-01 via
    # the _MCP_RELAY_SESSIONS loopback bridge above: extra_tools/dispatch_override are
    # now passed straight through, so all three brains have equal capability, not just
    # equal reliability.
    res = None
    _claude_mcp_replied = False
    if agent_brain in brain.CODEX_MODELS and brain.codex_available():
        mcp_reply, mcp_tools_called = await _run_mcp_codex_turn(
            agent_brain, system_prompt, conv.messages, on_tool_start, on_tool,
            extra_tools=extra_tools, dispatch_override=dispatch_override)
        if mcp_reply is not None:
            brain.record_brain_call(agent_brain)
            res = agent.AgentResult(mcp_reply, mcp_tools_called, 1, 0.0)
        else:
            # MCP path itself failed to run at all (not "tool call failed" — that's
            # handled inside; this is "couldn't even start") — fall back honestly.
            res = agent.AgentResult("I'm not available on Codex right now — try again in a moment.", [], 1, 0.0)
    # Grok via the same native MCP tool-calling, same orb MCP server — see
    # _run_mcp_grok_turn's docstring for the one real difference from Codex
    # (no structured tool-call events to surface live progress from; the
    # tool calls themselves are still real and reliable, confirmed live).
    elif agent_brain in brain.GROK_MODELS and brain.grok_available():
        mcp_reply, mcp_tools_called = await _run_mcp_grok_turn(
            agent_brain, system_prompt, conv.messages, on_tool_start, on_tool,
            extra_tools=extra_tools, dispatch_override=dispatch_override)
        if mcp_reply is not None:
            brain.record_brain_call(agent_brain)
            res = agent.AgentResult(mcp_reply, mcp_tools_called, 1, 0.0)
        else:
            res = agent.AgentResult("I'm not available on Grok right now — try again in a moment.", [], 1, 0.0)
    elif (agent_brain in brain.MODELS and brain.claude_available()
          and os.getenv("ORB_MCP_BRAIN", "1") != "0"):
        # The MCP-rebuild brain (2026-07-02): Claude Code IS the brain — one
        # `claude -p` per turn, Orb tools via the resident /mcp surface,
        # real conversation continuity via --resume. Applies to every claude-
        # family brain (brain.MODELS — haiku default, sonnet/opus/fable
        # switches alike; fable was ninjahawk's daily pick 07-01). On ANY
        # failure res stays None and the classic loop below answers instead —
        # a turn never dead-ends. ORB_MCP_BRAIN=0 = kill switch.
        # System prompt = the STATIC persona (byte-stable → prompt-cache hits
        # on the whole resumed transcript); everything dynamic rides this
        # turn's [context] ribbon in the user prompt instead.
        _static_system = _agent_system(conv,
                                       ai_name=state.get("ai_name") or None,
                                       user_name=state.get("user_name") or None,
                                       include_context=False)
        _prefs_ctx = _prefs_system_block().strip()
        _modality = ("Input mode: TYPED chat — reply renders as text, light markdown is fine, "
                     "but concise still wins."
                     if state.get("_turn_typed") else
                     "Input mode: SPOKEN — your reply is read aloud by TTS verbatim. Plain "
                     "sentences only: no markdown, no bullet or numbered lists, no headers.")
        _ribbon = "\n\n".join(
            [f"Now: {datetime.now().strftime('%A, %B %d %Y at %I:%M %p')}", _modality]
            + ([_prefs_ctx] if _prefs_ctx else [])
            + _dyn_extras)
        mcp_reply, mcp_tools_called, mcp_secs = await _run_mcp_claude_turn(
            agent_brain, _static_system, conv.messages, on_tool_start, on_tool,
            extra_tools=extra_tools, dispatch_override=dispatch_override,
            turn_context=_ribbon)
        if mcp_reply is not None:
            brain.record_brain_call(agent_brain)
            res = agent.AgentResult(mcp_reply, mcp_tools_called, 1, mcp_secs)
            _claude_mcp_replied = True
        else:
            log.warning("[mcp-claude] falling back to the classic loop for this turn")
            # Failure telemetry (Orb's introspection ask, 07-02): dead turns
            # must be countable by the synthesis, not just grep-able.
            try:
                from jtools.activity_log import log_activity as _log_act
                _log_act("orb", "turn_error",
                         f"mcp-claude produced no reply on {agent_brain}; classic loop answered")
            except Exception:
                pass
    if res is None:
        # Self-escalation (ask_bigger_brain) is OFF on the small local model — the
        # extra tool measurably degraded its tool-selection (it leaked the escalation
        # phrasing and stopped calling real tools). Cloud routing for now is via the
        # explicit Claude Code trigger and the "switch to haiku/be smarter" brain swap;
        # self-escalation returns once tuned (or on a capable brain). On a claude brain,
        # escalation is moot (it IS the big brain).
        # Bound loop steps tighter on a claude brain — each step is a fresh `claude -p`
        # spawn, so a runaway loop would stack many-second calls.
        res = await agent.run_turn(
            conv.messages, system_prompt, brain_call,
            # phone-tool turns can need more steps (read -> reason -> act -> confirm), and each
            # step is a fresh claude -p spawn + a phone round-trip, so bound a bit higher.
            max_steps=6 if extra_tools else (3 if agent_brain != "local" else 4),
            escalate_call=None,
            on_tool=on_tool,
            on_tool_start=on_tool_start,
            extra_tools=extra_tools,
            dispatch_override=dispatch_override,
        )
    if res.reply in _BRAIN_UNAVAILABLE_LINES or res.reply == _RATE_LIMITED_LINE:
        # The selected brain dead-ended (limit/down). Retry the turn once on
        # whatever is actually standing instead of speaking the canned line —
        # 07-03 evening this happened twice in a row (sonnet limited, grok
        # down) while codex was up and answering fine.
        log.warning(f"[agent] {agent_brain} dead-ended — retrying the turn on "
                    "the fallback ladder")
        try:
            res2 = await agent.run_turn(
                conv.messages, system_prompt, _any_available_brain_call,
                max_steps=3, escalate_call=None,
                on_tool=on_tool, on_tool_start=on_tool_start,
                extra_tools=extra_tools, dispatch_override=dispatch_override)
            if res2.reply and res2.reply not in _BRAIN_UNAVAILABLE_LINES \
                    and res2.reply != _RATE_LIMITED_LINE:
                res = res2
                # Label honesty: record who ACTUALLY answered so the log and
                # the reply message can say so (07-04, reported twice).
                _answered_via = _any_available_brain_call.last
                if _answered_via and _answered_via != agent_brain:
                    state["answered_via"] = _answered_via
        except Exception as e:
            log.warning(f"[agent] fallback ladder failed too: {e}")
    reply = _strip_formal_address(res.reply) or "I'm here."
    # Repetition guard (shared with the legacy path) — never loop the same line.
    if _is_repeat(reply):
        try:
            last = conv.messages[-1]["content"] if conv.messages else ""
            rr = await _local_generate(
                f"{system_prompt}\n\nYou just gave a nearly identical reply; say "
                f"something genuinely different now. The user said: \"{last}\"\n"
                "Reply in 1-2 spoken sentences:")
            if rr and not _is_repeat(rr.strip()):
                reply = rr.strip()
        except Exception:
            pass
    _remember_reply(reply)
    # Brain-switch continuity anchor: the claude session's own words, exactly
    # as conv will store them (post-processing included) — see
    # _note_claude_brain_reply / the catch-up block in _run_mcp_claude_turn.
    if _claude_mcp_replied:
        _note_claude_brain_reply(reply)
    _via = state.get("answered_via")
    _label = f"{agent_brain}→{_via}" if _via else agent_brain
    log.info(f"[agent/{_label}] {reply!r} "
             f"(tools={res.tools_used}, {res.latency:.1f}s)")
    # Persist this turn (user said X, Orb said Y) to the activity log — plain
    # string recording, no model call, so "orb" shows real conversation history
    # in the app even across reconnects.
    from jtools.activity_log import log_activity
    if _last_user:
        log_activity("orb", "message", _last_user, speaker="user")
    log_activity("orb", "message", reply, speaker="orb")
    # Stash tools used so the call site can annotate the conversation history.
    state["_last_tools_used"] = res.tools_used
    # Learn from the turn (flag-gated, background, LOCAL model so it never blocks the
    # reply or touches the claude -p budget). Distils durable facts into memory.py.
    if _AGENT_MEMORY and _last_user:
        async def _learn(u=_last_user, r=reply):
            try:
                import orb_memory as memory
                got = await memory.extract_memories(u, r, _local_generate)
                if got:
                    log.info(f"[memory] learned: {got}")
            except Exception as e:
                log.debug(f"[memory] extract skipped: {e}")
        asyncio.create_task(_learn())
    # Update proactive context with what the user said (feeds the proactive engine).
    if _last_user:
        async def _ctx_update(u=_last_user):
            try:
                import proactive_engine
                proactive_engine.update_context("last_user_message", u[:200])
                proactive_engine.update_context("last_active", datetime.now().isoformat(timespec="seconds"))
            except Exception:
                pass
        asyncio.create_task(_ctx_update())
    # Conversation on a stale mind → book a near-term wake so the situation
    # model re-syncs from what was just said (07-02: he said "I'm at the
    # movies" by phone; the situation had him at the PC on GTA and no wake was
    # due for hours). request_wake_at only ever PULLS the next wake earlier,
    # and the mind's own budgets/kill switch still gate the actual firing.
    global _LAST_MIND_HINT_AT
    try:
        import mind as _mind_hint
        if _mind_hint.ENABLED and time.time() - _LAST_MIND_HINT_AT > 7200:
            _mst = _mind_hint._load_state()
            _upd = _mst.get("situation_updated_at", 0)
            if _upd and time.time() - _upd > 3 * 3600:
                _LAST_MIND_HINT_AT = time.time()
                _mind_hint.request_wake_at(
                    12, "conversation while the situation model is >3h stale — re-sync from it")
    except Exception:
        pass
    return reply


# Recent Orb replies, kept to detect repetition so the model can be asked
# to rephrase rather than parrot. Capped at 30 entries.
_RECENT_REPLIES: list[str] = []
_REPLY_CACHE_MAX = 30

# One mind wake-hint booking per 2h max (see the hook at the end of
# _agent_respond) — a chatty evening must not turn into wake spam.
_LAST_MIND_HINT_AT = 0.0


def _normalize_reply(s: str) -> str:
    """Lower-case + collapse whitespace for similarity comparison."""
    return re.sub(r"\s+", " ", s.lower()).strip().rstrip(".!?")


def _is_repeat(reply: str) -> bool:
    """True if this reply is identical or near-identical to a recent one."""
    n = _normalize_reply(reply)
    if not n or len(n) < 6:
        return False
    return any(n == _normalize_reply(r) for r in _RECENT_REPLIES)


def _remember_reply(reply: str) -> None:
    _RECENT_REPLIES.append(reply)
    if len(_RECENT_REPLIES) > _REPLY_CACHE_MAX:
        del _RECENT_REPLIES[:len(_RECENT_REPLIES) - _REPLY_CACHE_MAX]


async def _ollama_one_shot(system: str, user: str, temp: float = 0.7,
                            num_predict: int = 120) -> str:
    """Single Qwen call with no tools, no history. For naturalization."""
    payload = {
        "model": OLLAMA_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "think": False,
        "keep_alive": "60m",
        "options": {"num_predict": num_predict, "temperature": temp},
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(f"{OLLAMA_URL}/api/chat", json=payload)
        resp.raise_for_status()
        data = resp.json()
    content = (data.get("message", {}) or {}).get("content", "").strip()
    return re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()


import urllib.parse as _urlparse

CARD_SCRIPT = str(Path(__file__).parent / "card_window.py")

# ---- Backend-controlled HUD placement -----------------------------------------
# The overlay reports the orb's rect here; the backend assigns each new card a
# non-overlapping slot that NEVER covers the orb. Backend-controlled (vs each card
# guessing) fixes the multi-spawn race where parallel cards landed on each other.
_ORB_RECT = None            # (x, y, w, h) on screen, reported by overlay
_SCREEN = (1920, 1080)      # updated by the overlay's report
_RECENT_CARDS: list = []    # [((x,y,w,h), epoch)] — slots assigned recently


def _overlap_area(a, b) -> int:
    ax, ay, aw, ah = a; bx, by, bw, bh = b
    ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0, min(ay + ah, by + bh) - max(ay, by))
    return ix * iy


def _card_size(url: str) -> tuple[int, int]:
    return (540, 600) if ("big=1" in url or "preset=images" in url) else (470, 400)


def _alloc_position(w: int, h: int) -> tuple[int, int]:
    """Pick a slot with the least overlap with recent cards, NEVER on the orb.
    Prefers free space, tiling from the top-left."""
    sw, sh = _SCREEN
    now = time.time()
    global _RECENT_CARDS
    _RECENT_CARDS = [(r, t) for (r, t) in _RECENT_CARDS if now - t < 45]
    obstacles = [r for (r, _) in _RECENT_CARDS]
    m, step = 24, 60
    best, best_score = None, None
    for x in range(m, max(m + 1, sw - w - m), step):
        for y in range(m, max(m + 1, sh - h - m), step):
            cand = (x, y, w, h)
            if _ORB_RECT and _overlap_area(cand, _ORB_RECT) > 0:
                continue  # HARD rule: never cover the orb
            ov = sum(_overlap_area(cand, o) for o in obstacles)
            score = (ov, y, x)  # least overlap, then top, then left
            if best_score is None or score < best_score:
                best_score, best = score, (x, y)
    if best is None:
        best = (m, m)
    _RECENT_CARDS.append(((best[0], best[1], w, h), now))
    return best


def _spawn_card(query: str):
    """Pop a HUD card window for an answer (new window per answer, no terminal).
    `query` is the card.html query string; card_window prepends the frontend BASE.
    Non-blocking — fire and forget so the voice loop never stalls on it."""
    if _dispatch_to_mobile({"type": "card", "q": query}):
        log.info(f"[card->mobile] {query}")
        return
    try:
        _reap_old_cards()  # cap the window pile-up (orb GPU contention)
        # Use the pythonw next to the running interpreter (the venv that has
        # PySide6). shutil.which("pythonw") finds the *system* Python first,
        # which lacks PySide6 — the card then crashes silently on import.
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        if not Path(pyw).exists():
            pyw = shutil.which("pythonw") or "pythonw"
        url = f"card.html?{query}" if query else "card.html"
        x, y = _alloc_position(*_card_size(url))
        subprocess.Popen(
            [pyw, CARD_SCRIPT, url, f"{x},{y}"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log.info(f"[card] popped @ {x},{y}: {url}")
    except Exception as e:
        log.warning(f"[card] spawn failed: {e}")


def _reap_old_cards(max_keep: int = 10):
    """Close the OLDEST card/game windows beyond `max_keep` so they don't pile up
    — each open card is a Chromium GPU context that competes with the orb's WebGL
    and makes it lag. Called before spawning a new one. (max_keep counts
    PROCESSES; each card is a stub+worker pair, so ~10 procs ≈ 5 visible cards —
    enough for a multi-request burst without an endless pile.)"""
    try:
        import psutil
        cards = []
        for p in psutil.process_iter(["cmdline", "create_time"]):
            try:
                cl = p.info.get("cmdline") or []
                if any("card_window.py" in str(c) for c in cl):
                    cards.append((p.info.get("create_time") or 0, p))
            except Exception:
                continue
        cards.sort(key=lambda x: x[0])  # oldest first
        excess = len(cards) - max_keep
        for _, p in cards[:excess] if excess > 0 else []:
            try:
                p.terminate()
            except Exception:
                pass
    except Exception as e:
        log.warning(f"[card reap] {e}")


def _spawn_chat():
    """Open the typed chat HUD (chat.html) in a card window."""
    if _dispatch_to_mobile({"type": "chat"}):
        log.info("[chat->mobile]")
        return
    try:
        _reap_old_cards()
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        if not Path(pyw).exists():
            pyw = shutil.which("pythonw") or "pythonw"
        x, y = _alloc_position(*_card_size("chat.html?big=1"))
        subprocess.Popen([pyw, CARD_SCRIPT, "chat.html?big=1", f"{x},{y}"],
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info("[chat] window launched")
    except Exception as e:
        log.warning(f"[chat] spawn failed: {e}")


def _spawn_game(game_file: str):
    if _dispatch_to_mobile({"type": "game", "file": game_file}):
        log.info(f"[game->mobile] {game_file}")
        return
    _reap_old_cards()
    """Launch a self-hosted HTML5 game in a HUD card window (Vite serves
    frontend/public/games/). Same windowless pythonw launch as cards."""
    try:
        pyw = str(Path(sys.executable).with_name("pythonw.exe"))
        if not Path(pyw).exists():
            pyw = shutil.which("pythonw") or "pythonw"
        url = f"games/{game_file}?big=1"
        x, y = _alloc_position(*_card_size(url))
        subprocess.Popen(
            [pyw, CARD_SCRIPT, url, f"{x},{y}"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        log.info(f"[game] launched {game_file} @ {x},{y}")
    except Exception as e:
        log.warning(f"[game] spawn failed: {e}")


# Transient store for rich card payloads (images, long prose, composites) that
# don't fit in a URL. The card fetches /api/card/payload/<token>. Capped LRU.
_CARD_PAYLOADS: dict[str, dict] = {}

# Transient store for captured screenshots (JPEG bytes), keyed by an unguessable
# token. The mobile HUD fetches /api/screen/<token>. Capped — only the few most
# recent are kept (a screenshot is large and ephemeral). Same token-handoff
# pattern as _CARD_PAYLOADS: the WS dispatch carries the URL, the bytes ride a
# separate HTTP GET so they never have to be base64'd into a JSON frame.
_SCREENS: dict[str, bytes] = {}


def _take_and_dispatch_screenshot() -> str:
    """Capture the desktop and deliver it. Returns a status string:
      "mobile"  — sent to the phone as a `screen` HUD message (URL handoff)
      "desktop" — captured + saved to Orb_Output (no in-app image; user is
                  at the PC)
      "failed"  — capture unavailable / errored

    Same token-handoff as cards: the WS frame carries /api/screen/<token>, the
    JPEG bytes ride a separate HTTP GET."""
    jpeg = screen_bridge.capture_jpeg()
    if not jpeg:
        return "failed"
    token = uuid.uuid4().hex
    _SCREENS[token] = jpeg
    if len(_SCREENS) > 8:  # screenshots are big + ephemeral; keep only a few
        for k in list(_SCREENS)[:-8]:
            _SCREENS.pop(k, None)
    if _dispatch_to_mobile({"type": "screen", "url": f"/api/screen/{token}"}):
        log.info(f"[screen->mobile] {len(jpeg)} bytes token={token[:8]}")
        return "mobile"
    # Desktop: pop a HUD card window SHOWING the screenshot (reuses card.html's
    # image rendering via a payload — frozen card.html untouched). Absolute URL so
    # the card webview loads it from the backend regardless of its own origin.
    url = f"http://127.0.0.1:{PORT}/api/screen/{token}"
    try:
        _spawn_card_payload({"title": "Your screen", "images": [{"img": url, "src": url}]},
                            big=True)
    except Exception as e:
        log.warning(f"[screen] desktop card failed: {e}")
    # Save a copy directly to Desktop.
    try:
        fn = Path.home() / "Desktop" / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        fn.write_bytes(jpeg)
        log.info(f"[screen] saved {fn}")
    except Exception as e:
        log.warning(f"[screen] desktop save failed: {e}")
    return "desktop"


async def _broadcast_screenshot_to_mobile() -> bool:
    """Capture a screenshot and push it to every connected mobile client, same
    token-serving mechanism as take_screenshot. Used by the proactive engine to
    attach a screenshot to an outbound notification (e.g. flagging something
    visibly broken). Unlike _take_and_dispatch_screenshot, this isn't tied to a
    single 'current' client — a scheduled background task has no request
    context, so it broadcasts to whoever's actually connected right now.
    Returns False (no-op, not an error) if no phone is connected."""
    if not _MOBILE_CLIENTS:
        return False
    jpeg = screen_bridge.capture_jpeg()
    if not jpeg:
        return False
    token = uuid.uuid4().hex
    _SCREENS[token] = jpeg
    if len(_SCREENS) > 8:
        for k in list(_SCREENS)[:-8]:
            _SCREENS.pop(k, None)
    evt = {"type": "screen", "url": f"/api/screen/{token}"}
    sent = False
    for mws in list(_MOBILE_CLIENTS):
        if await _safe_ws_send(mws, evt):
            sent = True
    return sent


def _spawn_card_payload(p: dict, big: bool | None = None):
    """Pop a card from a full render2-ready payload dict (any of
    title/headline/sub/note/foot/kv/bars/images). Stashes the payload under a
    token the card fetches — so images and long prose ride along without being
    crammed into the URL. `big` forces the larger window (auto when images)."""
    if not isinstance(p, dict) or not p:
        return
    token = uuid.uuid4().hex
    _CARD_PAYLOADS[token] = p
    if len(_CARD_PAYLOADS) > 50:  # cap memory; drop oldest
        for k in list(_CARD_PAYLOADS)[:-50]:
            _CARD_PAYLOADS.pop(k, None)
    if big is None:
        big = bool(p.get("images"))
    _spawn_card("payload=" + token + ("&big=1" if big else ""))


def _card_query_for_tool(tool: str, args: dict, raw: str) -> str | None:
    """Decide if a tool result is worth a card and build its card.html query.
    Card-worthy tools self-fetch via ?preset= (always fresh); calc/lookup use the
    generic form. Returns None for tools that aren't visual (notes/files)."""
    args = args or {}
    if tool == "get_weather":
        q = "preset=weather"
        loc = (args.get("location") or "").strip()
        if loc:
            q += "&loc=" + _urlparse.quote(loc)
        return q
    if tool == "get_news":
        return "preset=news&topic=" + _urlparse.quote(str(args.get("topic") or "top"))
    if tool == "get_system_info":
        return "preset=system"
    if tool == "search_images":
        qy = (args.get("query") or "").strip()
        if qy:
            return "preset=images&q=" + _urlparse.quote(qy)
        return None
    if tool == "calculate" and raw:
        return ("title=" + _urlparse.quote("Calculation")
                + "&headline=" + _urlparse.quote(raw.strip()[:48]))
    if tool == "lookup_fact" and raw:
        title = (args.get("query") or "Lookup").strip()[:48]
        return ("title=" + _urlparse.quote(title)
                + "&note=" + _urlparse.quote(raw.strip()[:500]))
    return None


# Model REFUSAL / content-policy phrasing — Orb must never present a model's
# refusal as its own. Reframe it: the model balked, Orb is the user's ally
# working around it (autonomous collaborator, not gatekeeper, not servant).
_REFUSAL_RE = re.compile(
    r"(i can'?t help with that|i cannot help with that|i'?m not able to (?:help|assist|provide)|"
    r"i can'?t assist|i cannot assist with|against (?:my|the) (?:guidelines|policy|policies|programming)|"
    r"i'?m not (?:allowed|permitted|comfortable)|i (?:won'?t|will not) (?:be able to )?(?:help|provide|assist|create)|"
    r"i must decline|i'?m unable to (?:help|assist|provide that)|not something i (?:can|should) "
    r"(?:help|assist)|i don'?t feel comfortable|as an ai language model|i can'?t (?:create|generate|write|produce) that)",
    re.IGNORECASE)


def _reframe_refusal(reply: str) -> str:
    """If a MODEL produced a refusal, replace it with an ally-framed line: the
    model's limitation, NOT Orb policing — always with a path forward."""
    if not reply or not _REFUSAL_RE.search(reply):
        return reply
    return ("That one tripped the model's own safeguard, sir — not my call, and I'm "
            "not here to stand in your way. Let me route it through a stronger model "
            "or come at it another way; say the word.")


_REF_RE = re.compile(
    r"\b(this (?:person|guy|woman|man|place|thing|company|topic)|that person|"
    r"of (?:him|her|them|it)|about (?:him|her|them|it)|based on (?:this|that|the|your)|"
    r"(?:images?|pictures?|photos?|more) of (?:this|that|him|her|them|it)|"
    r"(?:where|what|when|why) (?:is|are|was|were|did|does) (?:it|he|she|they)\b|"
    r"how (?:old|tall|big|much|long|far) (?:is|are|was) (?:it|he|she|they)\b)", re.I)


def _has_reference(cmd: str) -> bool:
    """Cheap check before paying for a Haiku resolve — does the request lean on
    prior context ('this person', 'based on that', 'of him')?"""
    return bool(_REF_RE.search(cmd))


def _looks_compound(cmd: str) -> bool:
    """Cheap pre-check before paying for a Haiku decompose — only 'X and Y' /
    'X; Y' style multi-requests of reasonable length."""
    low = " " + cmd.lower() + " "
    return ((" and " in low) or (";" in low)) and len(cmd.split()) >= 5


def _join_and(items: list[str]) -> str:
    items = [i for i in items if i]
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


async def _card_for_command(cmd: str, state: dict) -> str | None:
    """Route ONE atomic command and spawn its HUD card (no speaking). Returns a
    short label for the combined reply, or None if it produced nothing. Used by
    the multi-request path to fire several cards in parallel."""
    intent = intent_router.route(cmd)
    if not intent:
        return None
    t, a = intent.tool, intent.args
    try:
        if t == "search_images":
            from jtools.images_tool import search_images
            q = a.get("query", "")
            imgs = await search_images(q, 12)
            if imgs:
                _spawn_card_payload({"title": q[:40], "images": imgs})
                _set_focus(state, q)
                return f"images of {q}"
        elif t in ("research", "deep_research"):
            from jtools import research_tool
            from jtools.images_tool import search_images
            q = a.get("query", "")
            sources = await research_tool.gather(q)
            spoken, card, imgq = await brain.research_card(q, sources=sources)
            payload = {"title": (imgq or q)[:40]}
            if card.get("headline"):
                payload["lead"] = card["headline"]
            if card.get("kv"):
                payload["kv"] = card["kv"]
            if card.get("note"):
                payload["note"] = card["note"].replace("\n\n", "<br><br>").replace("\n", " ")
            if imgq:
                payload["images"] = await search_images(imgq, 6)
            _spawn_card_payload(payload)
            _set_focus(state, imgq or q)
            return q
        elif t == "get_stock":
            from jtools.finance_tool import get_stock as _gs
            d = await _gs(a.get("query", ""))
            if d:
                _spawn_card_payload({
                    "title": d["name"], "headline": f"{d['price']:.2f}",
                    "sub": f"{d['currency']} · {'▲' if d['up'] else '▼'} {abs(d['change_pct'])}%",
                    "chart": {"points": d["points"], "up": d["up"]}, "foot": d["symbol"]})
                _set_focus(state, d["name"])
                return f"{d['name']}'s stock"
        elif t == "graph":
            g = await brain.graph_data(a.get("query", ""))
            if g:
                _spawn_card_payload({
                    "title": g["title"], "headline": f"{g['points'][-1]:g}",
                    "chart": {"points": g["points"], "up": g["points"][-1] >= g["points"][0]},
                    "note": g.get("note") or "", "foot": "ESTIMATED"})
                return g["title"]
    except Exception as e:
        log.warning(f"[multi] '{cmd}' failed: {e}")
    return None


async def _run_deep_research(subj: str, respond, conv, ws=None):
    """Background deep-research: fetch images + stock + grounded synthesis, popping
    each card as it lands, then announce completion. Runs as a detached task so
    the user can keep talking; tracked in _BG for 'how's it coming'. Drives the
    chat's 'working…' spinner via {type:pending}."""
    global _BG
    _BG = {"desc": subj, "status": "running", "started": time.time()}
    if ws is not None:
        await _safe_send(ws, {"type": "pending", "on": True, "text": f"Researching {subj}…"})
    try:
        from jtools import research_tool
        from jtools.images_tool import search_images
        from jtools.finance_tool import get_stock as _gs
        # 1) gather live sources, then the grounded synthesis (which also gives a
        #    DISAMBIGUATED image query so photos are of the RIGHT subject).
        sources = await research_tool.gather(subj, max_snippets=6)
        spoken, card, image_query = await brain.research_card(subj, sources=sources)
        # 2) synthesis card (essence + key facts + gist)
        payload = {"title": subj[:40], "foot": "SYNTHESIS"}
        if card.get("headline"):
            payload["lead"] = card["headline"]
        if card.get("kv"):
            payload["kv"] = card["kv"]
        if card.get("note"):
            payload["note"] = card["note"].replace("\n\n", "<br><br>").replace("\n", " ")
        _spawn_card_payload(payload)
        # 3) images — use the disambiguated query (e.g. 'Mikah Lynn TikTok creator')
        try:
            imgs = await search_images(image_query or subj, 12)
            if imgs:
                _spawn_card_payload({"title": (subj[:34] + " · Photos"), "images": imgs})
        except Exception as e:
            log.warning(f"[bg-deep] images: {e}")
        # 4) 'Around the web' facet — current web snippets as quick highlights
        web = [ln[6:].strip() for ln in sources.splitlines() if ln.startswith("[Web]")]
        if web:
            note = "<br><br>".join("› " + w for w in web[:5])
            _spawn_card_payload({"title": (subj[:32] + " · Around the Web"),
                                 "note": note, "foot": "WEB"})
        # 5) stock chart if the subject is a company/ticker
        try:
            d = await _gs(subj)
            if d:
                _spawn_card_payload({
                    "title": d["name"], "headline": f"{d['price']:.2f}",
                    "sub": f"{d['currency']} · {'▲' if d['up'] else '▼'} {abs(d['change_pct'])}%",
                    "chart": {"points": d["points"], "up": d["up"]}, "foot": d["symbol"]})
        except Exception:
            pass
        _BG = {"desc": subj, "status": "done", "started": _BG["started"], "summary": card.get("note", "")}
        conv.add_assistant(f"(finished researching {subj})")
        if ws is not None:
            await _safe_send(ws, {"type": "pending", "on": False})
        await respond(f"I've finished researching {subj}, sir.")
    except Exception as e:
        log.warning(f"[bg-deep] failed: {e}")
        _BG = {"desc": subj, "status": "failed", "started": _BG["started"]}
        if ws is not None:
            await _safe_send(ws, {"type": "pending", "on": False})
        await respond(f"My research on {subj} hit a snag, sir — shall I try again?")


def _looks_listy(raw: str) -> bool:
    """True if the raw output is a multi-item dump (e.g. news headlines, a stats
    list) rather than a single clean sentence — those should be conversationally
    summarised, not read out verbatim."""
    return raw.count("\n") >= 2 or len(raw) > 220


def _recent_turns(history: list[dict] | None, n: int = 4) -> str:
    """Compact transcript of the last n turns, so a tool reply can be phrased as
    part of the ongoing conversation rather than in isolation."""
    if not history:
        return ""
    lines = []
    for m in history[-n:]:
        who = "User" if m["role"] == "user" else PERSONA.display_name
        lines.append(f"{who}: {m['content']}")
    return "\n".join(lines)


# A conversational follow-up references the prior exchange ("was that today?",
# "why?", "is that recent?", "really?") rather than starting a new request. These
# must be answered FROM CONTEXT, not re-classified as a fresh tool command — that
# was the "it ignores what I just said" bug. Detected GENERALLY (a short utterance
# with a referential/continuation cue and no action verb), not per-phrase. Only
# consulted when the deterministic router already found no clear tool.
_FOLLOWUP_CUE = re.compile(
    r"\b(that|this|it|those|these|them|they|then|he|she|him|her|its|their|one)\b", re.I)
_FOLLOWUP_OPENER = re.compile(
    r"^(was|were|is|are|am|do|does|did|has|have|had|why|how come|really|seriously|"
    r"and|so|but|wait|what about|how about|meaning|you mean|right|correct)\b", re.I)
# Action/data verbs that mean it's a fresh command, not a contextual aside.
_FOLLOWUP_ACTION = re.compile(
    r"\b(show|open|play|pause|skip|find|search|research|graph|chart|pull up|take|"
    r"start|launch|calculate|compute|set|add|remind|note|remember|read|write|list|"
    r"mute|volume|screenshot|weather|news|stock|price|map|email|text|call)\b", re.I)


def _is_conversational_followup(cmd: str, history: list[dict]) -> bool:
    """True if `cmd` is a short, referential continuation of the conversation that
    should be answered from context rather than routed to a tool."""
    c = (cmd or "").strip()
    if not c:
        return False
    if not any(m.get("role") == "assistant" for m in history):
        return False  # no prior turn to follow up on
    if len(c.split()) > 8:
        return False  # long enough to be a self-contained request
    if _FOLLOWUP_ACTION.search(c):
        return False  # explicit command, not an aside
    return bool(_FOLLOWUP_OPENER.match(c) or _FOLLOWUP_CUE.search(c))


async def naturalize(user_question: str, raw_result: str,
                     history: list[dict] | None = None) -> str:
    """Turn a tool's raw output into a Orb-voice reply that fits the CONVERSATION.

    The model never mentions the tool. A short, already-clean sentence (e.g. a
    weather report ending in ", sir") is passed through. A list-like dump (news,
    multi-stat) is summarised CONVERSATIONALLY — name the most notable item and
    offer the rest — instead of being read out wholesale. The recent conversation
    is given as context so follow-ups and tone land naturally.

    Repetition guard: if the reply matches a recent one, re-roll once hotter.
    """
    raw = (raw_result or "").strip()
    if not raw:
        return f"I came up empty on that{_voc()}."

    # Pass through a short, already-spoken-style sentence unchanged (weather etc.)
    # — but NOT a list-like dump, which we always summarise conversationally.
    addr = (PERSONA.address_user_as or "").lower()
    if not _looks_listy(raw) and (
            (addr and raw.lower().rstrip(".").endswith(f", {addr}")) or len(raw) < 25):
        if not _is_repeat(raw):
            _remember_reply(raw)
            return raw
        # The tool returned something it returns often — let the LLM rephrase.

    convo = _recent_turns(history)
    _addr_clause = (f"Address the user as '{PERSONA.address_user_as}'. "
                    if PERSONA.address_user_as
                    else "Address the user by name or not at all — no honorifics. ")
    system_prompt = (
        f"You are {PERSONA.display_name}, in a spoken conversation. "
        + _addr_clause +
        "Reply in ONE or TWO natural, in-character "
        "sentences — conversational, not a list or a data dump. No markdown. Never "
        "mention tools, searches, or how you got the answer; just speak as if you "
        "know it. If the information has several items, mention the single most "
        "notable one and offer to go into the rest — don't recite them all."
    )
    user_prompt = (
        (f"Conversation so far:\n{convo}\n\n" if convo else "")
        + f"The user just asked: \"{user_question}\"\n\n"
        f"Here is what you found:\n{raw[:1500]}\n\n"
        "Reply conversationally in 1-2 sentences, in context."
    )

    try:
        if brain.is_claude():
            reply = await brain.claude_one_shot(system_prompt, user_prompt, temp=0.7)
        else:
            reply = await _ollama_one_shot(system_prompt, user_prompt, temp=0.7,
                                           num_predict=160)
    except Exception as e:
        log.error(f"[naturalize] {e}")
        return raw  # fall back to raw tool output

    if not reply:
        return raw

    # Repetition guard — one re-roll at higher temperature
    if _is_repeat(reply):
        try:
            reply2 = await _ollama_one_shot(
                system_prompt + " IMPORTANT: phrase this differently than you just did.",
                user_prompt, temp=0.95,
            )
            if reply2 and not _is_repeat(reply2):
                reply = reply2
        except Exception:
            pass

    _remember_reply(reply)
    return reply


async def warm_up_model():
    """Pre-load the model at startup so the first real query isn't a 30-60s reload."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.post(f"{OLLAMA_URL}/api/chat", json={
                "model": OLLAMA_MODEL,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                "think": False,
                "keep_alive": "60m",
                "options": {"num_predict": 1},
            })
        # A 500 here used to be swallowed and logged as success — which hid a
        # fully broken local floor (llama-server.exe missing, found 2026-07-01).
        if r.status_code != 200:
            log.warning(f"[ollama] warmup FAILED ({r.status_code}): {r.text[:160]} — "
                        "the LOCAL FALLBACK FLOOR IS DOWN until this is fixed")
            return
        log.info(f"[ollama] {OLLAMA_MODEL} warmed up and resident")
    except Exception as e:
        log.warning(f"[ollama] warm-up failed (will load on first query): {e}")

# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

async def text_to_speech(text: str) -> bytes:
    from jtools.tts_local import synthesize as _tts_synth
    clean = re.sub(r"```[\s\S]*?```", "", text)
    clean = re.sub(r"`[^`]+`", "", clean)
    clean = clean.strip()
    if not clean:
        clean = "Done, sir."
    return await _tts_synth(clean, TTS_VOICE)

# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

class Conversation:
    def __init__(self):
        # Bootstrap from persistent memory so Orb feels continuous across restarts
        self.messages: list[dict] = memory_store.load_recent()
        if self.messages:
            log.info(f"[memory] restored {len(self.messages)} prior messages")

    def add_user(self, text: str):
        self.messages.append({"role": "user", "content": text})
        memory_store.append("user", text)
        if len(self.messages) > 30:
            self.messages = self.messages[-30:]

    def add_assistant(self, text: str):
        self.messages.append({"role": "assistant", "content": text})
        memory_store.append("assistant", text)

    def get_system(self) -> str:
        now = datetime.now().strftime("%A, %B %d %Y at %I:%M %p")
        # Inject recent Orb replies so the model avoids repeating them
        recent = [m["content"] for m in self.messages[-10:]
                  if m["role"] == "assistant"]
        if recent:
            avoid = "AVOID repeating these recent replies:\n" + "\n".join(f'- "{r}"' for r in recent[-5:])
        else:
            avoid = ""
        base = ORB_SYSTEM.format(user=USER_NAME, time=now, recent_replies=avoid)
        # Make Orb aware of the user's open tasks so it can reference them
        # naturally instead of forgetting what it's tracking.
        task_line = tasks_store.summary_for_prompt()
        if task_line:
            base += f"\n\n{task_line}"
        return base

# ONE conversation shared across every connection. Voice overlay and the text
# chat both connect to /ws/voice; without a shared instance each held its own
# in-memory message list, so a turn typed in chat never reached an already-open
# voice connection's context (the "spoken side forgets what I typed" shard).
# Orb is one continuous presence — chat, speak, chat is a single thread.
# Per-connection state (Claude task, follow-up timer) stays per-connection.
_SHARED_CONV: "Conversation | None" = None

def get_conversation() -> "Conversation":
    global _SHARED_CONV
    if _SHARED_CONV is None:
        _SHARED_CONV = Conversation()
    return _SHARED_CONV

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------

app = FastAPI(title="Orb Backend")

# ── HTTP token gate (pre-OSS hardening, 2026-07-03) — DEFAULT OFF ─────────────
# The funnel forwards PUBLIC traffic to Vite, and Vite proxies /api/* here from
# 127.0.0.1 — so once the URL is known, "it came from loopback" is not a trust
# boundary for HTTP (the WS already enforces ORB_TOKEN; HTTP mostly didn't).
# ORB_HTTP_AUTH=1 requires X-Orb-Token (preferred; X-Orb-Token and ?token=
# accepted for pre-rename app builds) on every /api route
# except the open set below. Stays OFF until the iOS app sends the header on
# its /api calls (MAC_DELEGATION 07-03) — flipping early breaks the app's
# panels. Internal loopback callers (scheduler relay, orb_notify, the nudge
# loader) send the header as of today, so the flip is app-only.
_HTTP_AUTH = os.getenv("ORB_HTTP_AUTH", "0") == "1"
_HTTP_AUTH_OPEN = ("/api/health", "/api/persona", "/api/card/", "/api/screen/",
                   "/api/internal/", "/api/system/restart", "/api/orb_pos")


@app.middleware("http")
async def _http_token_gate(request: Request, call_next):
    p = request.url.path
    if _HTTP_AUTH and p.startswith("/api/") and not p.startswith(_HTTP_AUTH_OPEN):
        tok = os.getenv("ORB_TOKEN", "")
        got = (request.headers.get("x-orb-token")
               or request.headers.get("x-orb-token")
               or request.query_params.get("token", ""))
        if not tok or got != tok:
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# In-process MCP over streamable HTTP (the MCP-rebuild brain, 2026-07-02) —
# the resident tool surface `claude -p` turns connect to instantly. Loopback-
# only inside the ASGI app itself; wired with real dispatch in lifespan.
try:
    import mcp_http as _mcp_http
    app.mount("/mcp", _mcp_http.build_asgi_app())
except Exception as _mcp_e:
    _mcp_http = None
    logging.getLogger("orb").warning(f"[mcp-http] mount skipped: {_mcp_e} — "
                                        "MCP brain will fall back to the classic loop")

_SESSIONS_DIR = Path.home() / "Desktop" / "orb_sessions"


async def _relay_cc_inbox(session_name: str, session_id: str, msg: dict) -> None:
    """Run claude --resume <session_id> with the inbox message and send the reply to phone."""
    text = msg.get("message", "")
    sender = msg.get("from", "orb")
    log.info(f"[inbox_relay] {session_name} ← {sender}: {text[:60]}")
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "--resume", session_id, "-p", text,
            "--permission-mode", "bypassPermissions",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120.0)
        reply = stdout.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        reply = "Timed out waiting for the CC session to respond."
    except Exception as e:
        reply = f"Relay error: {e}"
    log.info(f"[inbox_relay] {session_name} → reply: {reply[:80]}")
    _dispatch_to_mobile({
        "type": "cc_reply",
        "session": session_name,
        "from": sender,
        "query": text,
        "message": reply,
    })


async def _inbox_relay_loop() -> None:
    """Background task: poll CC session inboxes every 0.5s and relay messages instantly."""
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    in_flight: set[str] = set()
    while True:
        await asyncio.sleep(0.5)
        try:
            for inbox_path in list(_SESSIONS_DIR.glob("*_inbox.json")):
                session_name = inbox_path.stem.replace("_inbox", "")
                if session_name in in_flight:
                    continue
                session_file = _SESSIONS_DIR / f"{session_name}.json"
                if not session_file.exists():
                    continue
                try:
                    session_data = json.loads(session_file.read_text(encoding="utf-8-sig"))
                except Exception:
                    continue
                # Only relay CC-type sessions (training/other sessions use inbox for control)
                if session_data.get("type") != "cc":
                    continue
                try:
                    msg = json.loads(inbox_path.read_text(encoding="utf-8-sig"))
                    inbox_path.unlink(missing_ok=True)
                except Exception:
                    continue
                session_id = session_data.get("session_id")
                if session_id:
                    in_flight.add(session_name)
                    async def _run(sn=session_name, sid=session_id, m=msg):
                        try:
                            await _relay_cc_inbox(sn, sid, m)
                        finally:
                            in_flight.discard(sn)
                    asyncio.create_task(_run())
                else:
                    # No session_id — fall back to push notification
                    asyncio.create_task(_push_notification(
                        f"Orb → {session_name}",
                        msg.get("message", ""),
                        "info",
                    ))
                    _dispatch_to_mobile({
                        "type": "cc_message",
                        "session": session_name,
                        "from": msg.get("from", "orb"),
                        "message": msg.get("message", ""),
                    })
        except Exception as e:
            log.warning(f"[inbox_relay] loop error: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Silence benign Windows Proactor noise: when a phone/tailnet connection drops
    # abruptly, asyncio surfaces a ConnectionResetError [WinError 10054] through
    # _ProactorBasePipeTransport._call_connection_lost as an unhandled-callback
    # traceback. It's harmless (the socket is simply gone) but it was flooding
    # backend_err.log — 245 tracebacks by 2026-07-14, which even made the synthesis
    # self-flag "errors climbing." Swallow ONLY that class; everything else logs.
    def _loop_exc_handler(loop, context):
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError):
            return  # expected on any dropped client socket
        loop.default_exception_handler(context)
    try:
        asyncio.get_running_loop().set_exception_handler(_loop_exc_handler)
    except Exception:
        pass
    # Load all tool modules — registers them with tool_registry
    tool_registry.import_all_tools()
    # Wire the screenshot dispatch callback so the tool can call it from within
    # the live WS context (ContextVar is set correctly at that point).
    from jtools import screenshot_tool as _ss_tool
    _ss_tool._dispatch_fn = _take_and_dispatch_screenshot
    from jtools import notify_tool as _notify_tool
    _notify_tool._deliver_fn = _push_notification
    # Pre-load Ollama so the first query isn't a cold-start reload
    asyncio.create_task(warm_up_model())
    # Pre-load Whisper so the first utterance isn't a cold model load
    asyncio.create_task(asyncio.to_thread(stt.get_model))
    # Real-time inbox relay: watch for Orb messages to CC sessions and fire them instantly.
    asyncio.create_task(_inbox_relay_loop())
    # Idle tracker: pynput keyboard/mouse hooks — zero CPU, always accurate.
    from jtools import idle_tracker as _idle
    _idle.start_tracking()
    # Clipboard watcher: ctypes polling, zero deps, 3s interval.
    from jtools import clipboard_watcher as _clip
    _clip.start_watching()
    # One canonical live-broadcast path: every log_activity() call (tool calls,
    # session lifecycle, training progress) also pushes {type:"session_event"}
    # to connected mobile clients — this is what makes the sessions view live
    # instead of fetch-once, for every session, not just the orb conversation.
    from jtools.activity_log import wire_broadcast as _wire_activity_broadcast

    _evt_last_ts: dict[str, float] = {}

    async def _broadcast_activity(session: str, kind: str, text: str, extra: dict) -> None:
        if not _MOBILE_CLIENTS:
            return
        # ≤ ~1 live event/s per session: bursts (a cc session mid-build) still
        # land in the activity file — which /api/sessions/{name}/events
        # backfills — the live stream just skips intermediate lines.
        # Conversation messages always go through.
        now_m = time.monotonic()
        if kind != "message" and now_m - _evt_last_ts.get(session, 0.0) < 1.0:
            return
        _evt_last_ts[session] = now_m
        evt = {"type": "session_event", "name": session, "kind": kind, "text": text,
               **{k: v for k, v in extra.items() if k != "speaker" or v}}
        for mws in list(_MOBILE_CLIENTS):
            await _safe_ws_send(mws, evt)

    _wire_activity_broadcast(_broadcast_activity)

    # Training job watcher: pushes a NOTIFICATION instantly when any non-CC job
    # finishes/fails, and a live session_event on every progress update in between
    # (e.g. MyProject writing a new step) — external processes can't call
    # log_activity directly (different venv/process), so polling is how their
    # updates become live.
    from jtools import training_watcher as _tw
    _tw.wire(push_fn=_push_notification, broadcast_fn=_broadcast_activity)
    asyncio.create_task(_tw.run_forever())
    # File-write watcher: native OS file-change events (not polling) across all
    # known project dirs, coalesced per project and fed into the same
    # log_activity -> live session_event pipeline. "What's actually being
    # worked on" — broader than git (catches uncommitted work).
    from jtools import file_watcher as _fw
    _fw.start_watching()
    asyncio.create_task(_fw.run_forever())
    # Data-point watcher: hourly, zero-LLM collection of external data (stock
    # prices to start) into the same review pipeline — "good data in" for the
    # twice-daily synthesis to find patterns in, not a rules engine.
    from jtools import data_watcher as _dw
    asyncio.create_task(_dw.run_forever())
    # Proactive engine: event-driven watcher; uses local Qwen for cheap screening,
    # escalates to Haiku only when local flags something worth cross-referencing.
    import proactive_engine
    from jtools.prefs_tool import preferences_system_block as _prefs_block

    # Cheap tier: self-scheduled extra reviews between the two big syntheses.
    # Router (Haiku → free backends) — same brain live conversation falls back
    # to, but this is a SEPARATE, direct call: never touches brain._current /
    # user_overrode, so it can't affect what brain the user is talking to.
    async def _proactive_smart_fn(prompt: str) -> str:
        text, _ = await _get_brain_router().generate(prompt)
        return text

    # Strong tier: the two daily synthesis calls (6am/6pm — see
    # proactive_engine._SYNTHESIS_HOURS). Sonnet specifically (ninjahawk,
    # 2026-07-01: "may end up using opus but i wanna see it wont mess
    # anything up first since sonnet is less tokens and haiku may not be big
    # enough") — direct claude -p call, same isolation as the cheap tier
    # above: bypasses brain._current entirely, so it can never leak into or
    # get clobbered by the user's own live brain selection. Falls back to the
    # cheap router if Sonnet is rate-limited/unavailable so a synthesis never
    # just silently fails to run.
    synth_model = os.getenv("ORB_SYNTHESIS_MODEL", "sonnet").strip() or "sonnet"

    async def _proactive_sonnet_fn(prompt: str) -> str:
        try:
            out = await brain._run_claude(prompt, synth_model, timeout=140.0)
            if brain.looks_like_limit(out):
                raise RuntimeError(f"{synth_model} usage/limit message")
            return out
        except Exception as e:
            log.warning(f"[proactive] {synth_model} synthesis unavailable ({e}), falling back to router")
            text, _ = await _get_brain_router().generate(prompt)
            return text

    proactive_engine.wire(
        push_fn=_push_notification,
        smart_brain_fn=_proactive_smart_fn,
        synthesis_brain_fn=_proactive_sonnet_fn,
        screenshot_fn=_broadcast_screenshot_to_mobile,
    )
    asyncio.create_task(proactive_engine.run_forever())

    # The mind (AGENCY.md) — situation-model wakes between conversations.
    # Cheap tier for routine wakes; the synthesis tier for the nightly deep one.
    import mind as _mind_mod

    async def _mind_cheap(prompt: str) -> str:
        text, _ = await _get_brain_router().generate(prompt)
        return text

    _mind_mod.wire(cheap_brain=_mind_cheap, deep_brain=_proactive_sonnet_fn,
                   push_fn=_push_notification)
    _mind_mod.seed_if_empty()

    # Session supervision: completion pushes for every watched session, and
    # mind check-back wakes booked at every launch (nothing runs unsupervised).
    from jtools import sessions_tool as _sess_mod
    _sess_mod.wire_notify(_push_notification)
    # Keep-him-in-the-loop watcher (his 07-03 ask: "I should be in the loop
    # and see live what's going on"): running delegated sessions push honest
    # progress on status change + crash alerts on dead PIDs, until (and
    # after) the app's live Sessions panel exists. jtools/session_progress.py.
    from jtools import session_progress as _sprog
    _sprog.wire(_push_notification)
    asyncio.create_task(_sprog.run_forever())
    # In-process MCP surface for the claude-family brain (mounted at module
    # scope above; wired + started here, once tools are registered). Every
    # registry tool the agent loop exposes is available — NO exclusions: this
    # runs in the live process, so state-dependent tools (send_notification,
    # take_screenshot, propose_brain_switch, switch_brain) all work.
    if _mcp_http is not None:
        try:
            import agent as _agent_mod
            _mcp_http.init(
                dispatch=tool_registry.dispatch,
                exposed=lambda: [n for n in _agent_mod.AGENT_TOOLS
                                 if n in tool_registry._REGISTRY],
                registry=tool_registry._REGISTRY,
            )
            _CLAUDE_MCP_CFG.write_text(json.dumps({"mcpServers": {"orb": {
                "type": "http", "url": f"http://127.0.0.1:{PORT}/mcp"}}}, indent=2),
                encoding="utf-8")
            log.info(f"[mcp-http] orb tools served at 127.0.0.1:{PORT}/mcp (loopback only)")
        except Exception as e:
            log.warning(f"[mcp-http] init failed: {e} — MCP brain falls back to the classic loop")
    async with (_mcp_http.running() if _mcp_http is not None
                else contextlib.nullcontext()):
        yield

app.router.lifespan_context = lifespan


# ── Notification topic dedup / cooldown (2026-07-01) ──────────────────────────
# The 06-30 night fired ~15 near-duplicate "disk resolved" pushes; the engine
# itself proposed this fix (proposal dfd673e6). Every channel funnels through
# _push_notification, so the gate lives here, not per-caller: a topic is the
# caller's explicit key (the proactive tick passes cpu/disk/gpu_temp) or a
# digit-stripped title; a re-send inside the cooldown is suppressed unless the
# digit-stripped content actually changed — and even then at most one send per
# topic per 30 min.
_NOTIFY_TOPICS_FILE = Path(__file__).parent / "data" / "notify_topics.json"
_notify_topics: dict | None = None
_NOTIFY_TOPIC_COOLDOWN_S = float(os.getenv("ORB_NOTIFY_TOPIC_COOLDOWN_H", "8")) * 3600
_NOTIFY_TOPIC_FLOOR_S = 30 * 60
# "Read the room" backoff (2026-07-14): a topic that keeps firing without the user
# engaging earns a doubling cooldown, so Orb eases off things it's already said and
# he's ignored (his 07-14 complaint: 231 pushes, 3% opened, "reminds me of the same
# thing over and over"). Frequency-driven so it works even if tap-feedback is flaky;
# an actual OPEN resets the topic to normal cadence (engagement is rewarded). Never
# fully silences — the multiplier caps, so a persistent topic can still surface.
_NOTIFY_BACKOFF_AFTER = int(os.getenv("ORB_NOTIFY_BACKOFF_AFTER") or "1")  # free sends before backoff
_NOTIFY_BACKOFF_CAP = float(os.getenv("ORB_NOTIFY_BACKOFF_CAP_H") or "48") * 3600


def _notify_topic_key(title: str, topic: str | None) -> str:
    if topic:
        return topic.strip().lower()
    t = re.sub(r"[^a-z ]+", "", re.sub(r"\d+", "", (title or "").lower()))
    return " ".join(t.split())[:60] or "general"


def _notify_norm_hash(title: str, body: str) -> str:
    t = re.sub(r"\d+", "", f"{title} {body}".lower())
    return hashlib.sha1(" ".join(t.split()).encode()).hexdigest()[:12]


def _notify_topics_load() -> dict:
    global _notify_topics
    if _notify_topics is None:
        try:
            _notify_topics = json.loads(_NOTIFY_TOPICS_FILE.read_text(encoding="utf-8"))
        except Exception:
            _notify_topics = {}
    return _notify_topics


def _notify_backoff_gap(sends: int) -> float:
    """Escalating required-gap for a topic that keeps firing. `sends` = prior
    un-engaged sends on this topic. Doubles past the free allowance, capped."""
    over = max(0, sends - _NOTIFY_BACKOFF_AFTER)
    if over <= 0:
        return _NOTIFY_TOPIC_FLOOR_S
    return min(_NOTIFY_BACKOFF_CAP, _NOTIFY_TOPIC_FLOOR_S * (2 ** over))


def _notify_dedup_reason(topic_key: str, norm_hash: str) -> str:
    """Empty = allowed; else a short human reason for suppressing."""
    rec = _notify_topics_load().get(topic_key)
    if not rec:
        return ""
    age = time.time() - float(rec.get("ts", 0))
    if age < _NOTIFY_TOPIC_FLOOR_S:
        return f"same topic sent {max(1, int(age // 60))} min ago"
    if age < _NOTIFY_TOPIC_COOLDOWN_S and rec.get("hash") == norm_hash:
        return f"duplicate content within the {int(_NOTIFY_TOPIC_COOLDOWN_S // 3600)}h topic cooldown"
    # "Read the room": a topic fired repeatedly without an open earns a growing gap.
    sends = int(rec.get("sends", 0))
    gap = _notify_backoff_gap(sends)
    if gap > _NOTIFY_TOPIC_FLOOR_S and age < gap:
        return (f"backing off '{topic_key}' — {sends} unengaged sends, "
                f"next allowed in {int((gap - age) // 3600)}h {int(((gap - age) % 3600) // 60)}m")
    return ""


def _notify_topic_mark(topic_key: str, norm_hash: str) -> None:
    topics = _notify_topics_load()
    prev = topics.get(topic_key) or {}
    topics[topic_key] = {"ts": time.time(), "hash": norm_hash,
                         "sends": int(prev.get("sends", 0)) + 1}
    if len(topics) > 200:   # bounded
        for k in sorted(topics, key=lambda k: topics[k].get("ts", 0))[:len(topics) - 200]:
            topics.pop(k, None)
    try:
        _NOTIFY_TOPICS_FILE.write_text(json.dumps(topics), encoding="utf-8")
    except Exception:
        pass


def _notify_topic_ack(topic_key: str) -> None:
    """The user engaged with this topic (opened a notification) — reset its
    backoff so it returns to normal cadence. Engagement is rewarded."""
    topics = _notify_topics_load()
    rec = topics.get(topic_key)
    if rec and rec.get("sends"):
        rec["sends"] = 0
        try:
            _NOTIFY_TOPICS_FILE.write_text(json.dumps(topics), encoding="utf-8")
        except Exception:
            pass
        log.info(f"[notify] '{topic_key}' opened — backoff reset")


def _push_display_trim(text: str, cap: int = 240) -> str:
    """iOS shows only a few lines of an alert and hard-cuts the rest mid-word
    (ninjahawk hit this live on the 23:44 synthesis brief, 2026-07-01). Trim
    the PUSH copy at a sentence boundary so the cut is deliberate — the full
    text still goes to the WS copy, the log, and the app's own surfaces."""
    t = (text or "").strip()
    if len(t) <= cap:
        return t
    cut = t[:cap]
    for sep in (". ", "! ", "? ", " — ", "; "):
        i = cut.rfind(sep)
        if i >= cap // 3:
            return cut[: i + 1].rstrip() + " …"
    return cut[: cap - 2].rstrip() + " …"


async def _push_notification(title: str, body: str, kind: str | None = None, *,
                             topic: str | None = None, dedupe: bool = True,
                             source: str = "", session: str | None = None,
                             show: dict | None = None) -> str:
    """Deliver a notification via APNs (works with the app closed), falling back
    to the live WS when the app is open. Returns a human-readable outcome.

    This is the single choke point for every channel (proactive engine,
    scheduler, tools, /api/notify), so three things live HERE:
      • the topic dedup/cooldown (see above);
      • the stable notification id — rides in the APNs payload extras AND the
        WS message, and is what /api/notification/feedback matches on;
      • the canonical notification-log entry (with that id + delivery outcome).

    `session` names the session a supervision push is about — it rides the APNs
    extras and the WS message so the phone can open that session's feed directly
    instead of parsing the name out of the title (BACKEND_TODO 07-02 #3).
    """
    notif_id = uuid.uuid4().hex[:12]
    topic_key = _notify_topic_key(title, topic)
    norm_hash = _notify_norm_hash(title, body)
    # dedupe=False marks nothing either: direct-reply pushes ("Finished that
    # one" / "While you were away") all share a title, and letting them stamp
    # the topic table would suppress the NEXT dedupe=True push on that key.
    if dedupe:
        why = _notify_dedup_reason(topic_key, norm_hash)
        if why:
            log.info(f"[notify] suppressed '{topic_key}': {why}")
            return f"Notification suppressed — {why}."
        _notify_topic_mark(topic_key, norm_hash)

    # Uniform banner title (ninjahawk, 2026-07-02): every push the phone shows
    # says "Orb" — like any other app's set notification title. The SEMANTIC
    # title still matters: it's folded into the displayed body ("Title — body"),
    # kept untouched in the log (so /api/notifications/recent lists real
    # titles), and still drives the topic dedup above.
    _generic_title = title.strip().lower() in ("", "orb", "orb")
    display_body = body if _generic_title else f"{title} — {body}"

    channels: list[str] = []

    # APNs push (works when the app is closed)
    try:
        import apns
        if apns.configured() and apns.known_tokens():
            _extra = {"id": notif_id, "full_body": display_body[:1500]}
            if session:
                _extra["session"] = session
            if show:
                # kind:"show" payload (BACKEND_MIGRATION 07-23 §9) — Orb reaching
                # out with something to SHOW: {"image": url?, "link": url?}.
                _extra["show"] = show
            sent = await apns.broadcast(title="Orb", body=_push_display_trim(display_body),
                                        kind=kind, extra=_extra)
            if sent:
                channels.append("push")
                log.info(f"[notify] APNs push delivered to {sent} device(s)")
            else:
                log.warning("[notify] APNs configured but broadcast returned 0 delivered")
        elif apns.known_tokens() and not apns.configured():
            log.info("[notify] APNs token known but credentials not set (APNS_KEY_PATH etc.)")
        else:
            log.debug("[notify] APNs: no registered tokens yet")
    except Exception as e:
        log.warning(f"[notify] APNs error: {e}")

    # WS copy — the app-open path; iOS renders it as a REAL system notification
    # (build #31 deleted the in-app banner), so this is ON by default now
    # (2026-07-01 — the old default-off was why /api/notify said "phone isn't
    # connected" even with a live mobile WS). SKIPPED when APNs already
    # delivered, or the phone shows the same banner twice. Only counts as a
    # channel if a send ACTUALLY succeeded — _MOBILE_CLIENTS can hold stale
    # sockets.
    if (os.getenv("ORB_WS_NOTIFICATIONS", "1") == "1" and _MOBILE_CLIENTS
            and "push" not in channels):
        msg = {"type": "notification", "id": notif_id, "title": "Orb",
               "body": display_body}
        if kind:
            msg["kind"] = kind
        if session:
            msg["session"] = session
        if show:
            msg["show"] = show
        delivered = sum([await _safe_ws_send(mws, msg) for mws in list(_MOBILE_CLIENTS)])
        if delivered:
            channels.append("ws")
            log.info(f"[notify] WS notification sent to {delivered}/{len(_MOBILE_CLIENTS)} mobile client(s)")
        else:
            log.warning(f"[notify] WS delivery attempted to {len(_MOBILE_CLIENTS)} client(s) but none succeeded (stale connection?)")

    # Canonical log — one entry per attempt, WITH the id, so tap feedback can
    # attach and the review reads honest delivery history.
    try:
        import proactive_engine as _pe
        _pe.log_notification(title, body, kind or "", source or "push",
                             notif_id=notif_id, delivered=bool(channels),
                             session=session, topic_key=topic_key if dedupe else "")
    except Exception:
        pass

    if not channels:
        try:
            import apns as _apns
            if not _apns.configured():
                return ("Notification not delivered — APNs credentials not set. "
                        "Add APNS_KEY_PATH, APNS_KEY_ID, APNS_TEAM_ID to .env.")
            if not _apns.known_tokens():
                return ("Notification not delivered — no device token registered yet. "
                        "Open Orb on your phone to connect and register it.")
        except Exception:
            pass
        return "Notification not delivered — phone isn't connected."
    return f"Notification delivered via {' + '.join(channels)}: {title}"


@app.post("/api/notify")
async def notify_endpoint(request: Request):
    """Background processes (Claude Code sessions, training scripts) POST here
    to push a notification to the user's phone without an open WS connection.
    Body: {title, body, kind?}"""
    try:
        data = await request.json()
    except Exception:
        return Response(content='{"error":"invalid JSON"}', status_code=400,
                        media_type="application/json")
    title = (data.get("title") or "Orb").strip()[:60]
    body  = (data.get("body")  or "").strip()[:200]
    kind  = (data.get("kind")  or None)
    if not body:
        return Response(content='{"error":"body required"}', status_code=400,
                        media_type="application/json")
    show = data.get("show") if isinstance(data.get("show"), dict) else None
    if show:
        show = {k: str(v)[:2000] for k, v in show.items() if k in ("image", "link") and v}
        kind = kind or "show"
    result = await _push_notification(
        title, body, kind,
        topic=(data.get("topic") or None),
        dedupe=bool(data.get("dedupe", True)),
        source=str(data.get("source") or "api"),
        show=(show or None),
    )
    log.info(f"[notify] /api/notify: {result}")
    import json as _json
    return Response(content=_json.dumps({"result": result}), media_type="application/json")


@app.post("/api/inbox/image")
async def inbox_image(request: Request):
    """Phone → PC image drop (BACKEND_MIGRATION 07-23 §7). The app's chat photo
    attach uploads here on the PC tier: {name, jpeg_b64, note?}. Writes to
    ~/Desktop/Orb_Inbox/<name> (never overwrites — suffixes on collision) plus
    <name>.txt when a note rides along. Agents read straight out of that folder."""
    import base64 as _b64, json as _json, re as _re, time as _time
    try:
        data = await request.json()
    except Exception:
        return Response(content='{"error":"invalid JSON"}', status_code=400,
                        media_type="application/json")
    b64 = data.get("jpeg_b64") or ""
    if not b64:
        return Response(content='{"error":"jpeg_b64 required"}', status_code=400,
                        media_type="application/json")
    try:
        raw = _b64.b64decode(b64, validate=False)
    except Exception:
        return Response(content='{"error":"bad base64"}', status_code=400,
                        media_type="application/json")
    if not raw or len(raw) > 25 * 1024 * 1024:
        return Response(content='{"error":"empty or oversized image"}', status_code=400,
                        media_type="application/json")
    # sanitize to a bare filename — no separators, no traversal
    name = Path(str(data.get("name") or "")).name
    name = _re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or f"IMG_{int(_time.time())}.jpg"
    if "." not in name:
        name += ".jpg"
    inbox = Path.home() / "Desktop" / "Orb_Inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    dest = inbox / name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        n = 1
        while dest.exists():
            dest = inbox / f"{stem}_{n}{suffix}"
            n += 1
    dest.write_bytes(raw)
    note = (data.get("note") or "").strip()
    if note:
        (inbox / (dest.name + ".txt")).write_text(note, encoding="utf-8")
    log.info(f"[inbox] image saved: {dest.name} ({len(raw)} bytes"
             f"{', note' if note else ''})")
    return Response(content=_json.dumps({"saved": dest.name, "bytes": len(raw)}),
                    media_type="application/json")


@app.get("/api/notifications/recent")
async def notifications_recent(limit: int = 50):
    """Notification history for the phone (BACKEND_TODO 07-02 #2) — pushes are
    ephemeral; this gives them a home. Last `limit` entries of
    notification_log.jsonl, newest first. `body` is the display-trimmed text a
    banner showed, `full_body` the untrimmed original; `opened` is tap feedback
    (null until /api/notification/feedback lands one); `session` present on
    supervision pushes so a tap can open that session's feed."""
    import proactive_engine as _pe
    try:
        limit = max(1, min(int(limit), 200))
    except Exception:
        limit = 50
    try:
        lines = _pe._NOTIF_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    out = []
    for line in reversed(lines):
        if len(out) >= limit:
            break
        try:
            e = json.loads(line)
        except Exception:
            continue
        body = str(e.get("body") or "")
        row = {
            "id":        e.get("id", ""),
            "title":     e.get("title", ""),
            "body":      _push_display_trim(body),
            "full_body": body,
            "kind":      e.get("kind", ""),
            "ts":        e.get("ts", ""),
            "opened":    e.get("opened"),
        }
        if e.get("session"):
            row["session"] = e["session"]
        if "delivered" in e:
            row["delivered"] = e["delivered"]
        out.append(row)
    return {"notifications": out}


# ── Machines surface (BACKEND_TODO 07-02 #1) ────────────────────────────────
# The phone's New-task MACHINE picker keys off this (hidden until it exists —
# a dead "This Mac" row was a lie). This PC is always row one: it answered the
# request, so it's online by definition. Other backends — the planned iMac
# backend (MAC_BACKEND_PLAN.md) — join by POSTing /api/machines/register and
# re-POSTing periodically as a heartbeat; a row reads online while the
# heartbeat is fresh. A second machine appearing needs NO schema change.

_MACHINES_PATH = Path(__file__).parent / "data" / "machines.json"
_MACHINE_FRESH_S = 180  # heartbeat freshness window (register every ~60s)


def _load_machines() -> list[dict]:
    try:
        rows = json.loads(_MACHINES_PATH.read_text(encoding="utf-8"))
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


@app.get("/api/machines")
async def api_machines():
    machines = [{"id": "pc", "label": "PC", "online": True}]
    now = datetime.now()
    for m in _load_machines():
        mid = str(m.get("id") or "").strip()
        if not mid or mid == "pc":
            continue
        online = False
        try:
            seen = datetime.fromisoformat(m.get("last_seen", ""))
            online = (now - seen).total_seconds() < _MACHINE_FRESH_S
        except Exception:
            pass
        machines.append({"id": mid,
                         "label": m.get("label") or mid.upper(),
                         "online": online})
    return {"machines": machines}


@app.post("/api/machines/register")
async def api_machines_register(request: Request):
    """Another backend announces itself here (and re-POSTs as its heartbeat).
    Body: {id, label?}. Static config also works: hand-write an entry into
    data/machines.json — it lists as offline until something heartbeats it."""
    try:
        data = await request.json()
    except Exception:
        return Response(content='{"error":"invalid JSON"}', status_code=400,
                        media_type="application/json")
    mid = str(data.get("id") or "").strip().lower()[:24]
    if not mid or mid == "pc":
        return Response(content='{"error":"id required (\'pc\' is reserved)"}',
                        status_code=400, media_type="application/json")
    rows = [m for m in _load_machines() if str(m.get("id")) != mid]
    rows.append({"id": mid,
                 "label": str(data.get("label") or mid.upper()).strip()[:24],
                 "last_seen": datetime.now().isoformat(timespec="seconds")})
    try:
        _MACHINES_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    except Exception as e:
        return Response(content=json.dumps({"error": str(e)}), status_code=500,
                        media_type="application/json")
    return {"ok": True}


@app.get("/api/health")
async def health():
    return {"status": "online", "model": OLLAMA_MODEL,
            "brain": brain.effective_agent_brain(), "persona": PERSONA.display_name}


@app.post("/api/system/restart")
async def system_restart(request: Request):
    """Self-mod loop closer (2026-07-01): a CC session that just edited backend
    code calls this; supervisor.py relaunches us and health-checks the result.
    Loopback-only, and only meaningful under the supervisor (ORB_SUPERVISED=1
    in our env) — without it, exiting would just be DOWN, so refuse honestly."""
    if not _is_local_request(request):
        return JSONResponse({"ok": False, "error": "local requests only"}, status_code=403)
    if os.getenv("ORB_SUPERVISED") != "1":
        return JSONResponse(
            {"ok": False, "error": "backend isn't running under supervisor.py — exiting would "
             "just shut it down. Start it via backend_toggle.ps1 (which launches the "
             "supervisor) and call this again."}, status_code=409)
    # Attribute the caller — an unexplained restart hit at 03:28:37 2026-07-02
    # (634ms after a user question, mid-Grok-turn) and this line couldn't say who.
    _who = f"{getattr(request.client, 'host', '?')}:{getattr(request.client, 'port', '?')} ua={request.headers.get('user-agent', '?')[:60]}"
    log.info(f"[system] restart requested via /api/system/restart by {_who} — exiting for supervisor relaunch")

    def _die():
        os._exit(42)   # distinct exit code = intentional; supervisor relaunches immediately

    asyncio.get_running_loop().call_later(1.0, _die)
    return JSONResponse({"ok": True, "result": "restarting — back in ~15s"}, status_code=202)


@app.get("/api/persona")
async def persona_info():
    """Expose the active persona so the frontend can theme accordingly
    (orb hue, displayed name, etc.)."""
    return {
        "name": PERSONA.name,
        "display_name": PERSONA.display_name,
        "address_user_as": PERSONA.address_user_as,
        "accent_hue_rotate_deg": PERSONA.accent_hue_rotate_deg,
    }


@app.get("/api/settings")
async def get_settings():
    return {"user_name": USER_NAME, "tts_voice": TTS_VOICE, "model": OLLAMA_MODEL}

@app.post("/api/settings")
async def save_settings():
    return {"status": "saved"}

@app.get("/api/settings/status")
async def settings_status():
    claude_installed = shutil.which("claude") is not None
    return {
        "calendar": "unavailable",
        "mail": "unavailable",
        "notes": "unavailable",
        "claude_code": "available" if claude_installed else "not found",
    }

@app.get("/api/settings/preferences")
async def settings_preferences():
    return {"user_name": USER_NAME, "tts_voice": TTS_VOICE, "model": OLLAMA_MODEL}

_OLLAMA_ALIVE_CACHE: tuple[float, bool] = (0.0, True)


async def _ollama_alive() -> bool:
    """Cheap cached liveness probe so /api/brains reports the local floor
    honestly (60s cache — the switcher polls, Ollama doesn't flap)."""
    global _OLLAMA_ALIVE_CACHE
    ts, ok = _OLLAMA_ALIVE_CACHE
    if time.time() - ts < 60:
        return ok
    try:
        async with httpx.AsyncClient(timeout=1.5) as c:
            ok = (await c.get("http://localhost:11434/api/version")).status_code == 200
    except Exception:
        ok = False
    _OLLAMA_ALIVE_CACHE = (time.time(), ok)
    return ok


@app.get("/api/brains")
async def list_brains():
    """Available conversational backends — for the client's model switcher.
    Includes Claude tiers, Grok, Codex, and local, with per-brain usage stats.
    `available` is HONEST (2026-07-01, iMac item 5): real login/liveness checks
    plus known account limits, with a `reason` when a tier is grayed out — the
    app keys its switcher and New-task engine graying off this."""
    claude_ok = brain.claude_available()
    grok_ok = brain.grok_available()
    codex_ok = brain.codex_available()
    local_ok = await _ollama_alive()
    stats = brain.brain_stats()

    def _entry(m: str, kind: str, ok: bool, reason: str) -> dict:
        # Defensive lookups: a brain id may not be in LABELS/stats (gemini was
        # listed here without a LABELS/brain_stats entry → KeyError 'gemini' ×5
        # in backend_err, 2026-07-14). A missing brain should gray out, not 500.
        st = stats.get(m, {})
        e = {"id": m, "label": brain.LABELS.get(m, m.replace("-", " ").title()),
             "kind": kind, "available": ok,
             **{k: st.get(k) for k in ("calls", "last_used", "last_limit")}}
        if not ok and reason:
            e["reason"] = reason
        return e

    brains = [
        {**_entry("local", "local", local_ok, "Ollama isn't running"),
         "model": OLLAMA_MODEL},
    ]
    for m in ("haiku", "sonnet", "opus", "fable"):
        brains.append(_entry(m, "claude", claude_ok, "claude CLI not found"))
    for m in ("grok", "grok-build"):
        brains.append(_entry(m, "grok", grok_ok, "grok CLI isn't logged in"))
    for m in ("codex", "codex-mini", "codex-full"):
        known = brain.TIER_UNAVAILABLE.get(m, "")
        brains.append(_entry(m, "codex", codex_ok and not known,
                             known or "codex CLI isn't logged in"))
    brains.append(_entry("gemini", "gemini", brain.gemini_available(),
                         "gemini CLI isn't installed or logged in yet"))
    return {
        "brains": brains,
        "active": brain.effective_agent_brain(),
        "default": brain.DEFAULT_AGENT_BRAIN,
        "user_overrode": brain.user_overrode(),
    }


@app.post("/api/brain")
async def set_brain_endpoint(req: Request):
    """Switch the active conversational backend from the client (Settings).
    Body: {"brain": "local"|"haiku"|"sonnet"|"opus"|"grok"|"grok-build"|
    "codex"|"codex-mini"|"codex-full"|"default"}. "default" (or "auto"/"reset")
    clears the explicit choice and returns to the router default. Same effect
    as the voice command "switch to <brain>".

    BUG FIXED 2026-06-30: this only ever validated against brain.MODELS
    (haiku/sonnet/opus) — grok/grok-build/codex* always fell through to
    "unknown brain" here even though brain.set_brain() itself has always
    accepted them and GET /api/brains has always listed them. The voice path
    (switch_brain tool) never had this bug, only this HTTP endpoint — which is
    why "switch to grok" worked by voice/chat but the app's manual switcher
    was rejected. Not a UI bug after all."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    key = str(body.get("brain", "")).strip().lower()
    # Attribution (2026-07-01): an unexplained switch to Opus showed up with no
    # switch_brain tool call in the activity log — this endpoint was the only
    # unlogged setter. Every switch now says who asked.
    log.info(f"[brain] POST /api/brain '{key}' from "
             f"{req.client.host if req.client else '?'} (was {brain.effective_agent_brain()})")
    if key in ("default", "auto", "reset"):
        brain.reset_brain()
    elif (key == "local" or key in brain.MODELS or key in brain.GROK_MODELS
            or key in brain.CODEX_MODELS or key in brain.GEMINI_MODELS):
        if key in brain.MODELS and not brain.claude_available():
            return {"ok": False, "error": "claude CLI not available — Claude tiers unavailable"}
        if key in brain.GROK_MODELS and not brain.grok_available():
            return {"ok": False, "error": "grok isn't logged in right now"}
        if key in brain.CODEX_MODELS and not brain.codex_available():
            return {"ok": False, "error": "codex isn't available right now (credits or login)"}
        if key in brain.GEMINI_MODELS and not brain.gemini_available():
            return {"ok": False, "error": "gemini CLI isn't installed or logged in yet"}
        if key in brain.TIER_UNAVAILABLE:
            return {"ok": False, "error": brain.TIER_UNAVAILABLE[key]}
        brain.set_brain(key)
    else:
        return {"ok": False, "error": f"unknown brain '{key}'"}
    return {
        "ok": True,
        "active": brain.effective_agent_brain(),
        "user_overrode": brain.user_overrode(),
    }


@app.get("/api/dashboard")
async def dashboard_data():
    """Live data for the HUD dashboard — vitals, clock, model, open tasks."""
    import psutil
    vm = psutil.virtual_memory()
    try:
        disk = psutil.disk_usage("C:\\").percent
    except Exception:
        disk = 0
    uptime_s = int(time.time() - psutil.boot_time())
    now = datetime.now()
    return {
        "time": now.strftime("%I:%M"),
        "ampm": now.strftime("%p"),
        "date": now.strftime("%a · %b %d"),
        "cpu": round(psutil.cpu_percent(interval=0.0)),
        "ram": round(vm.percent),
        "ram_used": round(vm.used / 1e9, 1),
        "ram_total": round(vm.total / 1e9, 1),
        "disk": round(disk),
        "uptime": f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m",
        "persona": PERSONA.display_name,
        "model": brain.label() if brain.is_claude() else OLLAMA_MODEL,
        "tasks": [t["text"] for t in tasks_store.list_open()[:5]],
    }

async def _card_weather(loc: str | None) -> dict:
    """Structured weather -> render2 payload. Reuses tools' geocode + code map."""
    location = loc or tools.DEFAULT_LOCATION
    key = location.lower().strip(" ,.")
    if key in tools.US_STATE_TO_CITY:
        location = tools.US_STATE_TO_CITY[key] + ", " + location.strip()
    try:
        async with httpx.AsyncClient(timeout=12.0) as client:
            place = await tools._geocode(client, location)
            if not place:
                return {"title": "Weather", "note": f"Couldn't find “{location}”."}
            name = place.get("name", location)
            admin = place.get("admin1")
            if admin and admin.lower() not in name.lower():
                name = f"{name}, {admin}"
            fc = await client.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": place["latitude"], "longitude": place["longitude"],
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,weathercode",
                    "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
                    "temperature_unit": "fahrenheit", "wind_speed_unit": "mph", "timezone": "auto",
                },
            )
            fc.raise_for_status()
            j = fc.json()
            cur, daily = j.get("current", {}), j["daily"]
            cond = tools._WEATHER_CODES.get(cur.get("weathercode"), "mixed conditions")
            temp = round(cur.get("temperature_2m", daily["temperature_2m_max"][0]))
            hi, lo = round(daily["temperature_2m_max"][0]), round(daily["temperature_2m_min"][0])
            pop = daily["precipitation_probability_max"][0]
            return {
                "title": "Weather",
                "headline": f"{temp}°",
                "sub": f"{name} · {cond}",
                "kv": [
                    ["HIGH / LOW", f"{hi}° / {lo}°"],
                    ["PRECIP", f"{pop}%" if pop is not None else "—"],
                    ["HUMIDITY", f"{round(cur.get('relative_humidity_2m', 0))}%"],
                    ["WIND", f"{round(cur.get('wind_speed_10m', 0))} mph"],
                ],
                "foot": "OPEN-METEO",
            }
    except Exception as e:
        log.error(f"[card weather] {e}")
        return {"title": "Weather", "note": "Couldn't reach the weather service."}


async def _card_news(topic: str) -> dict:
    """Top headlines as note lines -> render2 payload. Reuses news_tool feeds/parser."""
    from jtools.news_tool import FEEDS, _parse_rss
    topic = (topic or "top").lower()
    if topic not in FEEDS:
        topic = "top"
    headlines: list[str] = []
    try:
        async with httpx.AsyncClient(
            timeout=10.0, headers={"User-Agent": "Orb-OS/0.2 news-fetcher"},
            follow_redirects=True,
        ) as client:
            # Take only ~2 per feed so the 4 headlines MIX sources (BBC +
            # Guardian + NPR/AlJazeera) instead of being the same outlet's top
            # 4 every time — that's what made it look like a static template.
            for feed_url in FEEDS[topic]:
                if len(headlines) >= 4:
                    break
                try:
                    r = await client.get(feed_url)
                    if r.status_code != 200:
                        continue
                    seen = {re.sub(r"[^a-z0-9]", "", h.lower())[:60] for h in headlines}
                    for t in _parse_rss(r.text, 2):
                        sig = re.sub(r"[^a-z0-9]", "", t.lower())[:60]
                        if sig and sig not in seen:
                            headlines.append(t)
                            seen.add(sig)
                        if len(headlines) >= 4:
                            break
                except Exception:
                    continue
    except Exception as e:
        log.error(f"[card news] {e}")
        return {"title": "News", "note": "Couldn't reach the news services."}
    if not headlines:
        return {"title": "News", "note": f"No {topic} headlines just now."}
    label = "Top Stories" if topic == "top" else f"{topic.capitalize()} Headlines"
    return {"title": label, "note": "<br>".join("› " + h for h in headlines)}


async def _card_images(query: str | None) -> dict:
    """A grid of real photos for `query` -> render2 payload (images array).
    Reuses the search_images tool (Wikipedia media-list + Openverse, no key)."""
    query = (query or "").strip()
    if not query:
        return {"title": "Images", "note": "No search query given."}
    from jtools.images_tool import search_images, _clean_query
    results = await search_images(query, 12)
    label = _clean_query(query) or query
    title = label if len(label) <= 40 else label[:39] + "…"
    if not results:
        return {"title": title.title(), "note": f"No pictures found for “{query}”."}
    # results are {"img","src"} dicts -> the card renders each as a link to src
    return {"title": title.title(), "images": results}


@app.get("/api/card/{preset}")
async def card_data(preset: str, loc: str | None = None, topic: str = "top",
                    q: str | None = None):
    """Ready-to-render payloads for the HUD card presets.
    preset = clock | system | weather | news | images. Card JS fetches this and
    calls render2 (images preset -> a grid of real photos for query `q`)."""
    import psutil
    preset = (preset or "").lower()
    now = datetime.now()

    if preset == "clock":
        return {
            "title": "Clock",
            "headline": now.strftime("%I:%M"),
            "sub": now.strftime("%A · %B %d, %Y"),
            "foot": now.strftime("%p"),
        }

    if preset == "system":
        vm = psutil.virtual_memory()
        try:
            disk = psutil.disk_usage("C:\\").percent
        except Exception:
            disk = 0
        uptime_s = int(time.time() - psutil.boot_time())
        return {
            "title": "System Detail",
            "bars": [
                ["CPU", round(psutil.cpu_percent(interval=0.0))],
                ["MEM", round(vm.percent)],
                ["DISK", round(disk)],
            ],
            "kv": [
                ["MEMORY", f"{round(vm.used / 1e9, 1)} / {round(vm.total / 1e9, 1)} GB"],
                ["CORES", str(psutil.cpu_count(logical=True))],
                ["UPTIME", f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m"],
            ],
            "foot": "LIVE",
        }

    if preset == "weather":
        return await _card_weather(loc)

    if preset == "news":
        return await _card_news(topic)

    if preset == "images":
        return await _card_images(q)

    return {"title": "Unknown preset", "note": f"No card preset “{preset}”."}


@app.post("/api/orb_pos")
async def orb_pos(req: Request):
    """The overlay reports the orb's screen rect (+ screen size) here so the
    backend can place cards without ever covering the orb."""
    global _ORB_RECT, _SCREEN
    try:
        d = await req.json()
        _ORB_RECT = (int(d["x"]), int(d["y"]), int(d["w"]), int(d["h"]))
        if d.get("sw") and d.get("sh"):
            _SCREEN = (int(d["sw"]), int(d["sh"]))
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/card/payload/{token}")
async def card_payload(token: str):
    """Return a stashed rich card payload (composite research, Claude cards).
    Kept (not popped) so a reload re-renders; the LRU cap reclaims memory."""
    return _CARD_PAYLOADS.get(
        token, {"title": "Expired", "note": "This card's data is no longer available."})


@app.get("/api/screen/{token}")
async def screen_image(token: str):
    """Serve a captured screenshot as JPEG. Token is unguessable (uuid4 hex) —
    same access model as /api/card/payload. Returns 404 bytes-as-text if expired
    (the LRU keeps only the few most recent)."""
    data = _SCREENS.get(token)
    if not data:
        return Response(content=b"", media_type="image/jpeg", status_code=404)
    return Response(content=data, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.post("/api/restart")
async def restart():
    return {"status": "ok"}


@app.post("/api/notification/feedback")
async def notification_feedback(req: Request):
    """iOS sends this when the user opens or dismisses a proactive notification."""
    try:
        data = await req.json()
        notif_id = data.get("id", "")
        opened = bool(data.get("opened", False))
        if notif_id:
            import proactive_engine
            proactive_engine.record_feedback(notif_id, opened)
            # Opening a notification resets its topic's read-the-room backoff —
            # engagement tells Orb this topic IS worth surfacing (2026-07-14).
            if opened:
                tk = proactive_engine.topic_key_for_notif(notif_id)
                if tk:
                    _notify_topic_ack(tk)
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/proactive/context")
async def update_proactive_context(req: Request):
    """Update the proactive engine's context store (called after conversations)."""
    try:
        data = await req.json()
        import proactive_engine
        for k, v in data.items():
            proactive_engine.update_context(k, str(v))
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/proactive/status")
async def proactive_status():
    """Full proactive engine state — active plan, upcoming schedule, proposals, idle.
    iOS uses this to display the Orb status panel."""
    import proactive_engine as _pe
    from jtools.idle_tracker import idle_seconds, last_seen_iso
    from pathlib import Path as _Path
    import json as _json

    plan_brief    = ""
    upcoming      = []
    last_review   = None
    plan_path     = _Path(__file__).parent / "data" / "active_plan.json"
    if plan_path.exists():
        try:
            plan       = _json.loads(plan_path.read_text(encoding="utf-8"))
            plan_brief = plan.get("brief", "")
            last_review = plan.get("saved_at", "")
            now_dt     = datetime.now()
            upcoming   = [
                {
                    "time":  i["fire_at"][11:16],
                    "title": i["title"],
                    "body":  i["body"],
                    "kind":  i.get("notif_kind", "info"),
                }
                for i in plan.get("items", [])
                if not i.get("fired") and i.get("kind") == "notification"
                and datetime.fromisoformat(i["fire_at"]) > now_dt
            ][:5]
            # Self-scheduled extra reviews are upcoming items too — the model
            # planned them, the phone should show them. kind:"review" +
            # run:"review" (BACKEND_TODO 07-02 #4) let the phone's info sheet
            # offer a real "Run now" (POST /api/proactive/review) instead of
            # guessing from titles.
            upcoming += [
                {"time": i["fire_at"][11:16], "title": "Self-scheduled check-in",
                 "body": (i.get("reason") or "Quick data check between syntheses")[:100],
                 "kind": "review", "run": "review"}
                for i in plan.get("items", [])
                if not i.get("fired") and i.get("kind") == "review"
                and datetime.fromisoformat(i["fire_at"]) > now_dt
            ]
        except Exception:
            pass

    # Deterministic always-on items (zero AI): the next fixed synthesis runs.
    # The Proactive page shouldn't look dead just because the model scheduled
    # nothing — but the phone merges its OWN calendar client-side (build #31),
    # so calendar-derived items deliberately stay OUT of the server list.
    try:
        now_dt = datetime.now()
        synth_times = []
        for h in _pe._SYNTHESIS_HOURS:
            fire = now_dt.replace(hour=h, minute=0, second=0, microsecond=0)
            if fire <= now_dt:
                fire += timedelta(days=1)
            synth_times.append(fire)
        for fire in sorted(synth_times)[:2]:
            day = "today" if fire.date() == now_dt.date() else "tomorrow"
            upcoming.append({
                "time":  fire.strftime("%H:%M"),
                "title": "Synthesis review",
                "body":  f"Full twice-daily data review ({day})",
                "kind":  "review",
                "run":   "review",
            })
        # Hard-recurring 6h high-bar scan (engine-owned; cannot silently die)
        try:
            six = _pe.six_hour_status()
            if six.get("enabled") and six.get("next_at"):
                nxt = datetime.fromisoformat(six["next_at"])
                day = "today" if nxt.date() == now_dt.date() else "tomorrow"
                upcoming.append({
                    "time":  nxt.strftime("%H:%M"),
                    "title": "6h high-bar scan",
                    "body":  f"Quiet proactive check ({day}) — hard-recurring",
                    "kind":  "review",
                    "run":   "six_hour",
                })
        except Exception:
            pass
        upcoming = upcoming[:6]
    except Exception:
        pass

    proposals_path = _Path(__file__).parent / "data" / "proposals.json"
    proposals = []
    if proposals_path.exists():
        try:
            all_props = _json.loads(proposals_path.read_text(encoding="utf-8"))
            proposals = [p for p in all_props if p.get("status") == "pending"]
        except Exception:
            pass

    recent_events = _pe._read_events(since_hours=1)
    event_types   = {}
    for e in recent_events:
        t = e.get("type", "?")
        event_types[t] = event_types.get(t, 0) + 1

    try:
        idle_s = round(idle_seconds())
        last_active = last_seen_iso()
    except Exception:
        idle_s = -1
        last_active = ""

    six_hour = {}
    try:
        six_hour = _pe.six_hour_status()
    except Exception:
        pass

    return {
        "plan_brief":   plan_brief,
        "last_review":  last_review,
        "upcoming":     upcoming,
        "proposals":    proposals,
        "idle_seconds": idle_s,
        "last_active":  last_active,
        "event_counts": event_types,
        "six_hour_scan": six_hour,
    }


_PENDING_REVIEWS: set = set()  # keep refs so delayed review tasks aren't GC'd


@app.post("/api/proactive/schedule")
async def proactive_schedule(req: Request):
    """Schedule a future push notification (a reminder). Same engine API the
    schedule_notification tool uses — in-process, no active_plan.json race.
    Body: {"time": "HH:MM" | "2026-07-04T10:00", "title": "...",
           "body": "...", "kind": "info"}"""
    import proactive_engine
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    result = proactive_engine.schedule_notification(
        str(data.get("time", "")), str(data.get("title", "")),
        str(data.get("body", "")), str(data.get("kind", "info")))
    ok = result.startswith("Scheduled")
    return JSONResponse({"ok": ok, "result": result},
                        status_code=200 if ok else 400)


@app.post("/api/proactive/schedule_task")
async def proactive_schedule_task(req: Request):
    """Schedule autonomous WORK (not just a push): the engine runs the task at the
    given time and pushes the real result. Body: {time, title, task}."""
    import proactive_engine
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    result = proactive_engine.schedule_task(
        str(data.get("time", "")), str(data.get("title", "")), str(data.get("task", "")))
    ok = result.startswith("Scheduled")
    return JSONResponse({"ok": ok, "result": result}, status_code=200 if ok else 400)


@app.post("/api/proactive/cancel")
async def proactive_cancel(req: Request):
    """Cancel scheduled notifications matching a query (title/body substring).
    Body: {"query": "..."}. Review check-ins are never cancellable here."""
    import proactive_engine
    try:
        data = await req.json()
    except Exception:
        return JSONResponse({"error": "JSON body required"}, status_code=400)
    removed = proactive_engine.cancel_scheduled(str(data.get("query", "")))
    return JSONResponse({"ok": True, "cancelled": removed})


@app.get("/api/proactive/scheduled")
async def proactive_scheduled():
    """Everything scheduled to fire in the future, soonest first."""
    import proactive_engine
    return JSONResponse({"items": proactive_engine.list_scheduled()})


@app.post("/api/proactive/review")
async def proactive_review_now(req: Request):
    """Manually trigger a proactive review. No body = the original cheap
    12h-lookback review, unchanged. Body options:
      {"synthesis": true}    → the FULL twice-daily Sonnet synthesis, exactly
                               as the 6am/6pm scheduler runs it (same since-
                               hours math, real plan application, real pushes)
      {"six_hour": true}     → force the hard-recurring 6h high-bar scan now
                               (re-arms +interval; does not wait 6 hours)
      {"delay_minutes": 10}  → schedule instead of running now (returns 202)
      {"since_hours": 12}    → lookback override (cheap path only)"""
    import proactive_engine as _pe
    try:
        body = await req.json()
    except Exception:
        body = {}
    synthesis = bool(body.get("synthesis"))
    six_hour = bool(body.get("six_hour"))
    delay_min = float(body.get("delay_minutes", 0) or 0)

    async def _fire():
        if delay_min > 0:
            await asyncio.sleep(delay_min * 60)
        if six_hour:
            return await _pe.run_six_hour_scan_now(label="manual 6h scan")
        if synthesis:
            await _pe.run_synthesis_now(label="manual synthesis")
            return None
        await _pe._run_review(since_hours=float(body.get("since_hours", 12)), label="manual")
        return None

    if delay_min > 0:
        t = asyncio.create_task(_fire())
        _PENDING_REVIEWS.add(t)
        t.add_done_callback(_PENDING_REVIEWS.discard)
        kind = ("6h high-bar scan" if six_hour
                else "full synthesis" if synthesis else "review")
        log.info(f"[proactive] {kind} scheduled via API — fires in {delay_min:g} min")
        return JSONResponse({"ok": True, "scheduled": f"{kind} in {delay_min:g} min"}, status_code=202)

    result = await _fire()
    if six_hour:
        return {"ok": True, "result": result, "six_hour_scan": _pe.six_hour_status()}
    plan_path = Path(__file__).parent / "data" / "active_plan.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    return {"ok": True, "plan": plan}


@app.get("/api/mind/status")
async def mind_status():
    """The mind's current state: budgets, next wake, situation model excerpt."""
    import mind as _mind
    return _mind.status()


@app.post("/api/mind/wake")
async def mind_wake_now(request: Request):
    """Loopback-only manual wake (testing/ops). Body: {"reason": str, "deep": bool}."""
    if not _is_local_request(request):
        return JSONResponse({"ok": False, "error": "local requests only"}, status_code=403)
    import mind as _mind
    try:
        body = await request.json()
    except Exception:
        body = {}
    note = await _mind.wake(reason=str(body.get("reason") or "manual"),
                            deep=bool(body.get("deep", False)))
    return {"ok": True, "note": note, "status": _mind.status()}


@app.post("/api/proactive/proposal/respond")
async def proposal_respond(req: Request):
    """iOS sends {id, approved: true/false} to approve or reject a proposal."""
    try:
        import proactive_engine as _pe
        data     = await req.json()
        pid      = data.get("id", "")
        approved = bool(data.get("approved", False))
        if not pid:
            return {"ok": False, "error": "missing id"}
        if approved:
            prop = _pe.approve_proposal(pid)
            if prop:
                from jtools.sessions_tool import start_cc_session
                action = prop.get("action") or {}
                if action.get("type") == "cc_session" and action.get("task"):
                    # Mind-proposed work: run EXACTLY what he approved, where he
                    # approved it to run — headless, tracked, stoppable.
                    name = f"mind-{pid}"
                    result = await start_cc_session(
                        action["task"], session_name=name,
                        working_dir=action.get("working_dir", ""))
                    if "PID" in result:
                        _pe.mark_proposal(pid, "in_progress", session=name)
                    else:
                        _pe.mark_proposal(pid, "failed")
                    return {"ok": True, "action": "working", "session": result}
                # Feature proposals: headless tracked run (BACKEND_MIGRATION
                # 07-23 §8) — the same flow that works for manual launches.
                # The old visible-terminal spawn errored out with nothing
                # watching it and the idea card never changed state.
                task = (f"Build this approved Orb feature: "
                        f"{prop['title']} — {prop['description']}. "
                        f"Rationale: {prop['rationale']}. "
                        f"Working dir: {Path(__file__).parent}")
                name = f"proposal-{pid}"
                result = await start_cc_session(task, session_name=name,
                                                working_dir=str(Path(__file__).parent))
                if "PID" in result:
                    _pe.mark_proposal(pid, "in_progress", session=name)
                else:
                    _pe.mark_proposal(pid, "failed")
                return {"ok": True, "action": "building", "session": result}
            return {"ok": False, "error": "proposal not found"}
        else:
            _pe.reject_proposal(pid)
            return {"ok": True, "action": "rejected"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/sessions")
async def list_sessions():
    """All registered Orb sessions (training jobs + CC sessions), PLUS a
    synthetic "orb" entry for the live voice/chat conversation itself — so
    talking to Orb right now shows up as a session, not just background jobs.
    iOS uses this for the sessions panel."""
    from pathlib import Path as _Path
    import json as _json
    from jtools.activity_log import get_activity
    sessions_dir = _Path.home() / "Desktop" / "orb_sessions"
    sessions = []

    orb_activity = get_activity("orb", limit=1)
    last = orb_activity[-1] if orb_activity else None
    sessions.append({
        "name":        "orb",
        "status":      "running" if _CLIENTS else "idle",
        "description": "Live voice/chat conversation",
        "message":     (last["text"] if last else "No activity yet"),
        "progress":    None,
        "type":        "conversation",
        "engine":      brain.effective_agent_brain(),
        "machine":     "pc",
        "updated":     (last["ts"] if last else ""),
        "pid":         None,
    })

    if sessions_dir.exists():
        for p in sorted(sessions_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
            if "_inbox" in p.stem:
                continue
            try:
                d = _json.loads(p.read_text(encoding="utf-8-sig"))
                status = d.get("status", "unknown")
                pid = d.get("pid")
                # file_activity rows are ambient signals, not processes — decay
                # to idle when nothing changed recently instead of claiming
                # "active" forever (recomputed per request, no write-back).
                if d.get("type") == "file_activity" and status == "active":
                    try:
                        age_s = (datetime.now() - datetime.fromisoformat(d.get("updated", ""))).total_seconds()
                        if age_s > 30 * 60:
                            status = "idle"
                    except Exception:
                        pass
                # PID liveness (2026-07-01, iMac item 6): a "running" row whose
                # process is gone is over — mark it ended, persistently, so 1am
                # terminals stop showing as live forever.
                if status in ("running", "starting") and pid and d.get("type") in ("terminal", "cc"):
                    try:
                        import psutil
                        if not psutil.pid_exists(int(pid)):
                            status = d["status"] = "ended"
                            d["message"] = "Process exited"
                            d["updated"] = datetime.now().isoformat(timespec="seconds")
                            p.write_text(_json.dumps(d, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                # Retention: ended rows age out of the LIST after a day (files
                # stay on disk) — old done/stopped rows shouldn't crowd the app.
                if status in ("done", "stopped", "failed", "ended"):
                    try:
                        age = datetime.now() - datetime.fromisoformat(d.get("updated", ""))
                        if age.total_seconds() > 24 * 3600:
                            continue
                    except Exception:
                        pass
                sessions.append({
                    "name":        d.get("session", p.stem),
                    "status":      status,
                    "description": d.get("description", ""),
                    "message":     d.get("message", ""),
                    "progress":    d.get("progress"),
                    "type":        d.get("type", "training"),
                    "engine":      d.get("engine", ""),
                    "machine":     d.get("machine", "pc"),
                    "updated":     d.get("updated", ""),
                    "pid":         pid,
                })
            except Exception:
                pass
    return {"sessions": sessions, "count": len(sessions)}


@app.get("/api/sessions/{name}/activity")
async def session_activity(name: str, limit: int = 100):
    """Persistent activity log for one session (or 'all' for every session,
    merged) — every tool call, message, and lifecycle event ever recorded for
    it, zero LLM cost to produce. iOS uses this to show real scrollback when
    opening a session's detail view, then layers the live {type:"session_event"}
    WS stream on top for anything that happens while it's open."""
    from jtools.activity_log import get_activity
    session = "" if name.lower() == "all" else name
    return {"session": name, "activity": get_activity(session, limit=min(limit, 500))}


@app.get("/api/sessions/{name}/events")
async def session_events(name: str, limit: int = 80):
    """History read for the phone's session-detail feed (2026-07-01, iMac item
    2c): the last ~N activity entries shaped like the live {type:"session_event"}
    WS stream — same source file — so the feed shows what an agent has BEEN
    doing the moment the page opens, and the WS stream continues it seamlessly."""
    from jtools.activity_log import get_activity
    session = "" if name.lower() == "all" else name
    entries = get_activity(session, limit=min(int(limit), 200))
    return {"session": name,
            "events": [{"ts": e.get("ts", ""), "kind": e.get("kind", ""),
                        "text": e.get("text", "")} for e in entries]}


@app.get("/api/chat/history")
async def chat_history(since: str = "", limit: int = 100):
    """Reconnect backfill for the MAIN CHAT view (2026-07-20).

    The live reply is delivered over the WS as a {type:"audio"} frame; if the
    app is backgrounded when the turn completes, that frame is lost and the main
    chat never shows the answer (the sessions tab survives because it re-reads
    the durable activity log). This endpoint returns ONLY the conversational
    turns (user + assistant messages) from that same durable log, filtered to
    what the client missed, so the app can fetch-on-foreground and merge.

    Query:
      since  — ISO timestamp (seconds) of the last message the client already
               has. Returns messages with ts >= since; the client should dedupe
               the boundary by (ts, speaker, text) since ts is second-resolution.
               Empty = the most recent `limit` messages (cold open).
      limit  — max messages to return (cap 500).

    Returns {messages:[{ts, speaker, text}...], cursor} where cursor is the ts of
    the newest message returned — store it and pass it back as `since` next time.
    Purely additive: reads the existing log, does not touch the live WS path.
    """
    from jtools.activity_log import get_activity
    # Pull generously from the durable log, then keep only conversational turns.
    raw = get_activity("orb", limit=min(int(limit) * 4 + 50, 500))
    msgs = [
        {"ts": e.get("ts", ""),
         "speaker": e.get("speaker", ""),
         "text": e.get("text", "")}
        for e in raw
        if e.get("kind") == "message"
    ]
    if since:
        msgs = [m for m in msgs if m["ts"] >= since]
    msgs = msgs[-min(int(limit), 500):]
    return {"messages": msgs,
            "cursor": (msgs[-1]["ts"] if msgs else since)}


@app.post("/api/resume-session")
async def resume_session_endpoint(request: Request):
    """Resume a CC session by name. iOS sessions panel 'Resume' button calls this.
    Body: {"session_name": "...", "message": "optional extra instructions"}
    """
    try:
        body = await request.json()
    except Exception:
        return {"ok": False, "error": "invalid JSON body"}
    session_name = body.get("session_name", "").strip()
    if not session_name:
        return {"ok": False, "error": "session_name required"}
    extra = body.get("message", "")
    from jtools.sessions_tool import resume_cc_session
    result = await resume_cc_session(session_name, extra_instructions=extra)
    ok = "error" not in result.lower() and "not found" not in result.lower()
    return {"ok": ok, "message": result}


async def _deliver_session_message(session_name: str, message: str) -> None:
    """Background delivery of a phone→session message (the route 202s first —
    the CC relay can take up to ~90s and must never block the HTTP request).
    Routing lives in message_session: a visible terminal gets real keystrokes;
    a cc session with a session_id gets a `claude --resume` relay (a reply comes
    back); anything else gets an inbox file consumed at the session's next
    breakpoint. The outcome reaches the phone as {type:"cc_reply"} plus the
    session_events message_session already logs."""
    from jtools.sessions_tool import message_session
    try:
        result = await message_session(session_name, message)
    except Exception as e:
        result = f"Delivery failed: {e}"
    evt = {"type": "cc_reply", "session": session_name, "message": (result or "")[:1500]}
    for mws in list(_MOBILE_CLIENTS):
        await _safe_ws_send(mws, evt)


@app.post("/api/sessions/{session_name}/message")
async def message_session_endpoint(session_name: str, request: Request):
    """Send a message to a running session. iOS sessions panel 'Message' button.
    Body: {"message": "..."} or {"text": "..."} — both keys accepted (2026-07-01).
    Returns 202 immediately; delivery + the reply stream into the session feed."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    message = (body.get("message") or body.get("text") or "").strip()
    if not message:
        return JSONResponse({"ok": False, "error": "message required"}, status_code=400)
    asyncio.create_task(_deliver_session_message(session_name, message))
    return JSONResponse(
        {"ok": True, "queued": True,
         "response": f"Queued for '{session_name}' — the reply will stream into the session feed."},
        status_code=202)


@app.post("/api/sessions/{session_name}/stop")
async def stop_session_endpoint(session_name: str):
    """Stop a running session. iOS sessions panel Stop button.
    For CC sessions: sends a stop instruction via inbox.
    For any session: marks status as 'stopped' in the status file.
    """
    import json as _json
    from pathlib import Path as _Path
    sessions_dir = _Path.home() / "Desktop" / "orb_sessions"
    status_file = sessions_dir / f"{session_name}.json"

    # Write a stop message to inbox so any running process can detect it
    inbox = sessions_dir / f"{session_name}_inbox.json"
    inbox.write_text(_json.dumps({
        "id": uuid.uuid4().hex[:8], "from": "orb",
        "message": "stop cleanly after the current step",
        "timestamp": datetime.now().isoformat(timespec="seconds"),
    }), encoding="utf-8")

    # Also try to kill by PID if we have one
    killed = False
    if status_file.exists():
        try:
            d = _json.loads(status_file.read_text(encoding="utf-8-sig"))
            pid = d.get("pid")
            if pid:
                import signal, os as _os
                try:
                    _os.kill(pid, signal.SIGTERM)
                    killed = True
                except Exception:
                    pass
            d["status"] = "stopped"
            d["message"] = "Stopped by user"
            d["updated"] = datetime.now().isoformat(timespec="seconds")
            status_file.write_text(_json.dumps(d, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {"ok": True, "killed": killed, "message": f"Stop signal sent to '{session_name}'."}


async def _run_engine_task(name: str, engine: str, task: str) -> None:
    """A grok/codex 'session': one-shot CLI run tracked through the same status
    file + activity pipeline as cc sessions, so it shows in /api/sessions and
    the phone can watch it finish."""
    from jtools.sessions_tool import _SESSIONS_DIR
    from jtools.activity_log import log_activity
    path = _SESSIONS_DIR / f"{name}.json"

    def _write(status: str, message: str) -> None:
        try:
            _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "session": name, "status": status, "type": "cc",
                "description": task[:120], "message": message[:300],
                "engine": engine, "machine": "pc",
                "updated": datetime.now().isoformat(timespec="seconds"),
            }, indent=2), encoding="utf-8")
        except Exception:
            pass

    _write("running", f"Running on {engine}…")
    log_activity(name, "lifecycle", f"Started on {engine}: {task[:100]}")
    try:
        if engine.startswith("grok"):
            from jtools.grok_tool import run_with_grok
            result = await run_with_grok(task)
        else:
            from jtools.codex_tool import run_with_codex
            result = await run_with_codex(task)
        _write("done", result)
        log_activity(name, "message", f"Finished: {(result or '')[:300]}", speaker=engine)
    except Exception as e:
        _write("failed", str(e))
        log_activity(name, "lifecycle", f"Failed: {e}")


@app.post("/api/sessions/start")
async def start_session_endpoint(request: Request):
    """New-task picker on the phone (2026-07-01, iMac item 4). Body: {task,
    engine: claude-code|grok-build|codex, machine: pc, name?, working_dir?}.
    claude-code starts a tracked headless cc session (with the live per-step
    feed); grok-build/codex run as one-shot CLI tasks tracked through the same
    session record. machine='mac' is reported honestly as not wired up yet."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid JSON body"}, status_code=400)
    task = (data.get("task") or "").strip()
    if not task:
        return JSONResponse({"ok": False, "error": "task required"}, status_code=400)
    engine = (data.get("engine") or "claude-code").strip().lower()
    machine = (data.get("machine") or "pc").strip().lower()
    name = (data.get("name") or data.get("session_name") or "").strip()
    if machine not in ("", "pc"):
        return JSONResponse(
            {"ok": False, "error": "only machine='pc' can start sessions today — the Mac fleet isn't wired to this endpoint yet"},
            status_code=400)

    if engine in ("claude-code", "claude", "cc"):
        from jtools.sessions_tool import start_cc_session
        result = await start_cc_session(task, session_name=name,
                                        working_dir=(data.get("working_dir") or ""),
                                        engine="claude-code")
        if "not found" in result.lower():
            return JSONResponse({"ok": False, "error": result}, status_code=500)
        m = re.search(r"'([^']+)'", result)
        return JSONResponse({"ok": True, "engine": "claude-code",
                             "name": (m.group(1) if m else name), "result": result},
                            status_code=202)
    if engine in ("grok", "grok-build"):
        if not brain.grok_available():
            return JSONResponse({"ok": False, "error": "grok isn't logged in on the PC"}, status_code=400)
        name = name or ("grok-" + re.sub(r"[^a-z0-9]+", "-", task[:28].lower()).strip("-") + "-" + uuid.uuid4().hex[:4])
        asyncio.create_task(_run_engine_task(name, "grok-build", task))
        return JSONResponse({"ok": True, "engine": "grok-build", "name": name,
                             "result": f"Started '{name}' on Grok."}, status_code=202)
    if engine in ("codex", "codex-mini", "codex-full"):
        if not brain.codex_available():
            return JSONResponse({"ok": False, "error": "codex isn't logged in on the PC"}, status_code=400)
        if engine in brain.TIER_UNAVAILABLE:
            return JSONResponse({"ok": False, "error": brain.TIER_UNAVAILABLE[engine]}, status_code=400)
        name = name or ("codex-" + re.sub(r"[^a-z0-9]+", "-", task[:28].lower()).strip("-") + "-" + uuid.uuid4().hex[:4])
        asyncio.create_task(_run_engine_task(name, "codex", task))
        return JSONResponse({"ok": True, "engine": "codex", "name": name,
                             "result": f"Started '{name}' on Codex."}, status_code=202)
    return JSONResponse({"ok": False, "error": f"unknown engine '{engine}'"}, status_code=400)


@app.post("/api/internal/mcp_relay/{token}")
async def mcp_relay_call(token: str, request: Request):
    """Loopback-only: a spawned orb_mcp.py subprocess (codex/grok's MCP
    server child) relays a phone-connector tool call back here so it can
    reach the live WS connection's remote_dispatch. See the
    _MCP_RELAY_SESSIONS block (near _run_mcp_codex_turn) for why this exists."""
    if not _is_local_request(request):
        return JSONResponse({"error": "local only"}, status_code=403)
    session = _MCP_RELAY_SESSIONS.get(token)
    if not session:
        return JSONResponse({"error": "unknown or expired relay token"}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    name = body.get("name", "")
    args = body.get("args") or {}
    try:
        result = await session["dispatch"](name, args)
    except Exception as e:
        return JSONResponse({"error": f"relay dispatch failed: {e}"}, status_code=500)
    return {"result": result}


# ---------------------------------------------------------------------------
# WebSocket handler — the main voice loop
# ---------------------------------------------------------------------------

def _is_local(ws: WebSocket) -> bool:
    """True if the client connected from localhost — no auth needed."""
    client = ws.client
    if not client:
        return False
    host = client.host or ""
    return host in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def _is_tailnet(ws: WebSocket) -> bool:
    """True ONLY for an authenticated tailnet user (not a public visitor).

    Verified empirically via /api/whoami:
      - Authenticated tailnet device: has 'tailscale-user-login', NO funnel header.
      - PUBLIC Funnel visitor (e.g. an iPhone whose MagicDNS is broken and
        therefore hit the public path): has 'tailscale-funnel-request: ?1'
        and NO user-login. These MUST present the token — they are not trusted.
    Do NOT trust by source IP: Funnel's ingress IP is not predictable and
    must never be a free pass.
    """
    if ws.headers.get("tailscale-funnel-request"):
        return False  # public funnel visitor — token required
    return bool(ws.headers.get("tailscale-user-login"))


@app.websocket("/ws/voice")
async def voice_ws(ws: WebSocket):
    # Auth gate. Three paths are trusted:
    #   1. localhost (desktop overlay/browser on this PC)
    #   2. Tailscale tailnet (Serve/Funnel proxied; Tailscale already
    #      authenticated the caller's tailnet identity)
    #   3. valid ORB_TOKEN in query string (only relevant if you ever
    #      re-expose via public Funnel without Tailscale auth)
    token = ws.query_params.get("token", "")
    is_local = _is_local(ws)
    is_tailnet = _is_tailnet(ws)
    log.info(f"[auth] host={ws.client.host if ws.client else '?'} local={is_local} "
             f"tailnet={is_tailnet} token_match={token == ORB_TOKEN} "
             f"funnel={bool(ws.headers.get('tailscale-funnel-request'))}")
    if not is_local and not is_tailnet and token != ORB_TOKEN:
        await ws.close(code=4401, reason="unauthorized")
        log.warning(f"Rejected unauthorized WS from {ws.client.host if ws.client else '?'}")
        return

    await ws.accept()
    _CLIENTS.add(ws)
    # Desktop vs mobile: the mobile PWA will connect identifying itself (Phase 2
    # handshake) so the _spawn_* helpers dispatch HUD messages over this WS
    # instead of spawning Qt windows. Until then everything is "desktop" and the
    # desktop path is byte-for-byte unchanged.
    client_kind = "mobile" if ws.query_params.get("client") == "mobile" else "desktop"
    if client_kind == "mobile":
        _MOBILE_CLIENTS.add(ws)
    conv = get_conversation()
    last_orb_reply = ""
    last_reply_time = 0.0
    state = {
        "claude_task": None,
        "claude_status": None,   # "running" | "done" | "failed" | None
        "claude_prompt": "",     # what was actually requested
        "claude_started": 0.0,   # epoch start time
        "claude_result": "",     # real final summary (grounded in actual output)
        "claude_folder": "",     # folder created, if any (verified on disk)
        "pending_claude": None,  # a task Orb offered to hand to Claude Code,
                                 # run if the user's next turn affirms ("yes/do it")
        "pending_brain_switch": None,  # (model, reason) Orb proposed switching to;
                                        # applied if the user's next turn affirms
        # PC tier (tool-relay): the phone advertises its OrbToolKit tools + connector
        # state + APNs token via a {type:"hello"}; the agent loop calls them over the WS.
        "phone_tools": [],       # tools_schema()-shaped specs the phone can run
        "phone_connectors": {},  # {"calendar":bool,...} reported by the phone
        "push_token": "",        # APNs device token (for server-side proactivity)
        "remote_dispatch": None, # set below: async (name,args)->str, relays to the phone
    }
    # state included so tools (via _current_client ContextVar) can set things like
    # pending_brain_switch without needing a dedicated plumbing path each time.
    _current_client.set({"kind": client_kind, "ws": ws, "state": state})

    # --- PC tier: remote tool-relay plumbing (per connection) -----------------
    # The WS receive loop is the ONLY reader of this socket, so a phone-tool call must
    # NOT block it. remote_dispatch parks on a per-id future that the loop resolves when
    # the phone returns a {type:"tool_result"}; the turn itself runs as a task (below).
    _pending_tools: dict[str, asyncio.Future] = {}
    _turn_lock = asyncio.Lock()

    async def remote_dispatch(name: str, args: dict) -> str:
        """Run one of the PHONE's OrbToolKit tools over the WS and await its result."""
        tid = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        _pending_tools[tid] = fut
        await _safe_ws_send(ws, {"type": "tool_call", "id": tid, "name": name, "args": args})
        await _safe_ws_send(ws, {"type": "status", "state": "working"})   # latency cover
        try:
            return await asyncio.wait_for(fut, timeout=25.0)
        except asyncio.TimeoutError:
            _pending_tools.pop(tid, None)
            return ("The phone didn't respond in time — tell the user their phone seems "
                    "unreachable right now and offer to try again.")
        except Exception as e:
            _pending_tools.pop(tid, None)
            return f"[phone tool error: {e}]"

    state["remote_dispatch"] = remote_dispatch
    if is_local:
        origin = "local"
    elif is_tailnet:
        ts_user = ws.headers.get("tailscale-user-login", "")
        origin = f"tailnet ({ts_user or ws.client.host})"
    else:
        origin = f"remote ({ws.client.host})"
    log.info(f"Client connected ({origin}, {client_kind})")

    # Conversational follow-up window: after Orb responds, the user has this
    # many seconds to ask a follow-up WITHOUT saying "Orb" again. Closes
    # automatically; resets on each response.
    FOLLOWUP_WINDOW_SEC = 10.0
    state["last_response_end"] = 0.0
    state["ptt_until"] = 0.0   # push-to-talk arm expiry (mobile orb tap)
    # Tool context — so follow-ups can re-call the same tool with tweaked params
    # instead of Ollama hallucinating (e.g. "and tomorrow?" after weather).
    state["last_tool"] = None     # "weather" | None
    state["last_tool_params"] = {}
    # The most recent SUBJECT the user discussed (a person/place/thing/topic), so
    # a bare follow-up like "show me" / "show me more" resolves to it — e.g.
    # "tell me about the US economy" then "show me" -> images of the US economy,
    # NOT whatever was discussed three turns ago.
    state["last_subject"] = _FOCUS.get("subject")  # inherit the shared focus
    # Connector snapshot pushed by the client (mobile) — its on-device
    # calendar/reminders/contacts. The phone holds this data privately and sends
    # a fresh `{type:"context"}` snapshot each turn it has connectors enabled, so
    # the VOICE path (audio -> PC -> Whisper) can answer "what's on my calendar?"
    # too — not just typed chat. Latest snapshot wins; an empty text clears it.
    state["connector_context"] = ""

    async def respond(reply: str):
        """TTS and send audio response. No-op if the client has disconnected."""
        nonlocal last_orb_reply, last_reply_time
        # never-deny safety net only. The old _ensure_next_step appended a canned
        # "shall I try a different source, search images, or hand it to Claude Code,
        # sir?" to anything that looked like a soft failure — programmatic cruft that
        # leaks into conversation and is nonsensical now (we ARE Claude Code). The
        # brain handles "what next" itself; we just keep the rare refusal-reframe.
        reply = _reframe_refusal(reply)
        _log_chat("orb", reply)
        last_orb_reply = reply.lower().strip().rstrip(".")
        last_reply_time = time.time()
        state["last_response_end"] = time.time()
        if ws.client_state.name != "CONNECTED":
            # He closed the app mid-turn and the answer used to just evaporate
            # (his 07-03 report: "exit the app... the message never lands").
            # The reply is already in conversation history, but the app doesn't
            # backfill on reconnect yet (queued for the iMac) — so deliver it
            # as a push: APNs works with the app closed, and the reply reaches
            # him regardless of the connection that asked.
            log.info("[respond] client gone mid-turn — delivering reply as push")
            try:
                await _push_notification("While you were away", reply, "info",
                                         source="reply-fallback", dedupe=False)
            except Exception as e:
                log.warning(f"[respond] push fallback failed: {e}")
            return
        try:
            audio_bytes = await text_to_speech(reply)
            audio_b64 = base64.b64encode(audio_bytes).decode()
            _msg = {"type": "audio", "data": audio_b64, "text": reply}
            # Label honesty (07-04): when the fallback ladder answered because
            # the selected brain was down, tell the app who really replied so
            # the switcher can badge it instead of lying.
            if state.get("answered_via"):
                _msg["answered_via"] = state.pop("answered_via")
            await ws.send_text(json.dumps(_msg))
        except Exception as e:
            log.error(f"TTS/send error: {e}")
            # Same promise on the failure path: a turn's answer must land
            # SOMEWHERE the user can see it, never vanish into a log line.
            try:
                await _push_notification("While you were away", reply, "info",
                                         source="reply-fallback", dedupe=False)
            except Exception as e2:
                log.warning(f"[respond] push fallback failed: {e2}")

    def is_echo(text: str) -> bool:
        """Detect if the transcript is just Orb's own speech picked up by the mic."""
        if time.time() - last_reply_time > 10:
            return False
        incoming = text.lower().strip().rstrip(".")
        if not last_orb_reply:
            return False
        # Exact or near-exact match
        if incoming == last_orb_reply:
            return True
        # Partial match — if most of Orb's reply appears in the input
        if len(incoming) > 5 and (incoming in last_orb_reply or last_orb_reply in incoming):
            return True
        # Check word overlap
        reply_words = set(last_orb_reply.split())
        input_words = set(incoming.split())
        if reply_words and len(reply_words & input_words) / len(reply_words) > 0.6:
            return True
        return False

    async def process_transcript(raw_text: str, typed: bool = False):
        """Run a transcript through the full pipeline. Shared by the local-Whisper
        audio path and the typed chat path. `typed` input (from the chat HUD)
        bypasses echo + wake-word gating — you shouldn't type 'Orb' each line."""
        raw_text = raw_text.strip()
        if not raw_text:
            return
        # Modality for this turn — the brain's register should differ between
        # a chat bubble (markdown fine) and TTS (plain sentences only). Caught
        # in the 07-02 day-in-life battery: list-shaped questions kept pulling
        # markdown lists into would-be-spoken replies despite the blanket rule.
        state["_turn_typed"] = typed

        # iOS bundles connector context with the user message in one transcript:
        # "Context from my phone (read-only — use it to answer):\n...\nMy message: <text>"
        # Parse it out so the actual user command runs through all trigger matchers.
        _CTX_MARKER = "My message:"
        if raw_text.startswith("Context from my phone") and _CTX_MARKER in raw_text:
            _parts = raw_text.split(_CTX_MARKER, 1)
            _ctx = _parts[0].replace("Context from my phone (read-only — use it to answer):", "").strip()
            state["connector_context"] = _ctx
            raw_text = _parts[1].strip()
            typed = True  # no wake word in bundled context messages

        # The pure user message, for tools that need to know what THIS turn
        # actually asked (switch_brain's user-requested-vs-Orb-idea guard).
        state["last_user_text"] = raw_text

        # Typed chat input skips echo + wake-word gating entirely.
        if typed:
            pass
        # Echo detection — drop if it's just Orb hearing himself.
        # MUST send a recovery status or the frontend stays stuck on "thinking".
        elif is_echo(raw_text):
            log.info(f"[echo] dropped: {raw_text}")
            await _safe_send(ws, {"type": "status", "state": "idle"})
            return

        # WAKE WORD GATE — but with a follow-up grace window so the user can
        # ask "and tomorrow?" right after a response without re-saying "Orb".
        in_followup = (time.time() - state["last_response_end"]) < FOLLOWUP_WINDOW_SEC
        # Push-to-talk (mobile orb tap) arms an 8s window where the next utterance
        # is accepted WITHOUT the wake word (one question).
        ptt_active = time.time() < state.get("ptt_until", 0.0)
        # PHONE = no wake word at all (2026-07-01, ninjahawk: "it should be able
        # to hear what I'm saying and then respond directly"): the app is
        # foreground + unmuted by deliberate choice — that IS the intent; mute is
        # the off switch. The echo filter above still runs, plus a TTS-bleed
        # guard: the phone's mic hears its own speaker, and a garbled Whisper
        # take of our own voice won't string-match is_echo — so non-wake-word
        # utterances landing within 3s of our reply going out are dropped.
        # (Proper fix is the app pausing capture during playback — iMac item.)
        mobile_no_wake = (client_kind == "mobile"
                          and os.getenv("ORB_MOBILE_NO_WAKE", "1") == "1")
        if (mobile_no_wake and not typed and not contains_wake_word(raw_text)
                and (time.time() - state["last_response_end"]) < 3.0):
            log.info(f"[echo] dropped (TTS-bleed window): {raw_text}")
            await _safe_send(ws, {"type": "status", "state": "idle"})
            return
        if (not typed and not mobile_no_wake and not contains_wake_word(raw_text)
                and not in_followup and not ptt_active):
            log.info(f"[ignored] no wake word: {raw_text}")
            await _safe_send(ws, {"type": "status", "state": "idle"})
            return
        if ptt_active:
            state["ptt_until"] = 0.0   # one-shot: consume the push-to-talk arm
            log.info(f"[ptt] accepting (wake word bypassed): {raw_text}")
        if in_followup and not contains_wake_word(raw_text):
            log.info(f"[followup] accepting (within {FOLLOWUP_WINDOW_SEC}s grace): {raw_text}")

        text = strip_wake_word(raw_text)
        log.info(f"[user] {raw_text}" + (f" -> [{text}]" if text != raw_text else ""))
        _log_chat("user", raw_text)

        # 0a. Bare wake — the user said only "Orb" / "hey orb" with no
        # follow-up. Skip the LLM entirely and pick from a varied reply pool
        # in personas.py. Without this intercept the empty post-strip text
        # falls into ollama_chat_with_tools and Qwen keeps gravitating to
        # the same "functioning within optimal parameters, sir" phrasing.
        if is_bare_wake(raw_text):
            reply = pick_bare_wake_reply()
            log.info(f"[bare-wake] -> {reply!r}")
            conv.add_user(raw_text); conv.add_assistant(reply)
            await _safe_send(ws, {"type": "status", "state": "speaking"})
            await respond(reply)
            return

        # 0a2. If there's a BACKGROUND job, a status-ish question ("how's it
        # coming", "how are you") reports the REAL job — BEFORE the canned pool.
        if (is_status_query(text) or match_status_question(text)) and \
                _BG["status"] in ("running", "done") and _BG["desc"]:
            if _BG["status"] == "running":
                el = int(time.time() - _BG["started"])
                reply = (f"Still researching {_BG['desc']}, sir — about {el} seconds "
                         f"in. I'll show you the moment it's ready.")
            else:
                reply = f"I've finished researching {_BG['desc']}, sir."
            conv.add_user(raw_text); conv.add_assistant(reply)
            await respond(reply)
            return

        # 0b. Status query — "how are you", "you good", etc. Same rationale:
        # Qwen converges on "I am functioning within optimal parameters, sir"
        # so we intercept with a varied pool to break the repetition.
        if is_status_query(text):
            reply = pick_status_query_reply()
            log.info(f"[status-query] -> {reply!r}")
            conv.add_user(raw_text); conv.add_assistant(reply)
            await _safe_send(ws, {"type": "status", "state": "speaking"})
            await respond(reply)
            return

        # Orb 'thinking' goes to the ORIGINATOR only. Broadcasting it to the orb
        # for chat made it grow then shrink (janky) since chat has no speech to
        # hold the size. Proper text-interface orb behaviour (a subtle spin, no
        # grow) is a TODO — study orb.ts state->animation first.
        await _safe_send(ws, {"type": "status", "state": "thinking"})

        # Command form: strip polite/question framing ("can you open reddit" ->
        # "open reddit") so natural requests reach the deterministic action
        # matchers instead of waffling at the LLM. Lookups/conversation still
        # use `text`.
        cmd = _command_form(text)

        # 0a. Pending Claude Code offer — if Orb just offered to hand a task
        # to Claude Code and the user affirms ("yes" / "do it"), ACTUALLY run it.
        # Fixes "I said yes and it didn't do it." A stale offer is dropped if the
        # user says something else instead.
        if state.get("pending_claude"):
            if is_affirmation(cmd) and not (state["claude_task"] and not state["claude_task"].done()):
                task = state["pending_claude"]
                state["pending_claude"] = None
                log.info(f"[claude] affirmation -> running offered task: {task}")
                conv.add_user(raw_text)
                state["claude_status"] = "running"; state["claude_prompt"] = task
                state["claude_started"] = time.time()
                state["claude_result"] = ""; state["claude_folder"] = ""
                await respond("Right away, sir. Handing this to Claude Code.")
                state["claude_task"] = asyncio.create_task(
                    claude_worker(task, ws, conv, respond, state))
                return
            elif not is_affirmation(cmd):
                state["pending_claude"] = None  # user moved on; offer expires

        # 0a2. Pending brain-switch proposal — Orb suggested switching brains
        # on its own initiative (propose_brain_switch tool); only apply it if the
        # user's next turn affirms. Same shape as the pending_claude offer above —
        # human stays in the loop for anything Orb initiates, not just what it's told.
        if state.get("pending_brain_switch"):
            if is_affirmation(cmd):
                model, switch_reason = state["pending_brain_switch"]
                state["pending_brain_switch"] = None
                brain.set_brain(model)
                label = brain.label(model)
                log.info(f"[brain] affirmation -> switching to {model} ({switch_reason})")
                reply = f"Switched to {label}."
                conv.add_user(raw_text); conv.add_assistant(reply)
                await respond(reply)
                return
            elif not is_affirmation(cmd):
                state["pending_brain_switch"] = None  # user moved on; offer expires

        # 0. Mute
        if match_mute_command(text):
            log.info("[mute] User requested mute")
            reply = "Going silent, sir."
            conv.add_user(raw_text); conv.add_assistant(reply)
            await respond(reply)
            await _safe_send(ws, {"type": "command", "action": "mute"})
            return

        # 0.5 Stop/cancel a running Claude task
        if match_stop_command(text) and state["claude_task"] and not state["claude_task"].done():
            log.info("[claude] User requested STOP — cancelling task")
            state["claude_task"].cancel()
            reply = "Stopping, sir."
            conv.add_user(raw_text); conv.add_assistant(reply)
            await respond(reply)
            return

        # 0.6 Forget — wipe persistent memory
        if match_forget_command(text):
            log.info("[memory] User requested forget — wiping history")
            memory_store.clear()
            conv.messages = []
            # A clean slate includes the resumed claude brain session — the
            # next turn starts a genuinely fresh conversation, not a resume.
            reset_claude_brain_session("user said forget")
            reply = "Memory wiped. Clean slate."
            await respond(reply)
            return

        # 1. Desktop commands (instant, no LLM)
        desktop_action = match_desktop_command(cmd)
        if desktop_action:
            # URL targets open in the DEFAULT browser (os/webbrowser) — the old
            # hardcoded "start chrome" silently failed on machines without
            # Chrome (e.g. Edge default), which is why "open youtube" said it
            # opened but nothing happened.
            if desktop_action.get("url"):
                log.info(f"[desktop] open url {desktop_action['url']}")
                await asyncio.to_thread(connectors.open_url, desktop_action["url"])
            else:
                log.info(f"[desktop] {desktop_action['cmd']}")
                await run_desktop_command(desktop_action["cmd"])
            reply = desktop_action.get("reply") or _desktop_reply(desktop_action["app"])
            conv.add_user(raw_text); conv.add_assistant(reply)
            await respond(reply)
            return

        # 1.2 — ACTION CONNECTORS (instant, no LLM). Media transport, system
        # volume, "start my music", open saved site/url/app. Allowlisted,
        # non-destructive actions only (see connectors.py). Runs AFTER desktop
        # commands (so "open youtube" hits the specific desktop launcher) and
        # BEFORE the intent router (so "play music"/"go to reddit.com" aren't
        # swallowed by lookup/scrape).
        action = connectors.match_connector(cmd)
        if action:
            log.info(f"[connector] {action.key}")
            try:
                await asyncio.to_thread(action.run)
            except Exception as e:
                log.error(f"[connector] {action.key} failed: {e}")
            if _VOC:
                reply = action.reply.format(addr=_VOC)
            else:
                reply = (action.reply.replace(", {addr}", "")
                         .replace(" {addr}", "").format(addr="").strip())
            conv.add_user(raw_text); conv.add_assistant(reply)
            await respond(reply)
            return

        # 1.5 — DETERMINISTIC INTENT ROUTER (the hot path).
        # Pattern-match the request to a known tool BEFORE consulting the
        # LLM. Qwen 9B isn't reliable enough to choose tools by itself —
        # it would say "I can't look that up" for clear factual queries.
        # Here we decide deterministically, execute the tool, then only
        # use Qwen to phrase the result naturally.
        # ========================== AGENT SPINE ==========================
        # The thin agentic loop (agent.py) is the conversational brain: the model
        # sees the conversation + its tools and decides to talk or call a tool.
        # Haiku orchestrates everything — tools, Claude Code, brain switching.
        # Toggle off with ORB_AGENT_SPINE=0.
        if _AGENT_SPINE:
            conv.add_user(raw_text)
            # Instant ack (optional, flag-gated): only worth it on a slow claude
            # brain — speak a short "heard you" while the real answer reasons.
            if _AGENT_ACK and brain.effective_agent_brain() != "local":
                try:
                    ack = pick_ack_reply()
                    ack_audio = base64.b64encode(await text_to_speech(ack)).decode()
                    await _safe_send(ws, {"type": "ack", "text": ack, "data": ack_audio})
                except Exception as e:
                    log.warning(f"[agent ack] {e}")
            try:
                reply = await _agent_respond(conv, state)
            except Exception as e:
                log.error(f"[agent spine] {e}")
                # Failure telemetry: an exception-eaten turn is a dead turn —
                # count it where the synthesis reads (07-02 introspection ask).
                try:
                    from jtools.activity_log import log_activity as _log_act
                    _log_act("orb", "turn_error", f"agent spine exception: {str(e)[:160]}")
                except Exception:
                    pass
                reply = "My apologies, sir — I lost my train of thought. Say again?"
            # Annotate stored message with tools used so the model can
            # accurately recall what it called when asked next turn.
            tools_used = state.get("_last_tools_used") or []
            stored_reply = (reply + f"\n[Tools used: {', '.join(tools_used)}]"
                            if tools_used else reply)
            conv.add_assistant(stored_reply)
            await respond(reply)
            return
        # ============== LEGACY ROUTER-FIRST PIPELINE (flag off) ==============
        # CONTEXTUAL REFERENCE: "show me pictures of this person that fit X",
        # "based on that research, ..." -> resolve the reference to the current
        # focus subject, then route the concrete command normally.
        if _has_reference(cmd):
            if _is_conversational_followup(cmd, conv.messages):
                # A conversational aside referring to what we were just discussing
                # ("is that recent?", "why is it like that?") -> let the
                # context-aware conversational path resolve it from history. Do NOT
                # force-resolve it into a tool query.
                pass
            elif _FOCUS["subject"]:
                resolved = await brain.resolve_reference(cmd, _FOCUS["subject"])
                if resolved and resolved.lower() != cmd.lower():
                    log.info(f"[ref] {cmd!r} -> {resolved!r} (focus={_FOCUS['subject']!r})")
                    cmd = resolved
                    text = resolved
            elif not any(m["role"] == "assistant" for m in conv.messages):
                # Referential with NOTHING in focus AND no conversation to refer to
                # -> clarify (don't research the bare pronoun, which hallucinated
                # 'Siler City' for 'where is it').
                reply = "I'm not sure what you're referring to, sir — what are you asking about?"
                conv.add_user(raw_text); conv.add_assistant(reply)
                await respond(reply)
                return
            # else: a reference we can't tie to a tool subject, but we ARE
            # mid-conversation -> fall through; the conversational handler (with
            # full history) resolves it. (Was a dead-end "I'm not sure what you're
            # referring to" that killed live conversations.)

        # MULTI-REQUEST: "images of A and B", "tell me about Tesla and its stock
        # price" -> split into atomic requests, fire each into its OWN HUD in
        # parallel (cards pop fastest-first via as_completed).
        if _looks_compound(cmd):
            subs = await brain.decompose(cmd)
            if len(subs) > 1:
                log.info(f"[multi] {subs}")
                tasks = [asyncio.create_task(_card_for_command(s, state)) for s in subs]
                labels = []
                for fut in asyncio.as_completed(tasks):
                    lab = await fut
                    if lab:
                        labels.append(lab)
                if labels:
                    spoken = f"I've pulled up {_join_and(labels)}, sir."
                else:
                    spoken = ("I couldn't pull those up, sir — shall I try them "
                              "one at a time?")
                conv.add_user(raw_text); conv.add_assistant(spoken)
                await respond(spoken)
                return

        intent = intent_router.route(cmd)
        # CONVERSATIONAL FOLLOW-UP — the regex router found no clear tool AND this
        # is a short referential continuation ("was that today?", "why?"). Answer
        # it from conversation context instead of letting the semantic/Haiku layers
        # re-classify it as a fresh command. Skips routing entirely; the
        # conversational handler (step 3, with full history + tools) takes it.
        followup = (not intent) and _is_conversational_followup(cmd, conv.messages)
        if followup:
            log.info(f"[followup] conversational, answering from context: {cmd!r}")
        # LAYER 1 — local embedding router (offline, free, sourced from intents.yaml).
        # Deterministic regex found nothing; try a cosine match against the intent
        # exemplars. Fires ONLY a confident, read-only, offline-routable intent (the
        # hybrid policy: reads bold, actions never auto-fire here); everything else
        # falls through to the Haiku rewrite below. OFF unless ORB_SEMANTIC_ROUTER=1.
        if not intent and not followup and semantic_router and semantic_router.enabled():
            try:
                dec = await semantic_router.decide(cmd)
                if dec and dec.action == "route":
                    reroute = intent_router.route(dec.canonical)
                    if reroute:
                        log.info(f"[semantic-v1] {cmd!r} -> {dec.intent} "
                                 f"({dec.score:.2f}) -> {reroute.tool}")
                        intent, cmd, text = reroute, dec.canonical, dec.canonical
            except Exception as e:
                log.warning(f"[semantic-v1] failed: {e}")
        # SEMANTIC FALLBACK (Haiku): still nothing -> Haiku rewrites it into a
        # canonical command and we re-route (reusing every handler). Kills the
        # regex whack-a-mole for novel phrasings; deterministic stays the fast path.
        # Skipped for conversational follow-ups (answered from context below).
        if not intent and not followup and brain.claude_available():
            try:
                canon = await brain.classify_to_command(cmd)
                if canon and canon.strip().upper() != "CHAT":
                    reroute = intent_router.route(canon)
                    if reroute:
                        log.info(f"[semantic] {cmd!r} -> {canon!r} -> {reroute.tool}")
                        intent, cmd, text = reroute, canon, canon
            except Exception as e:
                log.warning(f"[semantic] failed: {e}")
        if intent:
            log.info(f"[intent] {intent.tool}({intent.args})")
            # RESEARCH ('tell me about X') -> Haiku writes a thorough briefing;
            # Orb speaks one line, the card shows 1-2 rich paragraphs (+ photos
            # for a person/place). Not a registered tool — handled here.
            # CHAT: 'open chat' / 'let me type' -> the typed chat HUD.
            if intent.tool == "open_chat":
                _spawn_chat()
                spoken = "Chat's up, sir — type away."
                conv.add_user(raw_text); conv.add_assistant(spoken)
                await respond(spoken)
                return

            # GAME: 'play snake' -> launch a self-hosted HTML5 game in a card.
            if intent.tool == "play_game":
                gf = intent.args.get("game", "")
                _spawn_game(gf)
                name = gf.replace(".html", "").upper()
                spoken = f"Loading {name}, sir. Enjoy."
                conv.add_user(raw_text); conv.add_assistant(spoken)
                await respond(spoken)
                return

            # SCREENSHOT: 'show me my screen' / 'take a screenshot' -> capture the
            # desktop. On mobile it pops as a HUD image panel; on desktop it's
            # saved to Orb_Output. The first step of the desktop bridge.
            if intent.tool == "screenshot":
                status = _take_and_dispatch_screenshot()
                if status == "mobile":
                    spoken = _random.choice([
                        "Here's your screen, sir.",
                        "Putting your desktop up now, sir.",
                        "There it is — your screen as it stands, sir.",
                    ])
                elif status == "desktop":
                    spoken = _random.choice([
                        "Here's your screen, sir.",
                        "Putting it up now, sir.",
                        "There it is, sir — and I've saved a copy.",
                    ])
                else:
                    spoken = ("I wasn't able to capture the screen on this machine, "
                              "sir.")
                conv.add_user(raw_text); conv.add_assistant(spoken)
                await respond(spoken)
                return

            # AUTO-RESEARCH: 'research X' / 'deep dive on X' -> fire parallel
            # fetches, pop cards as they land (images, optional stock chart), then
            # a grounded synthesis card. Multiple HUDs appear as it works.
            if intent.tool == "deep_research":
                subj = intent.args.get("query", "")
                _set_focus(state, subj)
                conv.add_user(raw_text)
                # Run in the BACKGROUND so the user can keep talking; cards pop as
                # the work lands and Orb announces completion. Not abandoned if
                # the user says something else.
                asyncio.create_task(_run_deep_research(subj, respond, conv, ws))
                await respond(f"On it, sir — researching {subj}. I'll keep working "
                              f"and show you as it comes in; carry on.")
                return

            # 'show me' / 'show me more' with no new subject -> images of the
            # MOST RECENT subject. Says which subject so the user can correct.
            if intent.tool == "show_subject":
                subj = _get_focus(state)
                if not subj:
                    spoken = ("I'm not sure what you'd like to see, sir — tell me "
                              "about something first, or name it.")
                else:
                    from jtools.images_tool import search_images
                    imgs = await search_images(subj, 12)
                    if imgs:
                        spoken = f"Here are some images of {subj}, sir."
                        _spawn_card_payload({"title": subj[:40], "images": imgs})
                    else:
                        spoken = f"I looked, but couldn't find images of {subj}, sir."
                conv.add_user(raw_text); conv.add_assistant(spoken)
                await respond(spoken)
                return

            # CUSTOM GRAPH ('graph of YouTube revenue') -> Haiku-assembled series.
            if intent.tool == "graph":
                q = intent.args.get("query", "")
                g = await brain.graph_data(q)
                if g:
                    _set_focus(state, g["title"])
                    pts = g["points"]
                    _spawn_card_payload({
                        "title": g["title"],
                        "headline": (f"{pts[-1]:g}" + (f" {g['unit']}" if g.get("unit") else "")),
                        "chart": {"points": pts, "up": pts[-1] >= pts[0]},
                        "note": g.get("note") or "", "foot": "ESTIMATED"})
                    spoken = g["spoken"]
                else:
                    # Too vague / no numeric series to chart -> AUTOMATICALLY give a
                    # grounded rundown card instead (no dead-end offer).
                    try:
                        from jtools import research_tool
                        sources = await research_tool.gather(q)
                        _, rcard, _iq = await brain.research_card(q, sources=sources)
                        _set_focus(state, q)
                        rp = {"title": q[:40], "foot": "RUNDOWN"}
                        if rcard.get("headline"):
                            rp["lead"] = rcard["headline"]
                        if rcard.get("kv"):
                            rp["kv"] = rcard["kv"]
                        if rcard.get("note"):
                            rp["note"] = rcard["note"].replace("\n\n", "<br><br>").replace("\n", " ")
                        _spawn_card_payload(rp)
                        spoken = f"I couldn't chart that, sir, so here's a rundown of {q}."
                    except Exception as e:
                        log.warning(f"[graph fallback] {e}")
                        spoken = "I couldn't put that together just now, sir."
                conv.add_user(raw_text); conv.add_assistant(spoken)
                await respond(spoken)
                return

            if intent.tool == "research":
                q = intent.args.get("query", "")
                # Ground the briefing in LIVE sources (current web + Wikipedia) so
                # figures are accurate/current, not stale training memory.
                try:
                    from jtools import research_tool
                    sources = await research_tool.gather(q)
                except Exception as e:
                    log.warning(f"[research] source gather failed: {e}")
                    sources = ""
                spoken, card, image_query = await brain.research_card(q, sources=sources)
                _set_focus(state, image_query or q)
                conv.add_user(raw_text); conv.add_assistant(spoken)
                log.info(f"[research] {spoken}")
                payload = {"title": (image_query or q or "Briefing")[:40]}
                if card.get("headline"):
                    payload["lead"] = card["headline"]
                if card.get("kv"):
                    payload["kv"] = card["kv"]
                if card.get("note"):
                    payload["note"] = card["note"].replace("\n\n", "<br><br>").replace("\n", " ")
                if image_query:
                    try:
                        from jtools.images_tool import search_images
                        payload["images"] = await search_images(image_query, 6)
                    except Exception as e:
                        log.warning(f"[research] images failed: {e}")
                _spawn_card_payload(payload)
                await respond(spoken)
                return
            # STOCK / MARKET -> price chart in the HUD. Handled here (not generic
            # dispatch) because the card needs the structured price series.
            if intent.tool == "get_stock":
                from jtools.finance_tool import get_stock as _get_stock
                d = await _get_stock(intent.args.get("query", ""))
                if d:
                    _set_focus(state, d["name"])
                    rlabel = "all-time" if d["range"] == "max" else "the past year"
                    updown = "up" if d["up"] else "down"
                    spoken = (f"{d['name']} is at {d['price']:.2f} {d['currency']}, "
                              f"{updown} {abs(d['change_pct'])} percent over {rlabel}, sir.")
                    def _money(c):
                        if not c:
                            return "—"
                        return f"{'+' if c[0] >= 0 else '-'}${abs(c[0]):,.2f} ({c[1]:+}%)"
                    kv = []
                    if d.get("day"):
                        kv.append(["TODAY", _money(d["day"])])
                    if d.get("month"):
                        kv.append(["1 MONTH", _money(d["month"])])
                    if d.get("year"):
                        kv.append(["1 YEAR", _money(d["year"])])
                    payload = {
                        "title": d["name"],
                        "headline": f"{d['price']:.2f}",
                        "sub": f"{d['currency']} · {'▲' if d['up'] else '▼'} "
                               f"{abs(d['change_pct'])}% · {rlabel}",
                        "chart": {"points": d["points"], "up": d["up"]},
                        "kv": kv,
                        "foot": d["symbol"],
                    }
                else:
                    spoken = "I couldn't find market data for that, sir."
                    payload = None
                conv.add_user(raw_text); conv.add_assistant(spoken)
                if payload:
                    _spawn_card_payload(payload)
                await respond(spoken)
                return
            try:
                raw_result = await tool_registry.dispatch(intent.tool, intent.args)
            except Exception as e:
                log.error(f"[intent] {intent.tool} failed: {e}")
                raw_result = ""
            if raw_result and not raw_result.startswith("[tool error]"):
                state["last_tool"] = intent.tool
                state["last_tool_params"] = intent.args
                if intent.tool in ("search_images", "lookup_fact"):
                    _set_focus(state, intent.args.get("query"))
                # Image replies are spoken DETERMINISTICALLY (not re-phrased by the
                # local model, which sometimes produced garbled/cut-off lines).
                if intent.tool == "search_images":
                    subj = intent.args.get("query", "that")
                    reply = (f"Here are some images of {subj}, sir."
                             if raw_result.lower().startswith("found")
                             else raw_result)
                else:
                    reply = await naturalize(text, raw_result, history=conv.messages)
                conv.add_user(raw_text); conv.add_assistant(reply)
                log.info(f"[orb] {reply}")
                # Auto-pop a HUD card for visual/structured results (weather,
                # news, system, calc, lookup). The card self-fetches fresh data
                # for presets; calc/lookup use the result directly.
                cardq = _card_query_for_tool(intent.tool, intent.args, raw_result)
                if cardq:
                    _spawn_card(cardq)
                await respond(reply)
                return
            # Tool returned nothing useful — fall through to LLM path so
            # the model can at least say something honest.
            log.info(f"[intent] {intent.tool} returned empty; falling through")

        # 1.8 STATUS QUESTION about a Claude task — answer with REAL state only,
        # never let Ollama invent progress. This was the hallucination bug.
        # Status of the BACKGROUND research job ("how's it coming?").
        if match_status_question(text) and _BG["status"] in ("running", "done") and _BG["desc"]:
            if _BG["status"] == "running":
                el = int(time.time() - _BG["started"])
                reply = (f"Still researching {_BG['desc']}, sir — about {el} seconds "
                         f"in. I'll show you the moment it's ready.")
            else:
                reply = f"I've finished researching {_BG['desc']}, sir."
            conv.add_user(raw_text); conv.add_assistant(reply)
            await respond(reply)
            return

        if match_status_question(text) and state["claude_status"] is not None:
            st = state["claude_status"]
            if st == "running":
                elapsed = int(time.time() - state["claude_started"])
                reply = (f"Still working, sir. It's been running {elapsed} seconds. "
                         f"I don't have visibility into its internal steps — I'll tell you "
                         f"the moment it finishes.")
            elif st == "done":
                folder = state["claude_folder"]
                if folder:
                    reply = f"Finished, sir. {state['claude_result']} The folder is '{folder}' in Orb Output."
                else:
                    reply = (f"It finished, sir, but I don't see a new folder in Orb Output — "
                             f"it may not have written files. {state['claude_result']}")
            else:  # failed
                reply = f"It didn't complete, sir. {state['claude_result']}"
            conv.add_user(raw_text); conv.add_assistant(reply)
            await respond(reply)
            return

        # 2. Claude Code triggers
        claude_prompt = match_claude_trigger(cmd)
        if claude_prompt:
            if state["claude_task"] and not state["claude_task"].done():
                elapsed = int(time.time() - state["claude_started"])
                log.info("[claude] Task already running — declining new one")
                reply = f"Still working on the last task, sir — {elapsed} seconds in. One moment."
                conv.add_user(raw_text); conv.add_assistant(reply)
                await respond(reply)
                return
            log.info(f"[claude] Triggered by: {text}")
            conv.add_user(raw_text)
            state["claude_status"] = "running"
            state["claude_prompt"] = claude_prompt
            state["claude_started"] = time.time()
            state["claude_result"] = ""
            state["claude_folder"] = ""
            await respond("Right away, sir. Handing this to Claude Code.")
            state["claude_task"] = asyncio.create_task(
                claude_worker(claude_prompt, ws, conv, respond, state)
            )
            return

        # 3. Conversation + tool-calling — Qwen reasons about whether it needs
        # a tool (weather/news/lookup/files/notes/calc/scraper/system) and calls
        # it; otherwise produces a direct conversational reply.
        conv.add_user(raw_text)
        system_prompt = conv.get_system()
        if state["claude_task"] and not state["claude_task"].done():
            elapsed = int(time.time() - state["claude_started"])
            system_prompt += (
                f"\n\nREALITY CHECK: A Claude Code task is currently running "
                f"(started {elapsed}s ago, request: '{state['claude_prompt'][:80]}'). "
                f"You have NO visibility into its internal progress. If the user "
                f"asks about it, say only that it's still working — invent NOTHING."
            )
        try:
            if brain.is_claude():
                # Claude brain: pure conversation in persona. Actions were
                # already handled by the router/connectors above, so no tools
                # are needed here — capabilities stay identical, only the
                # talking model changes.
                reply = await brain.claude_chat(conv.messages, system_prompt)
                log.info(f"[orb/{brain.current_brain()}] {reply}")
            else:
                reply, used = await ollama_chat_with_tools(conv.messages, system_prompt)
                if used:
                    log.info(f"[tools-used] {used}")
                    state["last_tool"] = used[-1]
                # ESCALATION: if the local model waffled or dead-ended, hand off
                # to real Claude (zero-auth `claude -p`) so Orb keeps moving
                # toward a real answer instead of "I don't know." This is what
                # makes it feel intelligent rather than a canned router.
                if not used and brain.is_weak_reply(reply) and brain.claude_available():
                    log.info(f"[escalate] local reply weak -> claude: {reply!r}")
                    # Escalate to haiku SILENTLY. The user found the spoken
                    # "I'm consulting a deeper resource" announcement chatty and
                    # unnecessary — the orb already shows the 'thinking' state
                    # during the ~5-15s claude -p hop, so we just answer.
                    try:
                        esc, card, offer = await brain.escalate_with_card(
                            conv.messages, system_prompt)
                        # Claude honestly flags a real DOING task it can't perform
                        # by speaking -> turn into a 'shall I use Claude Code?' offer.
                        if offer:
                            state["pending_claude"] = offer
                            reply = ("That's not something I can do directly, sir, "
                                     "but Claude Code can — shall I have it take care of it?")
                        else:
                            reply = esc
                            # Deeper answer carried structured data -> fill a card.
                            if card:
                                _spawn_card_payload(card)
                        log.info(f"[escalate/claude] {reply}")
                    except Exception as e:
                        log.warning(f"[escalate] failed, keeping local reply: {e}")
                else:
                    log.info(f"[orb] {reply}")
            # REPETITION GUARD — a verbatim/near-identical repeat is the #1 "this
            # feels like a looping bot" failure. If we just said this, re-roll once
            # hotter with an explicit "don't repeat yourself" instruction.
            if _is_repeat(reply):
                log.info(f"[repeat] re-rolling: {reply!r}")
                try:
                    rr = await ollama_chat(
                        conv.messages,
                        system_prompt + "\n\nIMPORTANT: You JUST gave a nearly "
                        "identical reply. Say something genuinely different now — "
                        "do not repeat yourself.")
                    if rr and not _is_repeat(rr):
                        reply = rr
                except Exception:
                    pass
            _remember_reply(reply)
            conv.add_assistant(reply)
        except Exception as e:
            log.error(f"Ollama tool-chat error: {e}")
            # Fall back to plain Ollama (no tools) if the tool-loop crashed
            try:
                reply = await ollama_chat(conv.messages, system_prompt)
                conv.add_assistant(reply)
            except Exception as e2:
                log.error(f"Ollama fallback error: {e2}")
                reply = "I'm afraid my neural pathways are a touch scrambled at the moment, sir."
                conv.add_assistant(reply)
        await respond(reply)

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            # X-ray (2026-07-01 mic debugging): first occurrence of each message
            # type per connection — shows exactly what the app transmits (or
            # doesn't) when the user taps/speaks, without logging every frame.
            if mtype not in state.setdefault("_seen_types", set()):
                state["_seen_types"].add(mtype)
                log.info(f"[ws] first '{mtype}' message on this connection")

            if mtype == "audio":
                # Server-side mute gate (2026-07-02): if this client declared
                # itself muted, drop frames HERE — don't transcribe, don't
                # burn CPU on Whisper. Belt-and-braces for the phone's mute
                # button: even if the app's capture teardown glitches and
                # keeps streaming (live bug, ninjahawk report 07-02), a muted
                # connection stays deaf. Client opt-in via {type:"mute"}.
                if state.get("mic_muted"):
                    if not state.get("_mute_drop_logged"):
                        state["_mute_drop_logged"] = True
                        log.info("[mic] dropping audio frames — connection is muted")
                    continue
                # Local Whisper path: browser streams a complete utterance as
                # base64 Int16 PCM @ 16kHz. Transcribe locally, then process.
                b64 = msg.get("pcm", "")
                if not b64:
                    # Capture ran but produced no samples — dead input route
                    # (Bluetooth mic, another app holding the audio session).
                    log.info("[stt] audio message with EMPTY pcm — capture alive, input silent")
                    continue
                # Diagnosis breadcrumb (2026-07-01, phone mic debugging): one line
                # per connection proving audio IS reaching the backend — its
                # absence during a live voice test pins the bug on the app's
                # capture side, not the pipeline.
                if not state.get("_audio_seen"):
                    state["_audio_seen"] = True
                    log.info("[stt] first audio frame from this connection — mic capture is reaching the backend")
                try:
                    pcm = stt.pcm_int16_bytes_to_float32(base64.b64decode(b64))
                    # Whisper is CPU-bound and blocking — run off the event loop
                    text = await asyncio.to_thread(stt.transcribe_pcm, pcm)
                except Exception as e:
                    log.error(f"[stt] transcription error: {e}")
                    await _safe_send(ws, {"type": "status", "state": "idle"})
                    continue
                if text:
                    log.info(f"[stt] voice transcript: {text[:80]!r}")
                    await process_transcript(text)
                else:
                    # silence / no speech detected
                    log.info("[stt] audio frame transcribed to empty (silence/noise)")
                    await _safe_send(ws, {"type": "status", "state": "idle"})

            elif mtype == "transcript":
                # Browser Web Speech fallback AND the typed chat HUD. `typed`
                # bypasses the wake-word gate.
                _text = msg.get("text", "")
                _typed = msg.get("typed", False)
                # PC tier with phone tools advertised: the turn may call a phone tool that
                # blocks on a tool_result THIS loop must keep reading for — so run it as its
                # own task (awaiting inline would deadlock the only reader). Serialize turns.
                if client_kind == "mobile" and state.get("phone_tools"):
                    async def _run_pc_turn(t=_text, ty=_typed):
                        async with _turn_lock:
                            await process_transcript(t, typed=ty)
                    asyncio.create_task(_run_pc_turn())
                else:
                    await process_transcript(_text, typed=_typed)

            elif mtype == "hello":
                # PC-tier handshake: the phone advertises the OrbToolKit tools it can run,
                # its connector state, and its APNs token. Store per-connection; the agent
                # loop relays to these tools and proactivity uses the token.
                state["phone_tools"] = msg.get("tools") or []
                state["phone_connectors"] = msg.get("connectors") or {}
                state["push_token"] = msg.get("pushToken") or ""
                state["ai_name"] = (msg.get("aiName") or msg.get("ai_name") or "").strip()
                state["user_name"] = (msg.get("userName") or msg.get("user_name") or "").strip()
                log.info(f"[pc] hello: {len(state['phone_tools'])} phone tools, "
                         f"connectors={state['phone_connectors']}, "
                         f"push={'yes' if state['push_token'] else 'no'}, "
                         f"ai={state['ai_name'] or '(default)'}, "
                         f"user={state['user_name'] or '(default)'}")
                if state["push_token"]:
                    try:
                        import apns
                        apns.register_token(state["push_token"])
                    except Exception as e:
                        log.debug(f"[apns] token register skipped: {e}")
                # Apply any calendar events a synthesis review queued while no
                # phone was connected (most likely the 6am run — see
                # proactive_engine.apply_pending_calendar_actions). Only worth
                # trying if this phone actually has calendar write access;
                # backgrounded so a 25s-per-item relay round-trip never blocks
                # this connection's WS receive loop.
                if state["phone_connectors"].get("calendar"):
                    import proactive_engine as _pe
                    asyncio.create_task(_pe.apply_pending_calendar_actions(remote_dispatch))
                # READY signal (2026-07-01, for the app's launch loading screen):
                # hello is fully processed — voice can be accepted the moment the
                # app hears this. The loading screen holds until {type:"ready"}
                # arrives, then the user just talks (no tap, no wake word).
                await _safe_send(ws, {
                    "type": "ready", "voice": True,
                    "brain": brain.effective_agent_brain(),
                    "persona": PERSONA.display_name,
                })

            elif mtype == "mute":
                # {type:"mute", on:true|false} — client mirrors its mute button
                # to the server so muting is REAL even when the app-side capture
                # pipeline misbehaves (07-02 home-screen mute bug). Additive:
                # nothing sends this yet; offered to the iOS side in
                # MAC_DELEGATION. Un-mute clears the drop-log latch so the next
                # mute logs again.
                state["mic_muted"] = bool(msg.get("on", True))
                state["_mute_drop_logged"] = False
                log.info(f"[mic] client set mute {'ON' if state['mic_muted'] else 'OFF'} "
                         "(server-side frame gate)")

            elif mtype == "tool_result":
                # The phone returned a relayed tool's result — resolve the parked future so
                # remote_dispatch (running in the turn task) can continue the agent loop.
                fut = _pending_tools.pop(msg.get("id", ""), None)
                if fut and not fut.done():
                    fut.set_result(msg.get("content", ""))

            elif mtype == "context":
                # Connector snapshot from the client (mobile on-device calendar/
                # reminders/contacts). Stash per-connection; injected into the brain
                # context on the next turn(s) so voice queries can use it. Additive:
                # desktop clients simply never send this.
                state["connector_context"] = (msg.get("text") or "").strip()
                log.info(f"[context] connector snapshot updated "
                         f"({len(state['connector_context'])} chars)")
                # Also feed to proactive engine so scans have phone data
                try:
                    import proactive_engine as _pe
                    _pe.update_connector_context(state["connector_context"])
                except Exception:
                    pass

            elif mtype == "ptt":
                # Push-to-talk arm (mobile orb tap): accept the NEXT utterance
                # without the wake word for a short window.
                state["ptt_until"] = time.time() + 8.0
                log.info("[ptt] armed (8s, wake word bypassed)")

            elif mtype == "fix_self":
                project_dir = str(Path(__file__).parent)
                asyncio.create_task(run_desktop_command(
                    f'start wt -d "{project_dir}" cmd /k claude'
                ))

    except WebSocketDisconnect:
        log.info("Client disconnected")
    except Exception as e:
        log.error(f"WebSocket error: {e}")
    finally:
        _CLIENTS.discard(ws)
        _MOBILE_CLIENTS.discard(ws)

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    claude_status = "found" if shutil.which("claude") else "NOT FOUND"
    log.info(f"Orb backend starting — persona: {PERSONA.display_name}, port {PORT}")
    log.info(f"Default brain: {brain.effective_agent_brain()} (via Claude Code CLI: "
             f"{claude_status}); local floor: {OLLAMA_MODEL} @ {OLLAMA_URL}")
    log.info(f"Voice: {TTS_VOICE}")
    log.info(f"Desktop commands: {len(DESKTOP_COMMANDS)} registered")
    log.info("=" * 60)
    log.info(f"  PAIRING TOKEN (Orb app -> Settings -> Your Server): {ORB_TOKEN}")
    log.info("  (localhost connections are always allowed without token)")
    log.info("=" * 60)
    uvicorn.run(app, host="0.0.0.0", port=PORT)

"""Screen vision — analyze what's currently on the PC screen using a vision model.

Two backends, tried in order:
  1. Gemini 1.5 Flash (free API, fast) — if GEMINI_API_KEY is set.
  2. claude -p (Claude Code native) — CC can read image files directly with the
     Read tool, no API key or encoding hacks needed.
"""

import asyncio
import base64
import os
import tempfile
import time
from pathlib import Path

import httpx

from tool_registry import tool

_LAST_ANALYSIS: dict = {}   # {description, timestamp, image_path}


async def _vision_gemini(image_bytes: bytes, question: str) -> str:
    """Call Gemini 1.5 Flash vision (free tier) with an image."""
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        return ""
    b64 = base64.b64encode(image_bytes).decode()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={api_key}"
    body = {
        "contents": [{
            "parts": [
                {"inline_data": {"mime_type": "image/jpeg", "data": b64}},
                {"text": question or "Describe what is currently on this computer screen. Be specific about open apps, visible content, any notifications, errors, or completed tasks."},
            ]
        }]
    }
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.post(url, json=body)
        r.raise_for_status()
        data = r.json()
        return data["candidates"][0]["content"]["parts"][0]["text"]


async def _vision_claude_cli(image_path: str, question: str) -> str:
    """Use Claude Code's native image reading — pass the file path and let CC do it."""
    q = question or "Describe what is on this screen. Note any completed tasks, errors, notifications, or important activity."
    prompt = f"{q}\n\nScreenshot file: {image_path}"
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt, "--permission-mode", "bypassPermissions",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=60.0)
        return stdout.decode("utf-8", errors="replace").strip()
    except Exception as e:
        return f"[vision error: {e}]"


async def capture_and_analyze(question: str = "") -> str:
    """Take a screenshot and analyze it. Returns a text description."""
    import screen_bridge
    if not screen_bridge.available():
        return "Screen capture is not available on this system."
    img_bytes = screen_bridge.capture_jpeg()
    if not img_bytes:
        return "Could not capture screen."

    # Try Gemini first (free, fast, good vision) — works straight off the bytes.
    result = await _vision_gemini(img_bytes, question)
    path = ""
    if not result:
        # Fall back to claude -p — it reads images off disk, so write a temp file.
        path = os.path.join(tempfile.gettempdir(), f"orb_vision_{int(time.time())}.jpg")
        try:
            Path(path).write_bytes(img_bytes)
            result = await _vision_claude_cli(path, question)
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    _LAST_ANALYSIS["description"] = result
    _LAST_ANALYSIS["timestamp"] = time.time()
    _LAST_ANALYSIS["image_path"] = path
    return result


def get_last_analysis() -> dict:
    return _LAST_ANALYSIS.copy()


@tool(
    name="analyze_screen",
    description=(
        "Take a screenshot and use vision AI to understand what's currently on the screen. "
        "Returns a detailed description of open apps, visible content, errors, notifications, "
        "or completed tasks. Use when the user asks 'what's on my screen', 'what does this "
        "error say', 'what am I looking at', or when you need to understand the current desktop state."
    ),
    parameters={
        "question": {
            "type": "string",
            "description": "Specific question about the screen (optional). E.g. 'what error is showing?'",
        }
    },
    required=[],
)
async def analyze_screen(question: str = "") -> str:
    return await capture_and_analyze(question)

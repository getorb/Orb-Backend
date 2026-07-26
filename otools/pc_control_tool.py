"""PC control — mouse, keyboard, window management.

Gives Orb hands on the desktop. Ask it to click things, type text,
press shortcuts, or bring windows into focus. All actions are confirmed
before execution when destructive.
"""

import asyncio
from tool_registry import tool

import pyautogui
import pyautogui as _pag

# Safety: never move the mouse too fast, pause between actions
_pag.PAUSE = 0.05
_pag.FAILSAFE = True   # move mouse to top-left corner to abort


@tool(
    name="open_url",
    description=(
        "Open a website in the default browser (Edge). Use for 'open X website', 'open a "
        "browser to Y', 'open a new window with Z' — a single fast call, not a coding task. "
        "Do NOT reach for run_claude_code for this; that spawns a full agent subprocess just "
        "to open a page, which is slow and pointless when this does it directly."
    ),
    parameters={
        "url": {"type": "string", "description": "URL or site name (e.g. 'reddit.com', or a full search URL)."},
        "new_window": {"type": "boolean", "description": "Open in a new browser window instead of a new tab. Default false."},
    },
    required=["url"],
)
async def open_url(url: str, new_window: bool = False) -> str:
    import connectors
    await asyncio.to_thread(connectors.open_url, url, new_window)
    return f"Opened {url}" + (" in a new window." if new_window else ".")


@tool(
    name="mouse_click",
    description=(
        "Click the mouse at a specific screen position. Use when the user asks you to "
        "click a button, open a file, or interact with a UI element. Combine with "
        "analyze_screen to find where to click."
    ),
    parameters={
        "x": {"type": "integer", "description": "X coordinate (pixels from left)"},
        "y": {"type": "integer", "description": "Y coordinate (pixels from top)"},
        "button": {"type": "string", "description": "left, right, or double (default: left)"},
    },
    required=["x", "y"],
)
async def mouse_click(x: int, y: int, button: str = "left") -> str:
    def _do():
        if button == "double":
            _pag.doubleClick(x, y)
        elif button == "right":
            _pag.rightClick(x, y)
        else:
            _pag.click(x, y)
    await asyncio.to_thread(_do)
    return f"Clicked {button} at ({x}, {y})."


@tool(
    name="type_text",
    description=(
        "Type text at the current cursor position, as if typing on the keyboard. "
        "Use to fill in text fields, compose messages, enter commands. "
        "Click the target field first with mouse_click."
    ),
    parameters={
        "text": {"type": "string", "description": "Text to type"},
        "press_enter": {"type": "boolean", "description": "Press Enter after typing (default false)"},
    },
    required=["text"],
)
async def type_text(text: str, press_enter: bool = False) -> str:
    def _do():
        _pag.typewrite(text, interval=0.03)
        if press_enter:
            _pag.press("enter")
    await asyncio.to_thread(_do)
    suffix = " + Enter" if press_enter else ""
    return f"Typed: {text[:60]}{'...' if len(text) > 60 else ''}{suffix}"


@tool(
    name="press_key",
    description=(
        "Press a keyboard key or shortcut. Use for hotkeys, navigation, shortcuts. "
        "Examples: 'ctrl+c', 'alt+tab', 'win', 'escape', 'f5', 'ctrl+shift+t'"
    ),
    parameters={
        "key": {"type": "string", "description": "Key or shortcut (e.g. 'ctrl+c', 'alt+tab', 'enter', 'escape')"},
    },
    required=["key"],
)
async def press_key(key: str) -> str:
    def _do():
        if "+" in key:
            parts = [p.strip() for p in key.split("+")]
            _pag.hotkey(*parts)
        else:
            _pag.press(key)
    await asyncio.to_thread(_do)
    return f"Pressed: {key}"


@tool(
    name="move_mouse",
    description="Move the mouse to a position without clicking. Useful for hovering to reveal tooltips.",
    parameters={
        "x": {"type": "integer", "description": "X coordinate"},
        "y": {"type": "integer", "description": "Y coordinate"},
    },
    required=["x", "y"],
)
async def move_mouse(x: int, y: int) -> str:
    await asyncio.to_thread(_pag.moveTo, x, y, duration=0.2)
    return f"Mouse moved to ({x}, {y})."


@tool(
    name="get_screen_size",
    description="Get the screen resolution. Use before clicking to understand coordinate space.",
    parameters={},
    required=[],
)
async def get_screen_size() -> str:
    w, h = await asyncio.to_thread(_pag.size)
    return f"Screen size: {w}x{h} pixels."


@tool(
    name="scroll",
    description="Scroll the mouse wheel at a position. Positive clicks = scroll up, negative = scroll down.",
    parameters={
        "x": {"type": "integer", "description": "X coordinate to scroll at"},
        "y": {"type": "integer", "description": "Y coordinate to scroll at"},
        "clicks": {"type": "integer", "description": "Number of scroll clicks (positive=up, negative=down)"},
    },
    required=["x", "y", "clicks"],
)
async def scroll(x: int, y: int, clicks: int) -> str:
    await asyncio.to_thread(_pag.scroll, clicks, x=x, y=y)
    direction = "up" if clicks > 0 else "down"
    return f"Scrolled {direction} {abs(clicks)} clicks at ({x}, {y})."

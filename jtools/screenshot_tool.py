import screen_bridge
from tool_registry import tool

# Injected by server_win.py at startup so the tool can call _take_and_dispatch_screenshot
# from within the live WS connection context (ContextVar is set correctly at that point).
_dispatch_fn = None


@tool(
    name="take_screenshot",
    description=(
        "Capture a screenshot of the PC desktop and send it to the user's device. "
        "Use when the user asks to see the screen, wants to know what's on the desktop, "
        "asks you to show them something on the PC, or says 'show me your screen'."
    ),
    parameters={},
    required=[],
)
async def take_screenshot() -> str:
    if not screen_bridge.available():
        return "Screen capture is not available on this system."
    if _dispatch_fn is None:
        return "Screenshot dispatch not initialized."
    status = _dispatch_fn()
    if status == "failed":
        return "Screenshot capture failed."
    if status == "mobile":
        return "Screenshot sent to your device via the app."
    if status == "desktop":
        return "Screenshot captured and saved to your Desktop."
    return "Screenshot captured."

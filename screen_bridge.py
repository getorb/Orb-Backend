"""
Desktop bridge — screen capture (Windows).

The first step of the desktop bridge (see ORB_ROADMAP.md, Pillar A): the phone
asks "show me my screen" and the PC sends back a snapshot of whatever is on it.
Read-only and non-destructive — it only ever *reads* the framebuffer; it never
injects input. (Remote *control* is a separate, opt-in feature designed later.)
This is also the foundation for the live screen stream.

Capture uses Pillow's ImageGrab (already a dependency — no new package). The
pure interface is importable on any OS so it's unit-testable headless; the
actual grab returns None off-Windows/-macOS or on any failure, so the voice loop
never crashes on a capture error.

NOTE: this is the live, in-use Windows path. The legacy `screen.py` is the old
macOS AppleScript/`screencapture` helper used only by the unused `server.py`;
it is not touched here.
"""

from __future__ import annotations

import io
import sys
import time

# Most-recent capture, so a request that just happened can be re-served (e.g. a
# reconnecting phone) without grabbing again. Single-slot on purpose.
_LAST: dict = {"jpeg": None, "ts": 0.0, "w": 0, "h": 0}


def available() -> bool:
    """True if screen capture is usable on this machine."""
    if sys.platform not in ("win32", "darwin"):
        return False
    try:
        from PIL import ImageGrab  # noqa: F401
        return True
    except Exception:
        return False


def capture_jpeg(max_width: int = 1600, quality: int = 72,
                 all_screens: bool = False) -> bytes | None:
    """Grab the screen and return JPEG bytes (downscaled to `max_width`), or None.

    JPEG (not PNG) because a screenshot headed to a phone over Tailscale wants to
    be small; ~72 quality at 1600px is crisp enough to read UI text and a
    fraction of a PNG's size. `all_screens=True` spans every monitor; default is
    the primary display (what you usually mean by "my screen").
    """
    try:
        from PIL import Image, ImageGrab
    except Exception:
        return None
    try:
        img = ImageGrab.grab(all_screens=all_screens)
    except Exception:
        # Some headless / RDP / permission situations raise here — fail soft.
        return None
    try:
        if img.mode != "RGB":
            img = img.convert("RGB")
        w, h = img.size
        if max_width and w > max_width:
            new_h = max(1, round(h * max_width / w))
            # LANCZOS keeps small UI text legible after the downscale.
            img = img.resize((max_width, new_h), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=int(quality), optimize=True)
        data = buf.getvalue()
        _LAST.update(jpeg=data, ts=time.time(), w=img.size[0], h=img.size[1])
        return data
    except Exception:
        return None


def last_capture() -> bytes | None:
    """The most recent captured JPEG, if any (re-serve without re-grabbing)."""
    return _LAST.get("jpeg")

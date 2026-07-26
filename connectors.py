"""
Action connectors — voice commands that take REAL local action.

This is Orb's first step from narrator to actuator: media transport
(play/pause/next/prev), system volume, "start my music", and opening saved
sites / raw URLs / saved apps.

SAFETY (hard constraint — see memory: orb-safety-guardrail):
  Everything here is an ALLOWLIST of non-destructive actions. There is NO
  file deletion, NO arbitrary shell from voice, NO system mutation beyond
  media keys / volume / launching an app or URL. Media + volume go through
  Win32 virtual-key taps. Launching uses os.startfile / webbrowser (no shell
  string interpolation), and URLs are validated to http(s) first. Adding a
  new capability here should keep that property.

Preferences live in data/preferences.json so Orb can be tuned to "how the
user likes things" (favourite music source, saved sites/apps). The matcher
is pure/deterministic and importable on any OS (so it's unit-testable
headless); only the action *execution* is Windows-specific and no-ops
elsewhere.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

# ---------------------------------------------------------------------------
# Preferences store
# ---------------------------------------------------------------------------

PREFS_PATH = Path(__file__).parent / "data" / "preferences.json"

_DEFAULT_PREFS: dict = {
    # Music source for "start my music":
    #   - youtube_playlist: a YouTube playlist URL. If set, "start music"
    #     opens a RANDOM track from it each time (via yt-dlp), then the
    #     playlist continues. Empty -> fall back to start_cmd.
    #   - start_cmd: a URI/URL/path opened via os.startfile (Spotify's
    #     registered protocol by default). Shell-free.
    "music": {"youtube_playlist": "", "start_cmd": "spotify:"},
    # Named sites: "open my email" -> the URL. User-extensible later via the
    # self-modification phase.
    "sites": {
        "email": "https://mail.google.com",
        "gmail": "https://mail.google.com",
        "inbox": "https://mail.google.com",
        "calendar": "https://calendar.google.com",
        "drive": "https://drive.google.com",
        "youtube": "https://youtube.com",
        "github": "https://github.com",
        "reddit": "https://reddit.com",
    },
    # Named apps the user can launch by name ("open obsidian"). Empty by
    # default — populated later so a broad "open X" can't fire unexpectedly.
    "apps": {},
    # How many VK_VOLUME_UP/DOWN taps one "louder"/"quieter" sends (~2% each).
    "volume_step_presses": 4,
}


def load_prefs() -> dict:
    """Load prefs, filling any missing top-level keys from the defaults."""
    prefs = dict(_DEFAULT_PREFS)
    try:
        if PREFS_PATH.exists():
            saved = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                for k, v in saved.items():
                    prefs[k] = v
    except Exception:
        pass  # corrupt prefs -> fall back to defaults, never crash the voice loop
    return prefs


def save_prefs(prefs: dict) -> None:
    PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


def get_pref(path: list[str], default=None):
    node = load_prefs()
    for key in path:
        if isinstance(node, dict) and key in node:
            node = node[key]
        else:
            return default
    return node


# ---------------------------------------------------------------------------
# Low-level safe actions (Windows virtual-key taps; no-op off Windows)
# ---------------------------------------------------------------------------

_VK = {
    "next": 0xB0,        # VK_MEDIA_NEXT_TRACK
    "prev": 0xB1,        # VK_MEDIA_PREV_TRACK
    "stop": 0xB2,        # VK_MEDIA_STOP
    "play_pause": 0xB3,  # VK_MEDIA_PLAY_PAUSE
    "vol_mute": 0xAD,    # VK_VOLUME_MUTE
    "vol_down": 0xAE,    # VK_VOLUME_DOWN
    "vol_up": 0xAF,      # VK_VOLUME_UP
}


def _tap(vk: int, times: int = 1) -> None:
    if sys.platform != "win32":
        return
    import ctypes
    KEYEVENTF_KEYUP = 0x0002
    user32 = ctypes.windll.user32
    for _ in range(max(1, times)):
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def media_play_pause() -> None:
    _tap(_VK["play_pause"])


def media_next() -> None:
    _tap(_VK["next"])


def media_prev() -> None:
    _tap(_VK["prev"])


def media_stop() -> None:
    _tap(_VK["stop"])


def volume_up() -> None:
    _tap(_VK["vol_up"], get_pref(["volume_step_presses"], 4))


def volume_down() -> None:
    _tap(_VK["vol_down"], get_pref(["volume_step_presses"], 4))


def volume_mute() -> None:
    _tap(_VK["vol_mute"])  # toggles mute/unmute


def _launch(target: str) -> None:
    """Open a URI / URL / registered app protocol without a shell."""
    if sys.platform != "win32":
        return
    try:
        os.startfile(target)  # ShellExecute — handles URIs, URLs, paths
    except Exception:
        # Last-resort for bare app tokens (e.g. "spotify"); still no shell
        # string interpolation of user text beyond the single token.
        import subprocess
        try:
            subprocess.Popen(["cmd", "/c", "start", "", target],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass


# Cache of playlist video IDs, keyed by playlist URL, so "start music" is
# snappy after the first fetch and still picks a different track each time.
_playlist_cache: dict[str, list[str]] = {}


def _extract_list_id(url: str) -> str | None:
    m = re.search(r"[?&]list=([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None


def _playlist_video_ids(playlist_url: str) -> list[str]:
    """Flat-list a YouTube playlist's video IDs via yt-dlp (no download).
    Cached per URL for the session. Returns [] on any failure."""
    if playlist_url in _playlist_cache:
        return _playlist_cache[playlist_url]
    ids: list[str] = []
    try:
        import subprocess
        out = subprocess.run(
            ["yt-dlp", "--flat-playlist", "--no-warnings", "--print", "id",
             playlist_url],
            capture_output=True, text=True, timeout=40,
        )
        ids = [ln.strip() for ln in out.stdout.splitlines() if ln.strip()]
    except Exception:
        ids = []
    if ids:
        _playlist_cache[playlist_url] = ids
    return ids


def start_music() -> None:
    """Start music — returns IMMEDIATELY so Orb can confirm right away; the
    slow part (yt-dlp playlist fetch) runs in a background thread so the user
    isn't left waiting in silence."""
    threading.Thread(target=_do_start_music, daemon=True).start()


def _do_start_music() -> None:
    import random
    playlist = (get_pref(["music", "youtube_playlist"], "") or "").strip()
    if playlist:
        ids = _playlist_video_ids(playlist)
        if ids:
            vid = random.choice(ids)
            list_id = _extract_list_id(playlist)
            url = f"https://www.youtube.com/watch?v={vid}"
            if list_id:
                url += f"&list={list_id}"
            open_url(url)
            return
        # yt-dlp unavailable / failed — just open the playlist page.
        open_url(playlist)
        return
    target = get_pref(["music", "start_cmd"], "spotify:")
    _launch(target)
    # Delayed play so a freshly-opened, paused player actually starts.
    threading.Timer(3.0, media_play_pause).start()


def open_url(url: str, new_window: bool = False) -> None:
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    webbrowser.open(url, new=1 if new_window else 0)


# ---------------------------------------------------------------------------
# Matcher — deterministic, pure, testable
# ---------------------------------------------------------------------------

@dataclass
class Action:
    key: str                       # allowlist id, e.g. "media.play_pause"
    reply: str                     # spoken confirmation; "{addr}" -> persona address
    run: Callable[[], None] = field(default=lambda: None)


# The allowlist. Anything the matcher can produce must be in here. Keeping it
# explicit makes the safety surface auditable at a glance.
ALLOWED_ACTIONS = {
    "music.start", "media.play_pause", "media.pause", "media.stop",
    "media.next", "media.prev",
    "volume.up", "volume.down", "volume.mute", "volume.unmute",
    "open.site", "open.url", "open.app",
}


def _norm(text: str) -> str:
    return text.lower().strip().rstrip(".!?,;:").strip()


# Exact-or-prefix command tables (matched like DESKTOP_COMMANDS: the whole
# utterance equals the trigger, or starts with "<trigger> "). Anchored this
# way so mid-sentence words ("what's next on my list") never hijack.
_START_MUSIC = (
    "start music", "start my music", "start the music", "play my music",
    "put on music", "put on some music", "play some music", "put music on",
    "fire up some music", "start some music",
)
_PLAY = ("play", "resume", "unpause", "play music", "resume music",
         "play the music", "resume the music", "unpause music")
_PAUSE = ("pause", "pause music", "pause the music", "pause it", "pause the song")
_STOP_MUSIC = ("stop music", "stop the music", "stop playing")
_NEXT = ("next", "next song", "next track", "skip", "skip song", "skip this",
         "skip this song", "skip track", "skip the song")
_PREV = ("previous", "previous song", "previous track", "last song",
         "go back a song", "play the last song", "back a track")
_VOL_UP = ("volume up", "turn it up", "turn up", "louder", "turn up the volume",
           "raise the volume", "increase the volume", "increase volume",
           "turn the volume up", "crank it up", "bump it up", "make it louder")
_VOL_DOWN = ("volume down", "turn it down", "turn down", "quieter", "softer",
             "lower the volume", "turn down the volume", "decrease the volume",
             "decrease volume", "turn the volume down", "make it quieter")
_VOL_MUTE = ("mute the volume", "mute the sound", "mute the audio",
             "mute sound", "mute audio", "mute the system", "mute system")
_VOL_UNMUTE = ("unmute", "unmute the volume", "unmute the sound",
               "unmute the audio", "unmute audio")

# Parameterized openers (anchored at start).
_OPEN_MY = re.compile(
    r"^(?:open|pull up|bring up|go to|launch)\s+my\s+(.+)$", re.IGNORECASE)
_OPEN_URL = re.compile(
    r"^(?:open|go to|pull up|bring up)\s+"
    r"((?:https?://)?[a-z0-9-]+(?:\.[a-z0-9-]+)+(?:/[\w\-./?%&=#]*)?)\s*$",
    re.IGNORECASE)
# General opener: "open / go to / pull up <single-token>" — a bare website or
# app NAME (no spaces). Single token only, so multi-word phrases aren't treated
# as sites ("open the pod bay doors" won't match). This is what gives Orb
# general website logic ("open reddit" -> reddit.com) instead of only hardcoded
# links.
_OPEN_GENERIC = re.compile(
    r"^(?:open|launch|start|fire up|go to|pull up|bring up|take me to)\s+"
    r"([a-z0-9][a-z0-9.\-]{1,40})$", re.IGNORECASE)


def _matches(norm: str, triggers: tuple[str, ...]) -> bool:
    return any(norm == t or norm.startswith(t + " ") for t in triggers)


def match_connector(text: str) -> Action | None:
    """Map an utterance to an allowlisted Action, or None to fall through.

    Order matters: 'start music' before plain 'play'; specific volume mutes
    before the open-app catch-all. The open-app catch-all only fires for a
    name the user has actually saved in prefs.apps, so a broad 'open X' can't
    do something surprising."""
    norm = _norm(text)
    if not norm:
        return None

    # Music / media transport
    if _matches(norm, _START_MUSIC):
        return Action("music.start", "Starting your music, {addr}.", start_music)
    if _matches(norm, _STOP_MUSIC):
        return Action("media.stop", "Music stopped, {addr}.", media_stop)
    if _matches(norm, _PAUSE):
        return Action("media.pause", "Paused, {addr}.", media_play_pause)
    if _matches(norm, _PLAY):
        return Action("media.play_pause", "Playing, {addr}.", media_play_pause)
    if _matches(norm, _NEXT):
        return Action("media.next", "Next track, {addr}.", media_next)
    if _matches(norm, _PREV):
        return Action("media.prev", "Previous track, {addr}.", media_prev)

    # Volume
    if _matches(norm, _VOL_UP):
        return Action("volume.up", "Turned it up, {addr}.", volume_up)
    if _matches(norm, _VOL_DOWN):
        return Action("volume.down", "Turned it down, {addr}.", volume_down)
    if _matches(norm, _VOL_MUTE):
        return Action("volume.mute", "Muted, {addr}.", volume_mute)
    if _matches(norm, _VOL_UNMUTE):
        return Action("volume.unmute", "Unmuted, {addr}.", volume_mute)

    # "open my <name>" -> saved site
    m = _OPEN_MY.match(text.strip())
    if m:
        name = _norm(m.group(1))
        sites = load_prefs().get("sites", {})
        url = sites.get(name)
        if url:
            return Action("open.site", f"Opening your {name}, {{addr}}.",
                          lambda u=url: open_url(u))
        # Saved app by "open my <name>"?
        apps = load_prefs().get("apps", {})
        if name in apps:
            target = apps[name]
            return Action("open.app", f"Opening {name}, {{addr}}.",
                          lambda t=target: _launch(t))
        return Action("open.site",
                      f"I don't have anything saved as '{name}', {{addr}}.")

    # "go to <url>" -> raw URL (only when it really looks like a domain)
    m = _OPEN_URL.match(text.strip())
    if m:
        url = m.group(1)
        if not re.fullmatch(r"[\d.]+", url):  # not a version/number
            host = url.split("/")[0].replace("https://", "").replace("http://", "")
            return Action("open.url", f"Opening {host}, {{addr}}.",
                          lambda u=url: open_url(u))

    # General "open/go to/pull up <name>": a saved app, a saved site, or a
    # bare name opened as a website (reddit -> reddit.com). This is the general
    # "that's a website, just open it" logic — no per-site hardcoding needed.
    m = _OPEN_GENERIC.match(text.strip())
    if m:
        raw = m.group(1).strip()
        key = _norm(raw)
        apps = load_prefs().get("apps", {})
        if key in apps:
            return Action("open.app", f"Opening {key}, {{addr}}.",
                          lambda t=apps[key]: _launch(t))
        sites = load_prefs().get("sites", {})
        if key in sites:
            return Action("open.site", f"Opening {key}, {{addr}}.",
                          lambda u=sites[key]: open_url(u))
        host = raw if "." in raw else f"{raw}.com"
        return Action("open.url", f"Opening {raw}, {{addr}}.",
                      lambda u=host: open_url(u))

    return None

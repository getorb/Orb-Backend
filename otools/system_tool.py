"""System info tool — CPU, RAM, disk, uptime via psutil."""

import logging
import platform
import socket
import time
from datetime import datetime

from tool_registry import tool

log = logging.getLogger("orb.tools.system")

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d} day{'s' if d != 1 else ''}")
    if h:
        parts.append(f"{h} hour{'s' if h != 1 else ''}")
    if m and not d:
        parts.append(f"{m} minute{'s' if m != 1 else ''}")
    return " and ".join(parts) or "less than a minute"


def _gb(bytes_val: int) -> float:
    return round(bytes_val / 1_073_741_824, 1)


@tool(
    name="get_system_info",
    description=(
        "Report the user's system status — CPU usage, RAM usage, disk space, "
        "uptime. Use when the user asks 'how's my system', 'how much RAM is free', "
        "'what's the CPU at', 'check my disk space', 'how long has the computer "
        "been running'."
    ),
    parameters={
        "metric": {
            "type": "string",
            "description": "Which stat to report. 'all' (default), 'cpu', 'memory', 'disk', 'uptime'.",
            "enum": ["all", "cpu", "memory", "disk", "uptime"],
        },
    },
    required=[],
)
async def get_system_info(metric: str = "all") -> str:
    if not _PSUTIL:
        return "psutil isn't installed — I can't read system stats."
    metric = (metric or "all").lower()
    parts: list[str] = []

    if metric in ("all", "cpu"):
        cpu_pct = psutil.cpu_percent(interval=0.4)
        cores = psutil.cpu_count(logical=True)
        parts.append(f"CPU at {cpu_pct:.0f}% across {cores} threads")

    if metric in ("all", "memory"):
        mem = psutil.virtual_memory()
        parts.append(f"RAM {_gb(mem.used)} of {_gb(mem.total)} GB used ({mem.percent:.0f}%)")

    if metric in ("all", "disk"):
        try:
            disk = psutil.disk_usage("C:\\")
            parts.append(f"C: drive {_gb(disk.used)} of {_gb(disk.total)} GB used ({disk.percent:.0f}%)")
        except Exception:
            pass

    if metric in ("all", "uptime"):
        try:
            up = time.time() - psutil.boot_time()
            parts.append(f"up for {_fmt_uptime(up)}")
        except Exception:
            pass

    if not parts:
        return "I'm not sure which stat you wanted."

    host = socket.gethostname()
    osinfo = f"{platform.system()} {platform.release()}"
    return f"{host} ({osinfo}): " + "; ".join(parts) + "."

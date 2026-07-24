"""Generalized data-point watcher — periodic, zero-LLM collection of external
data (stock prices first; built so more source types can be added later)
feeding into the same review pipeline already used for PC-local signals.

Philosophy (per the user, 2026-06-30): don't hand-pick what's "interesting"
with fixed rules — collect good data cheaply and let the twice-daily LLM
review find the patterns, since that's specifically what it's good at good
data in, good output. This module only ever collects and logs; it NEVER
calls a model — same discipline as the 15s PC tick.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("orb.data_watcher")

_DATA_DIR = Path(__file__).parent.parent / "data"
_WATCH_LOG = _DATA_DIR / "watched_data.jsonl"
_WATCHLIST_FILE = _DATA_DIR / "watchlist.json"
_POLL_INTERVAL = 3600  # 1h — matches "hourly stock price"; cheap + keyless, no reason to go faster

_watchlist: dict[str, dict] = {}  # lowercased key -> {"target", "source_type", "added"}


def _load_watchlist() -> None:
    global _watchlist
    if _WATCHLIST_FILE.exists():
        try:
            _watchlist = json.loads(_WATCHLIST_FILE.read_text(encoding="utf-8"))
        except Exception:
            _watchlist = {}


def _save_watchlist() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        _WATCHLIST_FILE.write_text(json.dumps(_watchlist, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def add_watch(target: str, source_type: str = "stock") -> str:
    # Always reload first — MCP/stdio processes never run the poller that
    # otherwise calls _load_watchlist once at start, so an empty in-memory
    # dict would clobber the on-disk list on save.
    _load_watchlist()
    key = target.strip().lower()
    if not key:
        return "Need something to watch."
    if key in _watchlist:
        return f"Already watching {target}."
    _watchlist[key] = {"target": target, "source_type": source_type,
                       "added": datetime.now().isoformat(timespec="seconds")}
    _save_watchlist()
    return f"Now watching {target} ({source_type}), checked hourly — I'll mention it if a pattern's worth flagging, not every check."


def remove_watch(target: str) -> str:
    _load_watchlist()
    key = target.strip().lower()
    if key not in _watchlist:
        return f"Not currently watching {target}."
    del _watchlist[key]
    _save_watchlist()
    return f"Stopped watching {target}."


def list_watches() -> list[dict]:
    _load_watchlist()
    return list(_watchlist.values())


def _log_point(source_type: str, target: str, value: dict) -> None:
    entry = {"ts": datetime.now().isoformat(timespec="seconds"),
             "source_type": source_type, "target": target, "value": value}
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _WATCH_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


async def _collect_one(target: str, source_type: str) -> None:
    if source_type == "stock":
        try:
            from jtools.finance_tool import get_stock
            d = await get_stock(target)
            if d:
                _log_point("stock", target, {"symbol": d["symbol"], "price": d["price"],
                                             "currency": d["currency"]})
        except Exception as e:
            log.warning(f"[data_watcher] stock collect failed for {target}: {e}")


async def _collect_all() -> None:
    for w in list(_watchlist.values()):
        await _collect_one(w["target"], w["source_type"])


def _trim(keep_days: int = 30) -> None:
    """Watched data is small (one point/hour/target) — keep more history than
    the 7-day event log, patterns can take weeks to show up."""
    if not _WATCH_LOG.exists():
        return
    try:
        lines = _WATCH_LOG.read_text(encoding="utf-8").splitlines()
    except Exception:
        return
    cutoff = datetime.now() - timedelta(days=keep_days)
    kept = []
    for line in lines:
        if not line.strip():
            continue
        try:
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) >= cutoff:
                kept.append(line)
        except Exception:
            continue
    try:
        _WATCH_LOG.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except Exception:
        pass


def recent_summary(hours: float = 24.0) -> str:
    """Formatted block for the review prompt — recent points per watched target."""
    if not _watchlist:
        return "  (nothing being watched — use watch_stock to add something)"
    if not _WATCH_LOG.exists():
        return "  (watching, no data points collected yet)"
    cutoff = datetime.now() - timedelta(hours=hours)
    by_target: dict[str, list] = {}
    try:
        for line in _WATCH_LOG.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            e = json.loads(line)
            if datetime.fromisoformat(e["ts"]) < cutoff:
                continue
            by_target.setdefault(e["target"], []).append(e)
    except Exception:
        return "  (unavailable)"
    if not by_target:
        return "  (watching, no data points in this window yet)"
    lines = []
    for target, points in by_target.items():
        vals = [p["value"] for p in points]
        if points and points[0]["source_type"] == "stock":
            prices = [v["price"] for v in vals]
            cur = vals[-1].get("currency", "")
            lines.append(f"  {target}: {len(prices)} point(s) over {hours:.0f}h, "
                         f"{prices[0]:.2f} -> {prices[-1]:.2f} {cur}")
        else:
            lines.append(f"  {target}: {len(vals)} point(s)")
    return "\n".join(lines)


async def run_forever() -> None:
    _load_watchlist()
    log.info(f"[data_watcher] started — {len(_watchlist)} target(s), polling every {_POLL_INTERVAL}s")
    tick = 0
    while True:
        try:
            await _collect_all()
            tick += 1
            if tick % 24 == 0:  # roughly once a day at 1h interval
                _trim()
        except Exception as e:
            log.warning(f"[data_watcher] collect error: {e}")
        await asyncio.sleep(_POLL_INTERVAL)

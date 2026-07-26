"""Voice-facing tools for the data watchlist (otools/data_watcher.py) — track
something over time instead of just checking it once."""

from tool_registry import tool
from otools import data_watcher


@tool(
    name="watch_stock",
    description=(
        "Start tracking a stock/crypto/index price over time — checked hourly and logged "
        "quietly, so patterns can surface in the twice-daily review, NOT a one-time check "
        "(use get_stock for 'what's X trading at right now'). Use when the user says 'keep "
        "an eye on X', 'track Y price', 'watch Z for me going forward'."
    ),
    parameters={
        "target": {"type": "string", "description": "Company name, ticker, crypto, or index to watch."},
    },
    required=["target"],
)
async def watch_stock(target: str) -> str:
    return data_watcher.add_watch(target, "stock")


@tool(
    name="unwatch",
    description="Stop tracking something previously started with watch_stock.",
    parameters={
        "target": {"type": "string", "description": "The target to stop watching."},
    },
    required=["target"],
)
async def unwatch(target: str) -> str:
    return data_watcher.remove_watch(target)


@tool(
    name="list_watches",
    description="List everything currently being tracked over time (see watch_stock).",
    parameters={},
    required=[],
)
async def list_watches_tool() -> str:
    watches = data_watcher.list_watches()
    if not watches:
        return "Nothing being watched right now."
    return "\n".join(f"{w['target']} ({w['source_type']}, since {w['added'][:10]})" for w in watches)

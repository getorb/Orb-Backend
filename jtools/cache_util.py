"""Lightweight TTL memoization for tool calls safe to repeat within a short
window — cuts redundant network calls (same weather/stock/image query asked
twice in a few minutes) without touching the agent loop at all. This is a
plain function-level decorator, so it composes cleanly under @tool: the
registry only ever sees the wrapped callable and calls it by keyword args,
same as any tool function.

Only use this where staleness over a few minutes is genuinely fine — NOT for
anything time-sensitive beyond that (this is why watch_stock's periodic
history collection bypasses this entirely and calls finance_tool directly).
"""

from __future__ import annotations

import functools
import time
from typing import Callable


def ttl_cache(seconds: float) -> Callable:
    def decorator(fn: Callable) -> Callable:
        cache: dict[tuple, tuple[float, object]] = {}

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            key = (args, tuple(sorted(kwargs.items())))
            now = time.time()
            hit = cache.get(key)
            if hit and now - hit[0] < seconds:
                return hit[1]
            value = await fn(*args, **kwargs)
            cache[key] = (now, value)
            if len(cache) > 200:  # opportunistic cleanup, never grows unbounded
                cutoff = now - seconds
                for k in [k for k, (t, _) in cache.items() if t < cutoff]:
                    cache.pop(k, None)
            return value

        return wrapper
    return decorator

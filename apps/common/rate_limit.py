"""A fixed-window counter in the cache: the one way a route says "too many, wait"."""
from __future__ import annotations

from django.core.cache import cache


def over(key: str, limit: int, window: int) -> bool:
    """Count this request against `key`. True when it is over `limit` per `window` seconds."""
    if cache.add(key, 1, timeout=window):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        # Expired between add() and incr(); this request starts a fresh window.
        cache.add(key, 1, timeout=window)
        return False
    return count > limit

"""A fixed-window counter in the cache: the one way a route says "too many, wait".

Each window has its own key (`<key>:<window number>`), so a window always ends: a counter can't
outlive it even if the cache loses its expiry. That happened: Django's Redis `incr` checks the key
exists and then INCRs it, and a key that expires between the two comes back from INCR with no
expiry at all, so it counted forever and refused that person for good (a page making fast calls hit
it). The expiry is also put back on such a key, so it doesn't stay in the cache."""
from __future__ import annotations

import time

from django.core.cache import cache


def slot(key: str, window: int) -> str:
    """The cache key counting `key` in the current window."""
    return f"{key}:{int(time.time() // window)}"


def over(key: str, limit: int, window: int) -> bool:
    """Count this request against `key`. True when it is over `limit` per `window` seconds."""
    slot_key = slot(key, window)
    if cache.add(slot_key, 1, timeout=window * 2):
        return False
    try:
        count = cache.incr(slot_key)
    except ValueError:
        # Expired between add() and incr(); this request starts a fresh window.
        cache.add(slot_key, 1, timeout=window * 2)
        return False
    if count == 1:
        # INCR made the key (it expired after the existence check): give it its expiry back.
        cache.touch(slot_key, window * 2)
    return count > limit

"""Per-user write rate limiting for mutating MCP tools.

Mutating tools (e.g. delete_project) can do real damage, so we cap how
often a single user may call them. The limit is enforced against the
Django cache: a sliding fixed-window counter keyed by user + bucket.

Default: 10 writes per 60s per user. Tune via settings:
    MCP_WRITE_LIMIT (int), MCP_WRITE_WINDOW_SECONDS (int).
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache


class RateLimitError(Exception):
    """Raised when a user exceeds the per-window write budget."""


def _limit() -> int:
    return int(getattr(settings, "MCP_WRITE_LIMIT", 10))


def _window() -> int:
    return int(getattr(settings, "MCP_WRITE_WINDOW_SECONDS", 60))


def check_write_limit(user_id: str | int) -> None:
    """Increment + check the write counter for `user_id`.

    Raises RateLimitError if the user has exceeded the budget in the
    current window. Fixed-window: the key carries no timestamp; it just
    expires after `window` seconds, so a fresh window starts cleanly.
    """
    window = _window()
    key = f"mcp:write:{user_id}"
    # add() only sets if absent, returning True; that's how we know we're
    # the first writer in this window and must set the TTL.
    if cache.add(key, 1, timeout=window):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            # Key expired between add() and incr(); treat as fresh.
            cache.add(key, 1, timeout=window)
            count = 1
    if count > _limit():
        raise RateLimitError(
            f"MCP write rate limit exceeded ({_limit()} per {window}s). Try again shortly."
        )

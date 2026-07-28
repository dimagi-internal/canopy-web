"""One place to build the shared async Redis client for presence state.

Uses redis.asyncio directly (the same library channels-redis already depends
on) so we do not add a second Redis client dependency.

Module-level cache: the client owns its own connection pool. Re-creating it
per call would leak sockets. One cached instance per process is right for
ASGI workers.

Returns None when no REDIS_URL is configured. canopy-web runs on an
in-memory channel layer in that mode, and presence is an enhancement that
must degrade to "not available" rather than crash — every caller treats a
None client as "no presence".
"""
from __future__ import annotations

import redis.asyncio
from django.conf import settings

_client: redis.asyncio.Redis | None = None


async def get_redis() -> redis.asyncio.Redis | None:
    global _client
    url = getattr(settings, "PRESENCE_REDIS_URL", "") or ""
    if not url:
        return None
    if _client is None:
        _client = redis.asyncio.from_url(url, decode_responses=True)
    return _client


async def close_redis() -> None:
    """Testing hook — close and reset the cached client."""
    global _client
    if _client is not None:
        await _client.close()
        _client = None

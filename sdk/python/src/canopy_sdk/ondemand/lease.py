"""Cross-process lease over a dedicated capability, backed by Redis.

``SET NX EX`` to acquire; release and refresh are compare-and-act so a holder
whose TTL lapsed cannot delete or extend a newer holder's lease. Each prefers an
atomic Lua script and falls back to WATCH/MULTI for backends without EVAL.
"""
from __future__ import annotations

import time
from typing import Any

from redis.exceptions import ResponseError, WatchError

_RELEASE_LUA = (
    "if redis.call('get',KEYS[1])==ARGV[1] then "
    "return redis.call('del',KEYS[1]) else return 0 end"
)
_REFRESH_LUA = (
    "if redis.call('get',KEYS[1])==ARGV[1] then "
    "return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end"
)


class Lease:
    """Expects a Redis client created with ``decode_responses=True``."""

    def __init__(self, redis: Any, capability: str, ttl_s: int = 1800, *, key: str | None = None):
        self._r = redis
        self._ttl_s = ttl_s
        self._key = key or f"ondemand:{capability}:lock"

    def acquire(self, owner: str) -> bool:
        return bool(self._r.set(self._key, owner, nx=True, ex=self._ttl_s))

    def acquire_wait(self, owner: str, timeout_s: float, poll_s: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout_s
        while True:
            if self.acquire(owner):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))

    def release(self, owner: str) -> bool:
        return self._if_owner(owner, _RELEASE_LUA, (), lambda pipe: pipe.delete(self._key))

    def refresh(self, owner: str) -> bool:
        return self._if_owner(
            owner, _REFRESH_LUA, (self._ttl_s,), lambda pipe: pipe.expire(self._key, self._ttl_s)
        )

    def holder(self) -> str:
        return self._r.get(self._key) or ""

    def _if_owner(self, owner: str, lua: str, args: tuple, act) -> bool:
        try:
            return bool(self._r.eval(lua, 1, self._key, owner, *args))
        except (NotImplementedError, AttributeError):
            pass
        except ResponseError as e:
            if "unknown command" not in str(e).lower() and "eval" not in str(e).lower():
                raise
        # EVAL unsupported: WATCH/MULTI aborts the EXEC if the key changed after we read it.
        # Connection errors and timeouts propagate rather than reading as "not the owner".
        with self._r.pipeline() as pipe:
            try:
                pipe.watch(self._key)
                if pipe.get(self._key) != owner:
                    pipe.unwatch()
                    return False
                pipe.multi()
                act(pipe)
                result = pipe.execute()
            except WatchError:
                return False
            return bool(result and result[0])

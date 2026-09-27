"""Where a host keeps state: used ``jti``s, issued tokens, and cached documents.

Three small interfaces, each with an in-memory implementation for tests and
single-process dev. ``canopy_sdk.django`` provides database-backed ones.

**Single use must fail CLOSED.** A ``JtiStore`` that cannot record a ``jti``
must refuse the request, never let it through: a cache that silently drops
writes would quietly allow replays. That is why the Django store uses the
database (a unique constraint) and not the cache.
"""
from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .contract import ContractError


class Replayed(ContractError):
    """A ``jti`` that was already used."""

    def __init__(self, message: str = "this token has already been used"):
        super().__init__("replayed", message)


def jti_key(kind: str, jti: str) -> str:
    """How a used ``jti`` is keyed: its kind plus a hash, so a hostile value's
    length never reaches a column and two kinds never collide."""
    return f"{kind}:{hashlib.sha256(jti.encode()).hexdigest()}"


@runtime_checkable
class JtiStore(Protocol):
    """Records used ``jti``s. Also the DPoP REPLAY CACHE (kind ``dpop:<jkt>``)."""

    def consume(self, entries: list[tuple[str, str, int]]) -> None:
        """Record every ``(kind, jti, exp_epoch)`` as used — ALL or NONE.

        Raises ``Replayed`` when any one was already used. Each entry is kept
        until at least ``exp`` (after which the statement is refused on ``exp``
        anyway), so the store stays bounded.
        """

    def prune(self) -> None:
        """Drop entries past their expiry. Best-effort housekeeping."""


@dataclass
class IssuedToken:
    """An access token a host issued through the jwt-bearer grant.

    Persisted by the host — WITHOUT the raw token, which is returned to the
    caller once and never stored (``token_checksum`` is its SHA-256).
    """

    token_checksum: str
    subject: str
    client_id: str
    #: RFC 8693 ``act.sub``: who acts for the subject (canopy's client_id).
    actor: str
    scopes: tuple[str, ...]
    #: RFC 7638 thumbprint of the DPoP key the token is bound to (``cnf.jkt``).
    cnf_jkt: str
    grant_jti: str
    expires_at: float
    created_at: float = field(default_factory=time.time)

    @property
    def scope(self) -> str:
        return " ".join(self.scopes)

    def expired(self, now: float | None = None) -> bool:
        return self.expires_at <= (time.time() if now is None else now)


@runtime_checkable
class TokenStore(Protocol):
    def save(self, token: IssuedToken) -> None: ...

    def get(self, token_checksum: str) -> IssuedToken | None: ...

    def prune(self) -> None: ...


@runtime_checkable
class DocumentCache(Protocol):
    """A tiny cache (Django's cache satisfies it) for fetched metadata."""

    def get(self, key: str): ...

    def set(self, key: str, value, timeout: int) -> None: ...

    def add(self, key: str, value, timeout: int) -> bool: ...


# --- in-memory implementations ---------------------------------------------------


class MemoryJtiStore:
    """Process-local. Fine for tests and a single dev process; a multi-worker
    deployment needs a shared store (``canopy_sdk.django.stores.DjangoJtiStore``)."""

    #: Kept past ``exp`` so a statement claiming to expire in the past is still
    #: remembered long enough to cover the leeway.
    FLOOR_SECONDS = 300

    def __init__(self):
        self._seen: dict[str, float] = {}
        self._lock = threading.Lock()

    def consume(self, entries):
        now = time.time()
        keys = [(jti_key(kind, jti), max(float(exp), now) + self.FLOOR_SECONDS)
                for kind, jti, exp in entries]
        with self._lock:
            if len({k for k, _ in keys}) != len(keys) or any(
                    k in self._seen and self._seen[k] > now for k, _ in keys):
                raise Replayed()
            for k, until in keys:
                self._seen[k] = until

    def prune(self):
        now = time.time()
        with self._lock:
            for k in [k for k, until in self._seen.items() if until <= now]:
                del self._seen[k]


class MemoryTokenStore:
    def __init__(self):
        self._tokens: dict[str, IssuedToken] = {}
        self._lock = threading.Lock()

    def save(self, token):
        with self._lock:
            self._tokens[token.token_checksum] = token

    def get(self, token_checksum):
        with self._lock:
            return self._tokens.get(token_checksum)

    def prune(self):
        now = time.time()
        with self._lock:
            for k in [k for k, t in self._tokens.items() if t.expired(now)]:
                del self._tokens[k]

    def __len__(self):
        return len(self._tokens)


class MemoryCache:
    """A ``DocumentCache`` with per-key expiry."""

    def __init__(self):
        self._data: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            hit = self._data.get(key)
            if hit is None or hit[0] <= time.time():
                return None
            return hit[1]

    def set(self, key, value, timeout):
        with self._lock:
            self._data[key] = (time.time() + timeout, value)

    def add(self, key, value, timeout):
        with self._lock:
            hit = self._data.get(key)
            if hit is not None and hit[0] > time.time():
                return False
            self._data[key] = (time.time() + timeout, value)
            return True

    def clear(self):
        with self._lock:
            self._data.clear()

"""Rate limiting for the two endpoints that hand out a delegated identity.

`POST /api/auth/token-exchange` is `auth=None` (it authenticates via the
bearer app credential itself, not Django session/PAT auth) and, since
tenant-scoped provisioning shipped, can mint a PERSISTENT side effect — a
`WorkspaceMembership` row — not just a short-lived token. Before that change
a leaked credential's worst case was revocable short-lived tokens; now it's
membership rows that outlive revoking the credential. There was no
throttling anywhere on this endpoint, so a leaked credential could be
hammered without bound. Each app credential gets its own sliding
fixed-window budget — same pattern as `apps.mcp.rate_limit`'s per-user
write limit.

Default: 30 exchanges per 60s per credential. Tune via settings:
    TOKEN_EXCHANGE_LIMIT (int), TOKEN_EXCHANGE_WINDOW_SECONDS (int).

See docs/archive/plans/2026-07-26-tenant-scoped-provisioning.md (F2).

`POST /api/embed/token` mints the same kind of token for canopy's OWN signed-in
user and had no budget at all. It is cheaper to abuse than exchange in one
specific way: it needs only a stolen session cookie rather than an app secret,
and every call writes a `DelegatedToken` row. Keyed per USER rather than per
app, because the user is who is being minted for and an app is not being
authenticated here at all.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache


class ExchangeRateLimitError(Exception):
    """Raised when a credential exceeds its per-window exchange budget."""


def _limit() -> int:
    return int(getattr(settings, "TOKEN_EXCHANGE_LIMIT", 30))


def _window() -> int:
    return int(getattr(settings, "TOKEN_EXCHANGE_WINDOW_SECONDS", 60))


def check_exchange_limit(app_id) -> None:
    """Increment + check the exchange counter for `app_id`.

    Raises ExchangeRateLimitError if the credential has exceeded its budget
    in the current window. Fixed-window: the key carries no timestamp; it
    just expires after `window` seconds, so a fresh window starts cleanly.
    """
    window = _window()
    key = f"tokens:exchange:{app_id}"
    # add() only sets if absent, returning True; that's how we know we're
    # the first caller in this window and must set the TTL.
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
        raise ExchangeRateLimitError(
            f"token-exchange rate limit exceeded ({_limit()} per {window}s). Try again shortly."
        )


def _mint_limit() -> int:
    return int(getattr(settings, "EMBED_MINT_LIMIT", 30))


def _mint_window() -> int:
    return int(getattr(settings, "EMBED_MINT_WINDOW_SECONDS", 60))


class MintRateLimitError(Exception):
    """Raised when one user asks for delegated tokens too often."""


def check_mint_limit(user_id) -> None:
    """Increment + check the session-mint counter for `user_id`.

    Generous relative to real use: the client caches its token and refetches
    only near expiry (`REFRESH_SKEW_MS`), so a browser with several panels open
    still mints a handful an hour. A caller hitting 30 in a minute is looping.
    """
    window = _mint_window()
    key = f"tokens:mint:{user_id}"
    if cache.add(key, 1, timeout=window):
        count = 1
    else:
        try:
            count = cache.incr(key)
        except ValueError:
            cache.add(key, 1, timeout=window)
            count = 1
    if count > _mint_limit():
        raise MintRateLimitError(
            f"token mint rate limit exceeded ({_mint_limit()} per {window}s). "
            "Try again shortly."
        )

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


# --- the contact-token endpoint ----------------------------------------------
#
# The only one of the three that is UNAUTHENTICATED, and therefore the one that
# most needed a budget and least had one. Two limits, in the order the work
# happens, because a limit checked after the expensive part has not saved
# anything:
#
#   1. per CLIENT, before the assertion is parsed at all.
#   2. per ISSUER, once the app is known and before its signature is verified.
#
# The per-client limit is a floor against anonymous flooding, not a business
# limit: a host's BACKEND calls this endpoint, so all of one site's legitimate
# traffic arrives from a handful of addresses and a tight cap would throttle a
# busy partner rather than an attacker. The per-issuer limit is the real bound —
# it caps both signature verifications and the `Contact` rows a compromised key
# can create.


class ContactTokenRateLimitError(Exception):
    """Raised when contact-token minting exceeds a budget."""


def check_contact_token_client(client_ip) -> None:
    """Cheap guard, before anything is parsed.

    Deliberately generous. Reaching a signature check at all requires naming a
    registered app — an unknown `iss` is refused after a base64 decode — so the
    expensive path is already narrow; this only stops someone hammering it.
    """
    limit = int(getattr(settings, "CONTACT_TOKEN_CLIENT_LIMIT", 300))
    window = int(getattr(settings, "CONTACT_TOKEN_WINDOW_SECONDS", 60))
    if _over(f"tokens:contact:ip:{client_ip or 'unknown'}", limit, window):
        raise ContactTokenRateLimitError(
            f"too many contact-token requests ({limit} per {window}s)"
        )


def check_contact_token_issuer(name: str) -> None:
    """The real bound: how many visitors one site may vouch for per window.

    Caps the rows a leaked signing key can create, and the verifications a
    named app can make canopy perform.
    """
    limit = int(getattr(settings, "CONTACT_TOKEN_ISSUER_LIMIT", 120))
    window = int(getattr(settings, "CONTACT_TOKEN_WINDOW_SECONDS", 60))
    if _over(f"tokens:contact:iss:{name}", limit, window):
        raise ContactTokenRateLimitError(
            f"too many contact tokens for this site ({limit} per {window}s)"
        )


def _over(key: str, limit: int, window: int) -> bool:
    """Fixed-window counter. True when this request is over budget."""
    if cache.add(key, 1, timeout=window):
        return False
    try:
        count = cache.incr(key)
    except ValueError:
        # Expired between add() and incr(); this request starts a fresh window.
        cache.add(key, 1, timeout=window)
        return False
    return count > limit

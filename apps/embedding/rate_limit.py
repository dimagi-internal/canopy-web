"""Rate limiting for the endpoints that hand out a delegated identity.

`POST /api/embed/token` mints a `DelegatedToken` for canopy's OWN signed-in
user, and had no budget at all. It needs only a stolen session cookie rather
than an app secret, and every call writes a row. Keyed per USER rather than per
app, because the user is who is being minted for and no app is being
authenticated here.

`POST /api/auth/contact-token` has budgets of its own, below: two of them, per
client and per issuer, because it is unauthenticated — the signed assertion IS
the credential, so there is nobody to bill until one has been checked.

The per-credential exchange budget that used to live here went with
`/api/auth/token-exchange` (2026-09-22): a shared secret that minted a token
for any address in an app's allowed domains, and created the canopy user as a
side effect. Nothing needs a budget for it because nothing can call it.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache


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

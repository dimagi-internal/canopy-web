"""Rate limiting for the endpoints that hand out a delegated identity.

`POST /api/embed/token` mints a `DelegatedToken` for canopy's OWN signed-in
user, and had no budget at all. It needs only a stolen session cookie rather
than an app secret, and every call writes a row. Keyed per USER rather than per
app, because the user is who is being minted for and no app is being
authenticated here.

`POST /api/auth/contact-token` has budgets of its own, below: two of them, per
client and per issuer, because it is unauthenticated — the signed assertion IS
the credential, so there is nobody to bill until one has been checked.

A contact's sends (`/api/contact/sessions/{id}/send`) have a budget per contact.

There is no per-credential exchange budget: no endpoint mints a token from a
shared secret for an address in an app's allowed domains.
"""
from __future__ import annotations

from django.conf import settings
from django.core.cache import cache

from apps.common.rate_limit import over


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
    if over(f"tokens:contact:ip:{client_ip or 'unknown'}", limit, window):
        raise ContactTokenRateLimitError(
            f"too many contact-token requests ({limit} per {window}s)"
        )


def check_contact_token_issuer(name: str, limit: int | None = None) -> None:
    """The real bound: how many visitors one site may vouch for per window.

    Caps the rows a leaked signing key can create, and the verifications a
    named app can make canopy perform. `limit` is the site's own budget
    (`AppCredential.contact_token_issuer_limit`, which only a canopy superuser
    sets); None = the deployment's `CONTACT_TOKEN_ISSUER_LIMIT`.
    """
    if limit is None:
        limit = int(getattr(settings, "CONTACT_TOKEN_ISSUER_LIMIT", 120))
    window = int(getattr(settings, "CONTACT_TOKEN_WINDOW_SECONDS", 60))
    if over(f"tokens:contact:iss:{name}", limit, window):
        raise ContactTokenRateLimitError(
            f"too many contact tokens for this site ({limit} per {window}s)"
        )


# --- a contact's sends -------------------------------------------------------
#
# Every message a contact sends starts a turn, and a turn costs a model session on a
# runner. A contact has no account and no budget of their own, and a relay site sends
# for its people from a server, so nothing but this stops one person (or a host
# looping on one person) from spending the agent's runners. Per CONTACT, not per
# site: a busy site's other people are not held up by one of them.


class ContactSendRateLimitError(Exception):
    """Raised when one contact sends more than their budget."""


def check_contact_send(contact_id) -> None:
    """Count one send by `contact_id` against both budgets: per minute
    (`CONTACT_SEND_PER_MINUTE`) and per day (`CONTACT_SEND_PER_DAY`)."""
    per_minute = int(getattr(settings, "CONTACT_SEND_PER_MINUTE", 6))
    per_day = int(getattr(settings, "CONTACT_SEND_PER_DAY", 200))
    if over(f"contacts:send:min:{contact_id}", per_minute, 60):
        raise ContactSendRateLimitError(
            f"too many messages ({per_minute} a minute); wait a minute and send again")
    if over(f"contacts:send:day:{contact_id}", per_day, 24 * 3600):
        raise ContactSendRateLimitError(
            f"too many messages today ({per_day} a day); try again tomorrow")


# --- the site agents API ----------------------------------------------------
#
# `GET /api/sites/agents` is unauthenticated in the same way: the signed request
# IS the credential. A site asks every few minutes, so both budgets are far above
# real use; they bound the signature checks (and response builds) a flood can cause.


class SiteAgentsRateLimitError(Exception):
    """Raised when site agents API requests exceed a budget."""


def check_site_agents_client(client_ip) -> None:
    limit = int(getattr(settings, "SITE_AGENTS_CLIENT_LIMIT", 120))
    window = int(getattr(settings, "SITE_AGENTS_WINDOW_SECONDS", 60))
    if over(f"tokens:site-agents:ip:{client_ip or 'unknown'}", limit, window):
        raise SiteAgentsRateLimitError(f"too many site agents requests ({limit} per {window}s)")


def check_site_agents_issuer(name: str) -> None:
    limit = int(getattr(settings, "SITE_AGENTS_ISSUER_LIMIT", 60))
    window = int(getattr(settings, "SITE_AGENTS_WINDOW_SECONDS", 60))
    if over(f"tokens:site-agents:iss:{name}", limit, window):
        raise SiteAgentsRateLimitError(f"too many site agents requests for this site ({limit} per {window}s)")


def check_resources_updated(client_ip, issuer: str = "") -> None:
    """A host site telling canopy its resources changed
    (`POST /api/sites/resources-updated`): per client before anything is parsed,
    per site (`issuer`) before its signature is checked. Generous — a host may
    notify on every edit — but bounded."""
    limit = int(getattr(settings, "RESOURCES_UPDATED_LIMIT", 600))
    window = int(getattr(settings, "RESOURCES_UPDATED_WINDOW_SECONDS", 60))
    key = f"tokens:resources-updated:iss:{issuer}" if issuer else \
        f"tokens:resources-updated:ip:{client_ip or 'unknown'}"
    if over(key, limit, window):
        raise SiteAgentsRateLimitError(f"too many resource notifications ({limit} per {window}s)")

"""The parts of an OAuth 2.0 authorization server that do not depend on who it serves.

canopy runs two authorization servers: a person's MCP client signing in
(`apps/tokens/mcp_oauth.py`) and the HCP service for third-party apps
(`apps/contacts/hcp_oauth.py`). Their grants, consent and tokens differ; the
protocol rules below do not, so each server asks this module rather than
writing its own:

* `OAuthError` and `error_response` — an RFC 6749 §5.2 error and how it goes on
  the wire (JSON, `no-store`).
* `check_challenge` / `verify_pkce` — PKCE S256 (RFC 7636): a challenge is a
  43-character base64url SHA-256; a verifier is 43–128 unreserved characters
  (§4.1) and must hash to the challenge (§4.6).
* `check_redirect_uri` — RFC 6749 §4.1.3: when the authorization request named a
  `redirect_uri`, the token request must name the identical one.
* `redeem_once` — a one-time credential (an authorization code, a refresh
  token): looked up by its hash, bound to its client, redeemed under a row lock,
  and when it is presented AGAIN — even by a concurrent request — what it
  bought is revoked (RFC 6749 §4.1.2, OAuth 2.0 Security BCP §4.14 refresh-token
  reuse detection). The revocation runs outside the refused exchange's
  transaction, so the refusal cannot roll it back.

Each server supplies its models through callables; nothing here names one.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import re
from typing import Any, Callable

from django.db import transaction
from django.http import JsonResponse
from django.utils import timezone

#: RFC 7636 §4.1: code_verifier = 43*128unreserved.
PKCE_VERIFIER = re.compile(r"^[A-Za-z0-9\-._~]{43,128}$")
#: RFC 7636 §4.2: an S256 challenge is BASE64URL(SHA256(verifier)) without padding.
PKCE_S256_CHALLENGE = re.compile(r"^[A-Za-z0-9\-_]{43}$")


class OAuthError(Exception):
    """An RFC 6749 error: `code` is the wire value, `status` the HTTP status."""

    def __init__(self, code: str, description: str, status: int = 400):
        super().__init__(description)
        self.code = code
        self.description = description
        self.status = status

    def body(self) -> dict:
        return {"error": self.code, "error_description": self.description}


def no_store(response: JsonResponse) -> JsonResponse:
    """RFC 6749 §5.1: a response carrying (or refusing) tokens is never cached."""
    response["Cache-Control"] = "no-store"
    response["Pragma"] = "no-cache"
    return response


def error_response(err: OAuthError) -> JsonResponse:
    """RFC 6749 §5.2 on the wire."""
    return no_store(JsonResponse(err.body(), status=err.status))


# --- PKCE (RFC 7636) ------------------------------------------------------------------


def s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def check_challenge(challenge: str, method: str) -> str | None:
    """Why an authorization request's PKCE parameters are refused, or None."""
    if not challenge:
        return "PKCE is required: send code_challenge"
    if method != "S256":
        return "PKCE code_challenge_method must be S256"
    if not PKCE_S256_CHALLENGE.match(challenge):
        return "code_challenge is a base64url SHA-256 (43 characters)"
    return None


def verify_pkce(verifier: str, challenge: str) -> None:
    """Raise unless `verifier` is well-formed and hashes to `challenge`."""
    if not verifier:
        raise OAuthError("invalid_request", "PKCE code_verifier is required")
    if not PKCE_VERIFIER.match(verifier) or not hmac.compare_digest(s256(verifier), challenge or ""):
        raise OAuthError("invalid_grant", "PKCE code_verifier does not match")


# --- the token request ----------------------------------------------------------------


def check_redirect_uri(presented: str, recorded: str) -> None:
    """RFC 6749 §4.1.3. `recorded` is the redirect_uri the AUTHORIZATION request
    named ("" when it named none). When it named one, the token request must name
    the same one, exactly — omitting it is refused. When it named none, the
    parameter is not required (§4.1.3) and is not checked."""
    if recorded and (presented or "") != recorded:
        raise OAuthError("invalid_grant", "redirect_uri does not match the authorization request")


class _Replayed(Exception):
    def __init__(self, row):
        super().__init__("replayed")
        self.row = row


def redeem_once(
    raw: str,
    *,
    client_pk: Any,
    find: Callable[[str], Any],
    is_spent: Callable[[Any], bool],
    expires_at: Callable[[Any], Any],
    use: Callable[[Any], dict],
    revoke: Callable[[Any], None],
    what: str,
) -> dict:
    """Redeem a one-time credential and return what `use` issues for it.

    `find(raw)` returns its row (unlocked) or None; the row carries `client_id`.
    It is re-read under `select_for_update`, so of two concurrent redemptions
    exactly one wins and the other is a replay. `is_spent(row)` says whether it
    was already redeemed, `expires_at(row)` when it lapses (None: never). `use`
    runs inside the lock, does the server's own checks, marks the row spent and
    issues. A replay calls `revoke(row)` — after the transaction, so it sticks.
    """
    row = find(raw) if raw else None
    if row is None or row.client_id != client_pk:
        raise OAuthError("invalid_grant", f"unknown {what}")
    try:
        with transaction.atomic():
            locked = type(row).objects.select_for_update().filter(pk=row.pk).first()
            if locked is None:
                raise OAuthError("invalid_grant", f"unknown {what}")
            if is_spent(locked):
                raise _Replayed(locked)
            lapses = expires_at(locked)
            if lapses is not None and lapses <= timezone.now():
                raise OAuthError("invalid_grant", f"{what} expired")
            return use(locked)
    except _Replayed as replay:
        revoke(replay.row)
        raise OAuthError("invalid_grant", f"{what} already used; the grant is revoked") from None

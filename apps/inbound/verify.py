"""Verify that a push really came from our Pub/Sub subscription.

Google signs an OIDC JWT into the ``Authorization: Bearer`` header of every push
it delivers, when the subscription is configured with a push auth service
account. ``google-auth`` is already a dependency (Drive uses it), so this costs
no new package.

Fail CLOSED: unverifiable is refused. But note the shape of what is being
protected — the doorbell carries no content, so a forged ping can at worst cause
a `gog` read that finds nothing. Verification is here to stop an unauthenticated
stranger spending our Gmail quota, not to protect message integrity, because
there is no message.
"""
from __future__ import annotations

import logging

from django.conf import settings

logger = logging.getLogger(__name__)


class VerificationError(Exception):
    pass


def _bearer(request) -> str:
    raw = request.headers.get("Authorization", "")
    scheme, _, token = raw.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def verify_push(request) -> dict:
    """Return the token's verified claims, or raise ``VerificationError``.

    When ``INBOUND_PUSH_AUDIENCE`` is unset the endpoint refuses everything. That
    is deliberate: an unconfigured deployment should not quietly accept anonymous
    pushes, and the 300s poll means refusing costs latency rather than mail.
    """
    audience = getattr(settings, "INBOUND_PUSH_AUDIENCE", "") or ""
    if not audience:
        raise VerificationError("INBOUND_PUSH_AUDIENCE is unset")

    token = _bearer(request)
    if not token:
        raise VerificationError("no bearer token on the push request")

    try:
        from google.auth.transport import requests as google_requests
        from google.oauth2 import id_token
    except ImportError as exc:  # pragma: no cover - google-auth is a hard dep
        raise VerificationError(f"google-auth unavailable: {exc}") from exc

    try:
        claims = id_token.verify_oauth2_token(
            token, google_requests.Request(), audience
        )
    except Exception as exc:  # noqa: BLE001 — any verification failure is a refusal
        raise VerificationError(str(exc)) from exc

    expected_sa = getattr(settings, "INBOUND_PUSH_SERVICE_ACCOUNT", "") or ""
    if expected_sa and claims.get("email", "").lower() != expected_sa.lower():
        # Audience alone is not identity: anyone who learns the audience string
        # could mint a token for it from a different service account.
        raise VerificationError(
            f"push signed by {claims.get('email')!r}, expected {expected_sa!r}"
        )
    if expected_sa and not claims.get("email_verified", False):
        raise VerificationError("push token's email is not verified")

    return claims

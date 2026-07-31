"""Verify that a push really came from a workspace's own Pub/Sub subscription.

Google signs an OIDC JWT into the ``Authorization: Bearer`` header of every push
it delivers, when the subscription is configured with a push auth service
account. ``google-auth`` is already a dependency (Drive uses it), so this costs
no new package.

**Verification is per WORKSPACE, not per deployment.** The expected audience and
signer live on that workspace's ``InboundPushConfig``, and the workspace is named
in the URL — so a second tenant, in its own GCP project with its own service
account, verifies correctly, and one tenant's service account can never satisfy
another's check.

Fail CLOSED: unverifiable is refused, and a workspace with no audience configured
refuses everything. But note the shape of what is being protected — the doorbell
carries no content, so a forged ping can at worst cause a `gog` read that finds
nothing. Verification is here to stop a stranger spending our Gmail quota, not to
protect message integrity, because there is no message.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class VerificationError(Exception):
    pass


def _bearer(request) -> str:
    raw = request.headers.get("Authorization", "")
    scheme, _, token = raw.partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def verify_push(request, config) -> dict:
    """Return the token's verified claims, or raise ``VerificationError``.

    ``config`` is the target workspace's ``InboundPushConfig``. A missing config,
    or one with no audience, refuses — an unconfigured tenant must not quietly
    accept anonymous pushes.
    """
    if config is None or not config.audience:
        raise VerificationError("workspace has no inbound push audience configured")

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
            token, google_requests.Request(), config.audience
        )
    except Exception as exc:  # noqa: BLE001 — any verification failure is a refusal
        raise VerificationError(str(exc)) from exc

    expected_sa = (config.service_account or "").strip()
    if expected_sa and claims.get("email", "").lower() != expected_sa.lower():
        raise VerificationError(
            f"push signed by {claims.get('email')!r}, expected {expected_sa!r}"
        )
    if expected_sa and not claims.get("email_verified", False):
        raise VerificationError("push token's email is not verified")

    return claims

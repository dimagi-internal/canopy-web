"""On-behalf-of token exchange: a registered AppCredential (e.g. ace-web) asserts
the human it has already authenticated and receives a short-lived DelegatedToken
for that user. The reusable security model for products embedding canopy chat —
see docs/superpowers/specs/2026-07-25-ace-web-canopy-chat-cutover-design.md."""
from __future__ import annotations

import logging
from datetime import datetime

from allauth.account.models import EmailAddress
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.common.auth_domains import allowed_email_domains
from apps.workspaces import services as wsvc

from .models import AppCredential, DelegatedToken
from .rate_limit import ExchangeRateLimitError, check_exchange_limit

logger = logging.getLogger(__name__)

exchange_router = Router(tags=["auth"])

TTL_MIN, TTL_MAX, TTL_DEFAULT = 60, 86400, 3600


class TokenExchangeIn(Schema):
    acting_as_email: str
    ttl_seconds: int = TTL_DEFAULT


class TokenExchangeOut(Schema):
    token: str
    expires_at: datetime
    workspace: str | None = None


def _allowed_login_domains() -> set[str]:
    """Domains OAuth login currently accepts, reused so token-exchange can't
    silently drift from the org's central domain policy (`apps.common.auth_domains`).

    Deliberate divergence: OAuth's `email_in_allowlist` treats an empty
    `AUTH_ALLOWED_EMAIL_DOMAIN` as "allow any domain" (a permissive default for
    an unconfigured dev box). Minting a *working delegated credential* is a
    stronger action than letting someone browse in — an unconfigured allowlist
    must never grant it, so this stays fail-closed on empty.
    """
    return set(allowed_email_domains())


@exchange_router.post("/token-exchange", auth=None, response=TokenExchangeOut,
                      summary="Exchange an app credential for a delegated user token")
def token_exchange(request, payload: TokenExchangeIn):
    header = request.META.get("HTTP_AUTHORIZATION", "")
    raw = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    app = AppCredential.lookup(raw)
    if app is None:
        logger.warning(
            "token-exchange rejected: invalid app credential (acting_as=%s, ip=%s)",
            payload.acting_as_email, request.META.get("REMOTE_ADDR"),
        )
        raise HttpError(401, "invalid app credential")

    # F2 (2026-07-26 security review): this endpoint is `auth=None` and, since
    # provisioning shipped, can mint a persistent membership row rather than
    # just a revocable short-lived token — a leaked credential must not be
    # able to hammer it unboundedly. Per-credential, so it can't be used to
    # deny another app's traffic.
    try:
        check_exchange_limit(app.pk)
    except ExchangeRateLimitError:
        logger.warning(
            "token-exchange rejected: rate limit exceeded (app=%s, ip=%s)",
            app.name, request.META.get("REMOTE_ADDR"),
        )
        raise HttpError(429, "token-exchange rate limit exceeded")

    email = payload.acting_as_email.strip().lower()
    domain = email.rpartition("@")[2]
    app_domains = {d.lower() for d in app.allowed_delegation_domains}
    if "@" not in email or domain not in app_domains or domain not in _allowed_login_domains():
        logger.warning(
            "token-exchange rejected: domain not allowed (app=%s, domain=%s, ip=%s)",
            app.name, domain, request.META.get("REMOTE_ADDR"),
        )
        raise HttpError(403, "delegation not allowed for this domain")

    ttl = max(TTL_MIN, min(int(payload.ttl_seconds or TTL_DEFAULT), TTL_MAX))
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is not None and not user.is_active:
        # A deactivated (offboarded) account must never get fresh working
        # tokens — session auth already rejects inactive users, so exchange
        # must too. Uniform wording; no existence/activity detail leaked.
        logger.warning(
            "token-exchange rejected: inactive account (app=%s, email=%s, ip=%s)",
            app.name, email, request.META.get("REMOTE_ADDR"),
        )
        raise HttpError(403, "delegation not allowed for this account")
    # F6 (2026-07-26 security review): JIT user creation, provisioning, and
    # auto-join are one all-or-nothing write — a failure partway through
    # (e.g. the auto-join loop) must not leave a JIT user or a provisioned
    # membership committed with nothing else to show for it.
    workspace_slug = None
    with transaction.atomic():
        if user is None:
            user = User.objects.create_user(username=email, email=email)
            # Also create a verified allauth EmailAddress so a later real Google
            # login for this same human connects to this JIT user instead of
            # forking/blocking on allauth's duplicate-email path — see
            # apps.common.auth_adapter.CustomSocialAccountAdapter.pre_social_login.
            EmailAddress.objects.create(user=user, email=email, verified=True, primary=True)

        # Tenant-scoped provisioning: the workspace comes ONLY from this
        # credential's server-side row, never from the request — a rejected
        # exchange (invalid credential / disallowed domain / inactive user)
        # never reaches this line, so it can never leave a membership behind.
        # create-only (`ensure_member` is get_or_create): an existing
        # member's role is never raised or lowered by an app.
        #
        # Runs BEFORE auto_join_workspaces (F3): if `provision_workspace`
        # also happens to be an auto-join workspace for this domain, the
        # explicit `provision_role` grant must win — creating the row here
        # first makes the later auto-join call a no-op against it, rather
        # than auto-join silently creating it at `editor` first and making
        # a `viewer` grant a no-op instead. See
        # AppCredential's docstring and docs/archive/plans/
        # 2026-07-26-tenant-scoped-provisioning.md.
        if app.provision_workspace_id:
            _membership, created = wsvc.ensure_member(
                app.provision_workspace, user, app.provision_role, provisioned_by_app=app,
            )
            workspace_slug = app.provision_workspace_id
            if created:
                # F1: the one action here that GRANTS privilege must be
                # detectable — the three rejection paths above already log;
                # this was the missing fourth log line.
                logger.info(
                    "token-exchange provisioned membership (app=%s, email=%s, "
                    "workspace=%s, role=%s)",
                    app.name, email, workspace_slug, app.provision_role,
                )

        wsvc.auto_join_workspaces(user)

    AppCredential.objects.filter(pk=app.pk).update(last_used_at=timezone.now())
    raw_token, token = DelegatedToken.issue(app=app, user=user, ttl_seconds=ttl)
    return {"token": raw_token, "expires_at": token.expires_at, "workspace": workspace_slug}

"""On-behalf-of token exchange: a registered AppCredential (e.g. ace-web) asserts
the human it has already authenticated and receives a short-lived DelegatedToken
for that user. The reusable security model for products embedding canopy chat —
see docs/superpowers/specs/2026-07-25-ace-web-canopy-chat-cutover-design.md."""
from __future__ import annotations

from datetime import datetime

from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from ninja import Router, Schema
from ninja.errors import HttpError

from apps.workspaces import services as wsvc

from .models import AppCredential, DelegatedToken

exchange_router = Router(tags=["auth"])

TTL_MIN, TTL_MAX, TTL_DEFAULT = 60, 86400, 3600


class TokenExchangeIn(Schema):
    acting_as_email: str
    ttl_seconds: int = TTL_DEFAULT


class TokenExchangeOut(Schema):
    token: str
    expires_at: datetime


def _allowed_login_domains() -> set[str]:
    rawval = getattr(settings, "AUTH_ALLOWED_EMAIL_DOMAIN", "") or ""
    return {d.strip().lower() for d in rawval.split(",") if d.strip()}


@exchange_router.post("/token-exchange", auth=None, response=TokenExchangeOut,
                      summary="Exchange an app credential for a delegated user token")
def token_exchange(request, payload: TokenExchangeIn):
    header = request.META.get("HTTP_AUTHORIZATION", "")
    raw = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
    app = AppCredential.lookup(raw)
    if app is None:
        raise HttpError(401, "invalid app credential")

    email = payload.acting_as_email.strip().lower()
    domain = email.rpartition("@")[2]
    app_domains = {d.lower() for d in app.allowed_delegation_domains}
    if "@" not in email or domain not in app_domains or domain not in _allowed_login_domains():
        raise HttpError(403, "delegation not allowed for this domain")

    ttl = max(TTL_MIN, min(int(payload.ttl_seconds or TTL_DEFAULT), TTL_MAX))
    User = get_user_model()
    user = User.objects.filter(email__iexact=email).first()
    if user is None:
        user = User.objects.create_user(username=email, email=email)
    wsvc.auto_join_workspaces(user)
    AppCredential.objects.filter(pk=app.pk).update(last_used_at=timezone.now())
    raw_token, token = DelegatedToken.issue(app=app, user=user, ttl_seconds=ttl)
    return {"token": raw_token, "expires_at": token.expires_at}

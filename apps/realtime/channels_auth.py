"""WebSocket handshake auth: session cookie, then Bearer, then `?token=`.

Ports ace-web's AceSessionAuthMiddleware. Reads settings.SESSION_COOKIE_NAME
(sessionid_canopy on connectlabs, sessionid elsewhere) — never hardcoded. Bearer
resolution reuses apps/tokens (PersonalToken.lookup, then DelegatedToken.lookup)
so scripted clients — and SP4's ace-web — authenticate over WS exactly as they
do over REST.

Resolution order: session cookie → `Authorization: Bearer` (PersonalToken, then
DelegatedToken) → `?token=<raw>` query param. The query param accepts
DelegatedTokens ONLY — browsers can't set an Authorization header on
`new WebSocket()`, so short-lived delegated tokens are allowed to ride the
query string; long-lived PATs are deliberately rejected there by design (a PAT
in a URL would end up in access logs, browser history, and referrers).

The middleware always sets scope["user"] to a real User or AnonymousUser; the
per-surface authorization (can this user read this turn?) happens in the consumer.
"""
from __future__ import annotations

from http.cookies import SimpleCookie
from importlib import import_module

from channels.db import database_sync_to_async
from django.conf import settings
from django.contrib.auth import HASH_SESSION_KEY, SESSION_KEY, get_user_model
from django.contrib.auth.models import AnonymousUser
from django.utils.crypto import constant_time_compare


def _header(scope, name: bytes) -> bytes | None:
    for key, value in scope.get("headers", []):
        if key == name:
            return value
    return None


@database_sync_to_async
def _user_from_session(scope):
    raw = _header(scope, b"cookie")
    if not raw:
        return None
    jar = SimpleCookie()
    jar.load(raw.decode("latin1"))
    morsel = jar.get(settings.SESSION_COOKIE_NAME)
    if not morsel:
        return None
    engine = import_module(settings.SESSION_ENGINE)
    session = engine.SessionStore(morsel.value)
    uid = session.get(SESSION_KEY)
    if not uid:
        return None
    User = get_user_model()
    user = User.objects.filter(pk=uid, is_active=True).first()
    if user is None:
        return None
    # Verify the session auth hash, exactly as django.contrib.auth.get_user does,
    # so a session a password change SHOULD have invalidated cannot authenticate
    # over WS until it happens to expire.
    session_hash = session.get(HASH_SESSION_KEY)
    if not (session_hash and constant_time_compare(session_hash, user.get_session_auth_hash())):
        return None
    return user


@database_sync_to_async
def _user_from_bearer(scope):
    raw = _header(scope, b"authorization")
    if not raw or not raw.lower().startswith(b"bearer "):
        return None
    token_value = raw[7:].decode("latin1").strip()
    if not token_value:
        return None
    from apps.tokens.models import DelegatedToken, PersonalToken

    token = PersonalToken.lookup(token_value)
    if token is not None:
        return token.user
    delegated = DelegatedToken.lookup(token_value)
    if delegated is not None and delegated.user.is_active:
        return delegated.user
    return None


@database_sync_to_async
def _user_from_query_token(scope):
    """?token=<raw> on the WS URL — DelegatedTokens ONLY. Browsers can't set an
    Authorization header on `new WebSocket()`, so short-lived delegated tokens
    ride the query string; long-lived PATs are deliberately rejected here so
    they never land in URLs or access logs."""
    from urllib.parse import parse_qs

    qs = parse_qs((scope.get("query_string") or b"").decode("latin1"))
    values = qs.get("token") or []
    if not values:
        return None
    from apps.tokens.models import DelegatedToken

    token = DelegatedToken.lookup(values[0])
    if token is not None and token.user.is_active:
        return token.user
    return None


class RealtimeAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        user = (
            await _user_from_session(scope)
            or await _user_from_bearer(scope)
            or await _user_from_query_token(scope)
        )
        scope = dict(scope)
        scope["user"] = user or AnonymousUser()
        return await self.app(scope, receive, send)

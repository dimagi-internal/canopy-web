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
def _bearer_method(scope) -> str:
    """`pat` or `delegated` for the bearer token `_user_from_bearer` accepted."""
    raw = _header(scope, b"authorization")
    token_value = raw[7:].decode("latin1").strip() if raw else ""
    from apps.tokens.models import PersonalToken

    return "pat" if PersonalToken.lookup(token_value) is not None else "delegated"


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
def _contact_from_query_token(scope):
    """A CONTACT on `?token=`, for a visitor with no canopy account.

    Separate from `_user_from_query_token` and returning a different thing on
    purpose. Collapsing them would put a contact into `scope["user"]`, where
    every consumer in the app treats it as somebody with memberships — the same
    mistake `ContactToken` exists as its own model to prevent.
    """
    from urllib.parse import parse_qs

    from apps.tokens.models import ContactToken

    qs = parse_qs((scope.get("query_string") or b"").decode("latin1"))
    values = qs.get("token") or []
    if not values:
        return None
    token = ContactToken.lookup(values[0])
    return token.contact if token is not None else None


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
        # WHICH door authenticated this socket, alongside who — the "how sure are
        # we" grade a turn's initiator carries (apps/harness/initiator.py). A PAT
        # and an app-delegated token resolve to the same user and are not the
        # same claim.
        method = "session"
        user = await _user_from_session(scope)
        if user is None:
            user = await _user_from_bearer(scope)
            method = await _bearer_method(scope) if user is not None else ""
        if user is None:
            user = await _user_from_query_token(scope)
            method = "delegated" if user is not None else ""
        scope = dict(scope)
        scope["user"] = user or AnonymousUser()
        scope["auth_method"] = method
        # Only when nothing resolved a user. A contact and a user are never both
        # present, so a consumer cannot accidentally read the wrong one — and
        # `scope["user"]` stays anonymous for a contact, so any consumer that
        # has not been taught about them refuses by its existing check rather
        # than by remembering a new one.
        scope["contact"] = None if user else await _contact_from_query_token(scope)
        return await self.app(scope, receive, send)

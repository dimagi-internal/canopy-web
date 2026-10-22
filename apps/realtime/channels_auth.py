"""WebSocket handshake auth: session cookie, then Bearer, then `?ticket=`.

Ports ace-web's AceSessionAuthMiddleware. Reads settings.SESSION_COOKIE_NAME
(sessionid_canopy on connectlabs, sessionid elsewhere) — never hardcoded. A
bearer is resolved by `apps.tokens.principal.resolve_bearer`, the resolver REST
and MCP use, so scripted clients authenticate over WS exactly as over REST —
once per handshake, the principal carried through rather than looked up again.

Resolution order: session cookie → `Authorization: Bearer` (a runner key, a PAT,
then a delegated token) → `?ticket=` (a delegated token only). A browser cannot put a header on
`new WebSocket()`, so a page holding a delegated or contact token trades it for
a one-time ticket over REST (apps/tokens/ws_ticket.py) and opens the socket with
that. A token is never read from the URL: `?token=` was accepted until
2026-10-06 and put live tokens in access logs.

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


#: Where the handshake keeps the token a `?ticket=` stood for. Set only by
#: `_redeem_ticket`, server-side; a client cannot write a scope key, so this is
#: the one place a token can come from other than the Authorization header.
WS_TOKEN = "canopy.ws_token"


def _ticket_token(scope) -> str:
    return scope.get(WS_TOKEN) or ""


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


def _bearer_raw(scope) -> str:
    raw = _header(scope, b"authorization")
    if not raw or not raw.lower().startswith(b"bearer "):
        return ""
    return raw[7:].decode("latin1").strip()


def _principal_from_bearer_sync(scope, *, stamp: bool = True):
    """The user principal the `Authorization: Bearer` header names, through the
    one resolver every door uses (`apps/tokens/principal.py`) — or None.

    A socket takes a PAT, a delegated token, or a runner's own key; a runner key
    opens only its own runner's control channel (apps/harness/runner_auth.py),
    any other socket is as if anonymous. A socket is a use too: a runner on its
    WS channel made no REST call for long stretches and its token read as idle,
    so a PAT is stamped — except on a re-check (`stamp=False`)."""
    from apps.tokens import principal as P
    from apps.tokens.models import client_label

    value = _bearer_raw(scope)
    if not value:
        return None
    client = client_label((_header(scope, b"user-agent") or b"").decode("latin1"),
                          (_header(scope, b"x-canopy-client") or b"").decode("latin1"))
    found = P.resolve_bearer(value, accept={P.RUNNER_KEY, P.PAT, P.DELEGATED},
                             client=client if stamp else None)
    if found is not None and found.kind == P.RUNNER_KEY:
        from apps.harness import runner_auth

        rtok = found.token
        if not runner_auth.reaches_ws(scope.get("path", ""), rtok.runner_id,
                                      computer=rtok.runner.kind == rtok.runner.COMPUTER):
            return None
    return found


def _principal_from_ticket_sync(scope):
    """`?ticket=`'s token as a user: DelegatedTokens ONLY. Browsers can't set an
    Authorization header on `new WebSocket()`, so short-lived delegated tokens
    ride a ticket; long-lived PATs are never accepted there, so they never land
    in URLs or access logs."""
    from apps.tokens import principal as P

    return P.resolve_bearer(_ticket_token(scope), accept={P.DELEGATED})


@database_sync_to_async
def _user_from_bearer(scope):
    found = _principal_from_bearer_sync(scope)
    return found.user if found is not None else None


async def still_signed_in(scope) -> bool:
    """Does the credential that opened this socket still sign it in, as the same
    user? Re-resolved through the same door `RealtimeAuthMiddleware` used.

    A socket is authenticated once, at the handshake, and then lives for hours.
    Revoking the PAT behind it closed nothing: a runner whose token was revoked
    kept claiming turns over its socket while every REST call it made (events,
    transcript, finish) was refused, so each claimed turn vanished (2026-10-17,
    cloud-ec2-2). A consumer that ACTS on a frame asks this first.

    A scope the middleware never saw (no `auth_method`) has no credential to
    re-check, and keeps the user it was given.
    """
    user = scope.get("user")
    if not getattr(user, "is_authenticated", False):
        return False
    method = scope.get("auth_method")
    if method is None:
        return True
    if method == "session":
        again = await _user_from_session(scope)
    else:
        again = await _recheck_user(scope)
    return again is not None and again.pk == user.pk


@database_sync_to_async
def _recheck_user(scope):
    found = _principal_from_bearer_sync(scope, stamp=False) or _principal_from_ticket_sync(scope)
    return found.user if found is not None else None


@database_sync_to_async
def _contact_from_query_token(scope):
    """A CONTACT on `?token=`, for a visitor with no canopy account.

    Separate from `_user_from_query_token` and returning a different thing on
    purpose. Collapsing them would put a contact into `scope["user"]`, where
    every consumer in the app treats it as somebody with memberships — the same
    mistake `ContactToken` exists as its own model to prevent.
    """
    from apps.tokens import principal as P

    found = P.resolve_bearer(_ticket_token(scope), accept={P.CONTACT})
    return found.token.contact if found is not None else None


@database_sync_to_async
def _user_from_query_token(scope):
    found = _principal_from_ticket_sync(scope)
    return found.user if found is not None else None


def _delegated_token_sync(scope, *, known=None):
    """The DELEGATED token on this socket, or None.

    Read from the same two places a delegated token can ride — the bearer
    header and `?ticket=` — and never from anything else the client controls.
    `known` is the principal the handshake already resolved, so its token is
    not looked up twice."""
    from apps.tokens import principal as P

    if known is not None and known.kind == P.DELEGATED:
        return known.token
    candidates = [] if known is not None else [_bearer_raw(scope)]
    candidates.append(_ticket_token(scope))
    for value in candidates:
        found = P.resolve_bearer(value, accept={P.DELEGATED}) if value else None
        if found is not None:
            return found.token
    return None


@database_sync_to_async
def _delegated_app(scope):
    """The connected site behind a DELEGATED token on this socket, or None."""
    token = _delegated_token_sync(scope)
    return token.app if token is not None else None


@database_sync_to_async
def _delegated_runner_requirements(scope) -> tuple[str, ...]:
    """The runner requirements (ZDR) the site signed into this socket's
    delegated token, or (). The same value REST reads as
    `request.runner_requirements`; the chat consumer stamps it onto the
    session before every send, exactly as the REST send does."""
    token = _delegated_token_sync(scope)
    return tuple(token.runner_requirements or ()) if token is not None else ()


@database_sync_to_async
def _redeem_ticket(scope):
    """`?ticket=` exchanged for the token it stands for (apps/tokens/ws_ticket.py).

    The token goes on the scope under WS_TOKEN — never back into the query
    string — so everything below resolves a ticketed socket exactly as it would
    the token (identity, connected site, runner requirements, assurance), and a
    `token=` a client puts on the URL is read by nothing. An unknown, expired or
    spent ticket leaves no token: the consumer's existing check refuses it.
    """
    from urllib.parse import parse_qs

    scope = dict(scope)
    scope.pop(WS_TOKEN, None)
    values = parse_qs((scope.get("query_string") or b"").decode("latin1")).get("ticket") or []
    if values:
        from apps.tokens import ws_ticket

        raw = ws_ticket.redeem(values[0])
        if raw:
            scope[WS_TOKEN] = raw
    return scope


@database_sync_to_async
def _note_legacy_socket(scope):
    from apps.common import legacy_traffic

    user = scope.get("user")
    ua = (_header(scope, b"user-agent") or b"").decode("latin1")
    legacy_traffic.note(path=scope.get("path", ""), user=user, client=ua, protocol="ws")


@database_sync_to_async
def _resolve_principal(scope):
    """Bearer, then `?ticket=` — the user principal that opens this socket."""
    return _principal_from_bearer_sync(scope) or _principal_from_ticket_sync(scope)


@database_sync_to_async
def _delegated_token(scope, known):
    return _delegated_token_sync(scope, known=known)


class RealtimeAuthMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = await _redeem_ticket(scope)
        # WHICH door authenticated this socket, alongside who — the "how sure are
        # we" grade a turn's initiator carries (apps/harness/initiator.py). A PAT
        # and an app-delegated token resolve to the same user and are not the
        # same claim.
        method = "session"
        user = await _user_from_session(scope)
        found = None
        if user is None:
            found = await _resolve_principal(scope)
            user, method = (found.user, found.auth_method) if found is not None else (None, "")
        # The delegated token on this socket, if any — resolved once, and read
        # even when a session cookie signed the socket in (`via_widget`, and the
        # site's runner requirements, as the REST middleware does).
        delegated = await _delegated_token(scope, found) if user is not None else None
        # `site ∩ user` (apps/tokens/delegation.py). When a site's delegated
        # token is the identity, it opens only its own chat's stream; any other
        # socket (presence, supervisor, turns, runner) is as if unauthenticated.
        # The acting site rides on the scope so the chat consumer can hold it to
        # the site's own agents, exactly as REST does.
        delegated_app = None
        if found is not None and found.kind == "delegated":
            from apps.tokens import delegation

            delegated_app = found.token.app
            if not delegation.reaches_ws(scope.get("path", "")):
                user, method = None, ""
        scope = dict(scope)
        scope["user"] = user or AnonymousUser()
        scope["auth_method"] = method
        scope["delegated_app"] = delegated_app
        # Only ever from a DelegatedToken on this socket (never the client's
        # frames), and read even when a session cookie signed the socket in —
        # the same rule the REST middleware applies to `delegated_app`.
        scope["runner_requirements"] = (
            tuple(delegated.runner_requirements or ()) if user is not None and delegated else ()
        )
        # The agent the site bound the token to, read the same way: the chat
        # consumer opens no other agent's conversation on it.
        scope["bound_agent_id"] = (
            delegated.bound_agent_id if user is not None and delegated else None
        )
        # Only when nothing resolved a user. A contact and a user are never both
        # present, so a consumer cannot accidentally read the wrong one — and
        # `scope["user"]` stays anonymous for a contact, so any consumer that
        # has not been taught about them refuses by its existing check rather
        # than by remembering a new one.
        scope["contact"] = None if user else await _contact_from_query_token(scope)
        # Whether this socket belongs to an embedded widget: a contact, or ANY
        # delegated token on it. Checked apart from `delegated_app` on purpose —
        # canopy embedding its own widget is same-origin, so the session cookie
        # signs the socket in and `delegated_app` is never resolved, yet it is
        # still a widget. What a widget may not receive (tool calls) is decided
        # from this, never from a query value the client chooses.
        scope["via_widget"] = bool(scope["contact"] is not None or delegated is not None)
        from config.asgi_prefix import SCOPE_KEY

        if scope.get(SCOPE_KEY):
            # A socket on the OLD address (apps/common/legacy_traffic.py).
            await _note_legacy_socket(scope)
        return await self.app(scope, receive, send)

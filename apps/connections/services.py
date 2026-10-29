"""Connect, refresh, disconnect and grant — the one implementation every surface calls.

Request-free, like `apps.github_connect.github_app`: takes a `session` mapping
where OAuth state must live between the two legs, raises domain exceptions, and
does no HTTP-framework work. The settings views (person and agent), the CLI
token route and the runner turn-token route all come through here, so they
cannot disagree about what a usable grant is.

An `Owner` is `("user", user_id)` or `("agent", agent_id)`. There is no fallback
between owners: an agent uses its own connection or one explicitly granted to
it, never a person's; a person uses their own.

WHY THE CACHE AND THE LOCK. Salesforce may rotate the refresh token on every
refresh, so two callers refreshing at once can spend the same token and brick
the grant. A cached access token is reused until `ACCESS_TOKEN_MARGIN` before it
expires, and a refresh happens inside a transaction holding `select_for_update`
on the row, re-reading the cache AFTER the lock is taken, so a caller that
queued behind another's refresh uses that refresh's token instead of spending a
dead one (`github_app.access_token_for` is the same structure, for the same
reason).

ONLY `invalid_grant` MEANS RECONNECT. `invalid_grant` (revoked, rotated
elsewhere, user deactivated) stamps `refresh_failed_at`, which is what turns an
opaque 401 into "Reconnect" in the UI. Any other rejection (`invalid_client`,
`unsupported_grant_type`, ...) is canopy's own configuration, which reconnecting
cannot fix: it raises `ConnectionError("provider_error")` and stamps nothing. A
timeout or a 5xx says nothing about the grant either: it raises
`ConnectionError("provider_unavailable")` and stamps nothing.

AUDIT. An agent's connection records events in the agent's workspace (connect,
disconnect, grant, grant revoked; token handouts are recorded by the routes that
know the turn). A person's connection belongs to no workspace and
`Event.workspace` is NOT NULL by design, so connect, disconnect and every token
issue are written to the log at INFO instead. Either way the record carries
provider, owner, account label and who acted, never a token.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone

from apps.agents.models import Agent
from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.events import services as events

from . import providers
from .models import Connection, ConnectionGrant

logger = logging.getLogger(__name__)

Owner = tuple[Literal["user", "agent"], int]

# A cached access token is reused until this long before its expiry, so a token
# handed out does not die mid-operation.
ACCESS_TOKEN_MARGIN = timedelta(minutes=5)

# An agent's connection must be the AGENT's Salesforce user, not whoever happens
# to be signed in to Salesforce in the admin's browser: `login` makes Salesforce
# ask for credentials even with a live session, `consent` shows what is granted.
AGENT_AUTHORIZE_EXTRA = {"prompt": "login consent"}

# Where pending authorizations wait between `begin` and `complete`, keyed by
# `state`. Session-bound so the callback is tied to this browser; popped so a
# state is single-use.
SESSION_KEY = "connections_oauth"
# A browser that starts many flows and finishes none should not grow its session
# without bound; the oldest pending ones are dropped.
MAX_PENDING = 10

EVENT_SOURCE = "connections"


class NotConfigured(Exception):  # noqa: N818 - name fixed by the interface
    """The provider has no client id/secret on this deployment."""


class ConnectionError(Exception):  # shadows the builtin inside this module only; the interface names it
    """A connection operation failed. `code` is stable and safe to show:
    `bad_state`, `provider_error`, `provider_unavailable`, `ambiguous`,
    `own_connection`, `not_grantable`."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


class ReconnectRequired(ConnectionError):  # noqa: N818 - name fixed by the interface
    """The stored grant cannot mint a token any more; the owner must connect again."""

    def __init__(self, message: str = "") -> None:
        super().__init__("reconnect_required", message)


class NotConnected(Exception):  # noqa: N818 - name fixed by the interface
    """No connection for that owner and provider."""


@dataclass(frozen=True)
class AccessToken:
    token: str
    instance_url: str
    expires_at: datetime
    account_label: str


def owner_of(conn: Connection) -> Owner:
    return ("agent", conn.agent_id) if conn.agent_id is not None else ("user", conn.user_id)


def _owner_filter(owner: Owner) -> dict:
    kind, pk = owner
    if kind == "agent":
        return {"agent_id": pk}
    if kind == "user":
        return {"user_id": pk}
    raise ValueError(f"unknown owner kind {kind!r}")


def connections_for(owner: Owner) -> QuerySet[Connection]:
    return Connection.objects.filter(**_owner_filter(owner)).order_by("provider", "account_label", "pk")


def _configured(provider: str) -> providers.Provider:
    p = providers.get(provider)
    if not p.is_configured():
        raise NotConfigured(f"{p.title} is not set up on this deployment")
    return p


# ---- events ------------------------------------------------------------------

def _audit_fields(conn: Connection) -> dict:
    """What an audit record says about a connection, captured while the row
    exists (a disconnect records after deleting). Never a token."""
    return {"provider": conn.provider, "owner": conn.owner_label, "account_label": conn.account_label,
            "workspace": conn.agent.workspace if conn.agent_id is not None else None}


def _event(fields: dict, kind: str, by, summary: str, **extra) -> None:
    """An agent's connection: an Event in its workspace. A person's: an INFO log line."""
    who = (getattr(by, "email", "") or getattr(by, "username", "")) if by is not None else ""
    payload = {"provider": fields["provider"], "owner": fields["owner"],
               "account_label": fields["account_label"], "by": who, **extra}
    if fields["workspace"] is None:
        logger.info("connections.%s by=%s provider=%s owner=%s account_label=%s", kind, who or "-",
                    payload["provider"], payload["owner"], payload["account_label"])
        return
    try:
        events.record([{
            "source": EVENT_SOURCE, "kind": f"connections.{kind}", "level": "info",
            "summary": summary[:500], "payload": payload,
        }], workspace=fields["workspace"])
    except Exception:  # noqa: BLE001 - the audit row must never undo the change it records
        logger.exception("could not record connections.%s for %s", kind, fields["owner"])


def _issued(conn: Connection) -> None:
    """A person's token handout, logged. An agent's is recorded by the route
    that hands it out (it knows the turn and runner)."""
    if conn.agent_id is None:
        _event(_audit_fields(conn), "token_issued", None, "")


# ---- the authorization web flow ----------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier = _secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def begin(session, *, provider: str, owner: Owner, redirect_uri: str, return_to: str = "") -> str:
    """Park a PKCE verifier and a CSRF `state` in the session; return the authorize URL.

    The verifier never leaves the server; only its S256 challenge goes to the
    provider, so an intercepted `code` cannot be exchanged by anyone else.
    """
    p = _configured(provider)
    kind, owner_id = owner
    _owner_filter(owner)  # validates the kind
    verifier, challenge = _pkce_pair()
    state = _secrets.token_urlsafe(32)
    pending = dict(session.get(SESSION_KEY) or {})
    while len(pending) >= MAX_PENDING:
        pending.pop(next(iter(pending)))
    pending[state] = {"provider": provider, "owner_kind": kind, "owner_id": owner_id,
                      "verifier": verifier, "return_to": return_to}
    session[SESSION_KEY] = pending  # reassigned so a Django session marks itself modified
    return p.authorize_url(state, redirect_uri, challenge,
                           extra=AGENT_AUTHORIZE_EXTRA if kind == "agent" else None)


def pending_for(session, state: str) -> dict | None:
    """What `begin` parked under `state`, without consuming it (for a callback
    that must check the owner before completing)."""
    return (session.get(SESSION_KEY) or {}).get(state)


def complete(session, *, provider: str, code: str, state: str, redirect_uri: str, by) -> Connection:
    """Exchange `code`, read who the account is, and upsert the grant.

    The state is popped first, so a failed exchange still spends it. Nothing is
    stored unless the exchange AND the identity lookup succeed. Connecting the
    same account again for the same owner updates that row (and clears any
    reconnect stamp) rather than adding a second.
    """
    pending = dict(session.get(SESSION_KEY) or {})
    parked = pending.pop(state, None) if state else None
    session[SESSION_KEY] = pending
    if not parked or parked.get("provider") != provider:
        raise ConnectionError("bad_state", "start the connection again")

    p = _configured(provider)
    try:
        token = p.exchange_code(code, redirect_uri, parked["verifier"])
        ident = p.identity(token)
    except providers.ProviderError as e:
        raise ConnectionError("provider_error", str(e)) from None
    if not token.refresh_token:
        raise ConnectionError("provider_error", f"{p.title} issued no refresh token")

    if parked["owner_kind"] == "agent":
        agent = Agent.objects.filter(pk=parked["owner_id"]).first()
        if agent is None:  # deleted between begin and complete
            raise ConnectionError("bad_state", "that agent no longer exists")
        owner_kw = {"agent": agent}
    else:
        owner_kw = {"user_id": parked["owner_id"]}
    lookup = {"provider": provider, "account_id": ident.account_id, **owner_kw}
    defaults = {
        "account_label": ident.label,
        "instance_url": token.instance_url,
        "scopes": " ".join(p.scopes),
        "refresh_token_enc": encrypt_secret(token.refresh_token),
        "access_token_enc": encrypt_secret(token.access_token),
        "access_token_expires_at": token.expires_at,
        "refresh_failed_at": None,
        "connected_by": by,
    }
    try:
        with transaction.atomic():
            conn, _ = Connection.objects.update_or_create(**lookup, defaults=defaults)
    except IntegrityError:
        # A concurrent completion for the same account inserted the row between
        # our lookup and insert. It exists now: update it with this grant.
        with transaction.atomic():
            conn = Connection.objects.select_for_update().get(**lookup)
            for k, v in defaults.items():
                setattr(conn, k, v)
            conn.save()
    _event(_audit_fields(conn), "connected", by,
           f"{conn.owner_label} connected {p.title} as {conn.account_label}")
    return conn


# ---- using the grant ---------------------------------------------------------

def _cached_token(conn: Connection, min_life: timedelta) -> str:
    """The row's access token if it is good for at least `min_life` longer, else
    "". An undecryptable value (a rotated key) counts as no cache."""
    if not (conn.access_token_enc and conn.access_token_expires_at):
        return ""
    if conn.access_token_expires_at - min_life <= timezone.now():
        return ""
    try:
        return decrypt_secret(conn.access_token_enc)
    except Exception:  # noqa: BLE001
        return ""


def _handout(conn: Connection, token: str) -> AccessToken:
    return AccessToken(token=token, instance_url=conn.instance_url,
                       expires_at=conn.access_token_expires_at, account_label=conn.account_label)


def access_token_for(conn: Connection, *, min_life: timedelta = ACCESS_TOKEN_MARGIN,
                     refresh: bool = False) -> AccessToken:
    """A usable access token: the cached one while it has at least `min_life` left,
    else refreshed under the row lock.

    `min_life` is what the caller needs (a runner that re-stages every few minutes
    asks for more than the default margin). `refresh` skips the cache entirely —
    for a caller whose token Salesforce already rejected (a 401 before the assumed
    expiry); it still refreshes under the lock, so it cannot race another refresh.
    """
    conn = Connection.objects.get(pk=conn.pk)
    if conn.needs_reconnect:
        raise ReconnectRequired(f"reconnect {conn.provider}")
    cached = "" if refresh else _cached_token(conn, min_life)
    if cached:
        conn.last_used_at = timezone.now()
        Connection.objects.filter(pk=conn.pk).update(last_used_at=conn.last_used_at)
        _issued(conn)
        return _handout(conn, cached)

    p = _configured(conn.provider)
    rejected: providers.ProviderError | None = None
    with transaction.atomic():
        # Re-read under the lock: another process may have refreshed (or had its
        # refresh rejected) while this one waited.
        conn = Connection.objects.select_for_update().get(pk=conn.pk)
        if conn.needs_reconnect:
            raise ReconnectRequired(f"reconnect {conn.provider}")
        cached = "" if refresh else _cached_token(conn, min_life)
        if cached:
            conn.last_used_at = timezone.now()
            conn.save(update_fields=["last_used_at", "updated_at"])
            _issued(conn)
            return _handout(conn, cached)

        try:
            token = p.refresh(decrypt_secret(conn.refresh_token_enc))
        except providers.ProviderUnavailable as e:
            raise ConnectionError("provider_unavailable", str(e)) from None
        except providers.ProviderError as e:
            if e.code != "invalid_grant":
                # Not about the grant (our client config, an unexpected answer):
                # reconnecting would not fix it, so nothing is stamped.
                raise ConnectionError("provider_error", str(e)) from None
            # Stamped inside the block and raised after it, so the stamp commits
            # rather than rolling back with the exception.
            conn.refresh_failed_at = timezone.now()
            conn.save(update_fields=["refresh_failed_at", "updated_at"])
            rejected = e
        else:
            if token.refresh_token:  # rotated: the old one is dead now
                conn.refresh_token_enc = encrypt_secret(token.refresh_token)
            if token.instance_url:
                conn.instance_url = token.instance_url
            conn.access_token_enc = encrypt_secret(token.access_token)
            conn.access_token_expires_at = token.expires_at
            conn.refresh_failed_at = None
            conn.last_used_at = timezone.now()
            conn.save(update_fields=[
                "refresh_token_enc", "instance_url", "access_token_enc", "access_token_expires_at",
                "refresh_failed_at", "last_used_at", "updated_at",
            ])
            _issued(conn)
            return _handout(conn, token.access_token)

    logger.warning("connection %s refresh rejected (%s); reconnect required", conn.pk, rejected.code)
    raise ReconnectRequired(f"{conn.provider} rejected the stored grant ({rejected.code})")


def disconnect(conn: Connection, *, by) -> None:
    """Revoke at the provider (best effort) and delete the row.

    A failed revoke is logged, not raised: the person asked for canopy to stop
    holding the grant, and deleting it does that whatever the provider says.
    """
    refresh = ""
    try:
        refresh = decrypt_secret(conn.refresh_token_enc)
    except Exception:  # noqa: BLE001 - an unreadable token cannot be revoked; still delete
        logger.warning("connection %s: refresh token undecryptable; deleting without revoke", conn.pk)
    if refresh:
        try:
            providers.get(conn.provider).revoke(refresh)
        except Exception as e:  # noqa: BLE001
            logger.warning("connection %s: revoke failed (%s); deleting anyway", conn.pk, type(e).__name__)
    fields = _audit_fields(conn)
    conn.delete()
    _event(fields, "disconnected", by, f"{fields['owner']} disconnected {fields['provider']} "
                                       f"({fields['account_label']})")


# ---- which connection an agent uses; grants ----------------------------------

def connection_for_agent(agent, provider: str) -> Connection:
    """The agent's own connection for `provider`, else one granted to it.

    Never a person's connection. More than one candidate at the level that
    answers is `ambiguous` rather than a guess.
    """
    own = list(Connection.objects.filter(agent=agent, provider=provider)[:2])
    if len(own) == 1:
        return own[0]
    if len(own) > 1:
        raise ConnectionError("ambiguous", f"{agent.slug} holds more than one {provider} connection")
    granted = list(Connection.objects.filter(
        grants__agent=agent, provider=provider, agent__isnull=False,
    ).distinct()[:2])
    if len(granted) == 1:
        return granted[0]
    if len(granted) > 1:
        raise ConnectionError("ambiguous", f"{agent.slug} is granted more than one {provider} connection")
    raise NotConnected(f"{agent.slug} has no {provider} connection")


def grant(conn: Connection, agent, *, by) -> ConnectionGrant:
    """Let `agent` use another agent's connection. Idempotent."""
    if conn.agent_id is None:
        raise ConnectionError("not_grantable", "a person's connection is never granted to an agent")
    if conn.agent_id == agent.pk:
        raise ConnectionError("own_connection", "an agent already uses its own connection")
    if not (conn.agent.is_admin(by) and agent.is_admin(by)):
        raise PermissionDenied("granting a connection needs admin of both agents")
    g, created = ConnectionGrant.objects.get_or_create(
        connection=conn, agent=agent, defaults={"granted_by": by},
    )
    if created:
        _event(_audit_fields(conn), "granted", by, f"{conn.owner_label}'s {conn.provider} granted to {agent.slug}",
               grantee=agent.slug)
    return g


def revoke_grant(g: ConnectionGrant, *, by) -> None:
    """Stop `g.agent` using the connection. Admin of EITHER agent may: taking
    access away should never need more standing than either side has."""
    conn, agent = g.connection, g.agent
    if not (conn.agent.is_admin(by) or agent.is_admin(by)):
        raise PermissionDenied("revoking a grant needs admin of either agent")
    g.delete()
    _event(_audit_fields(conn), "grant_revoked", by,
           f"{conn.owner_label}'s {conn.provider} no longer granted to {agent.slug}", grantee=agent.slug)

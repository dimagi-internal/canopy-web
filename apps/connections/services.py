"""Connect, refresh, hand out, disconnect and grant — the one implementation every surface calls.

Request-free, like `apps.github_connect.github_app`: takes a `session` mapping
where OAuth state must live between the two legs, raises domain exceptions, and
does no HTTP-framework work. The settings views (person and agent), the CLI
token route and the runner turn-token routes all come through here, so they
cannot disagree about what a usable grant is. Nothing here names a provider:
what differs between providers lives in `providers/`.

THE APP. Every flow runs against one workspace's `ConnectionApp` (`app_for`):
its client id and secret, its options, its chosen scopes. A connection keeps the
app that minted it, and is refreshed and revoked with that app's credentials,
so rotating an app's secret carries every connection with it. `begin` parks the
app and `complete` refuses a callback for any other provider or workspace (an
OAuth mix-up defence: a code is only ever exchanged by the client it was issued
to). A person's connection is usable only while they can still read its app's
workspace; an agent connects with its own workspace's app, and its connection
is usable only while the agent is still in that app's workspace (a moved agent's
old connection stops; so do the grants of it, wherever the grantee is).

An `Owner` is `("user", user_id)` or `("agent", agent_id)`. There is no fallback
between owners: an agent uses its own connection or one explicitly granted to
it, never a person's; a person uses their own.

WHY THE CACHE AND THE LOCK. A provider may rotate the refresh token on every
refresh (Salesforce does), so two callers refreshing at once can spend the same
token and brick the grant. A cached access token is reused while it has the
caller's `min_life` left, and a refresh happens inside a transaction holding
`select_for_update` on the row, re-reading the cache AFTER the lock is taken, so
a caller that queued behind another's refresh uses that refresh's token instead
of spending a dead one (`github_app.access_token_for` is the same structure,
for the same reason).

ONLY `invalid_grant` MEANS RECONNECT. `invalid_grant` (revoked, rotated
elsewhere, user deactivated) stamps `refresh_failed_at`, which is what turns an
opaque 401 into "Reconnect" in the UI. Any other rejection (`invalid_client`,
`unsupported_grant_type`, ...) is canopy's own configuration, which reconnecting
cannot fix: it raises `provider_error` and stamps nothing. A timeout or a 5xx
says nothing about the grant either: `provider_unavailable`, nothing stamped.

AUDIT AND LOGS go through one function, `record()`, with one vocabulary:
`connections.<verb>` and the payload keys `connection`, `provider`, `owner`
(`agent:<slug>` | `user:<id>`; "" for an app), `account_label`, `by`, plus the
verb's own (`to`, `turn`, `runner`, `agent`, `grantee`, `refreshed`, `during`,
`error`, `workspace`, `client_id`). An audit record about an agent's connection
is an Event in the agent's workspace, and one about an app
(`app_configured`, `app_removed`) an Event in the app's workspace; a person's
connection belongs to no workspace (`Event.workspace` is NOT NULL by design),
so the same record is a log line. Operational records
(`provider_error`, `revoke_failed`: canopy's problem, not the owner's history)
are log lines for every owner. A log line is `connections.<verb> key=value …`
(`docs/architecture/vault.md` § Connection audit). Never a token, a code, a
state or a verifier.
"""
from __future__ import annotations

import base64
import dataclasses
import hashlib
import json
import logging
import re
import secrets as _secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError, transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from apps.agents.models import Agent
from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.events import services as events
from apps.workspaces import permissions as perms
from apps.workspaces import services as workspaces

from . import providers
from .models import Connection, ConnectionApp, ConnectionGrant

logger = logging.getLogger(__name__)

Owner = tuple[Literal["user", "agent"], int]

# A cached access token is reused until this long before its expiry, so a token
# handed out does not die mid-operation.
ACCESS_TOKEN_MARGIN = timedelta(minutes=5)

# Where pending authorizations wait between `begin` and `complete`, keyed by
# `state`. Session-bound so the callback is tied to this browser; popped so a
# state is single-use.
SESSION_KEY = "connections_oauth"
# A browser that starts many flows and finishes none should not grow its session
# without bound; the oldest pending ones are dropped.
MAX_PENDING = 10

EVENT_SOURCE = "connections"


# ---- errors --------------------------------------------------------------------

class ConnectionError(Exception):  # shadows the builtin inside this module only; the interface names it
    """A connection operation failed. `code` is stable and safe to show; the
    routes map it to a response in one place (`problems.problem_for`):
    `not_configured`, `not_connected`, `reconnect_required`, `ambiguous`,
    `insufficient_scope`, `provider_error`, `provider_unavailable`, `bad_state`,
    `own_connection`, `not_grantable`, `not_usable`, `app_locked` (an app's client id, or an
    option naming its authorization server, cannot change while connections
    use it), `invalid_app` (an app's settings
    were refused; the message says why), `wrong_account` (the provider signed
    in an account this owner must not connect)."""

    code = ""

    def __init__(self, code: str = "", message: str = "") -> None:
        self.code = code or self.code
        super().__init__(message or self.code)


class NotConfigured(ConnectionError):  # noqa: N818 - name fixed by the interface
    """The workspace has no app (client id and secret) for the provider."""

    code = "not_configured"

    def __init__(self, message: str = "") -> None:
        super().__init__("", message)


class NotConnected(ConnectionError):  # noqa: N818 - name fixed by the interface
    """No connection for that owner and provider."""

    code = "not_connected"

    def __init__(self, message: str = "") -> None:
        super().__init__("", message)


class ReconnectRequired(ConnectionError):  # noqa: N818 - name fixed by the interface
    """The stored grant cannot mint a token any more; the owner must connect again."""

    code = "reconnect_required"

    def __init__(self, message: str = "") -> None:
        super().__init__("", message)


@dataclass(frozen=True)
class AccessToken:
    token: str
    instance_url: str
    expires_at: datetime
    account_label: str
    refreshed: bool = False  # minted by a refresh just now, rather than the cached one


def _owner_filter(owner: Owner) -> dict:
    kind, pk = owner
    if kind == "agent":
        return {"agent_id": pk}
    if kind == "user":
        return {"user_id": pk}
    raise ValueError(f"unknown owner kind {kind!r}")


def connections_for(owner: Owner) -> QuerySet[Connection]:
    return Connection.objects.filter(**_owner_filter(owner)).order_by("provider", "account_label", "pk")


def _configured(app: ConnectionApp | None, provider: str = "") -> providers.Provider:
    """`app`'s provider, bound to it; NotConfigured without a client id and secret."""
    if app is None:
        raise NotConfigured(f"{provider} is not set up for this workspace")
    p = providers.for_app(app)
    if not p.is_configured():
        raise NotConfigured(f"{p.title} is not set up for workspace {app.workspace_id}")
    return p


# ---- the workspace's app -------------------------------------------------------

def _slug(workspace) -> str:
    return getattr(workspace, "slug", workspace)


def app_for(workspace, provider: str) -> ConnectionApp:
    """The workspace's (a `Workspace` or slug) app for `provider`, set up with a
    client id and secret; NotConfigured otherwise. UnknownProvider for a
    provider not registered."""
    providers.get_class(provider)
    app = (ConnectionApp.objects.select_related("workspace")
           .filter(workspace_id=_slug(workspace), provider=provider).first())
    _configured(app, provider)
    return app


def readable_workspaces(user) -> set[str]:
    """The workspaces `user` can read: THE rule for whose apps a person may use
    and whose connections still hand them a token (`workspaces` decides membership)."""
    return {slug for slug, role in workspaces.member_roles(user).items() if perms.role_allows(role, perms.READ)}


def person_may_use(user, workspace) -> bool:
    """Whether `user` may connect with, and use connections from, the apps of
    `workspace` (a `Workspace` or slug): they can read it."""
    return _slug(workspace) in readable_workspaces(user)


def apps_for_person(user, provider: str) -> list[ConnectionApp]:
    """The set-up apps for `provider` in every workspace `user` can read, by workspace."""
    if provider not in providers.CLASSES:
        return []
    rows = (ConnectionApp.objects.select_related("workspace")
            .filter(provider=provider, workspace_id__in=readable_workspaces(user)).order_by("workspace_id"))
    return [a for a in rows if providers.for_app(a).is_configured()]


def _app_subject(app: ConnectionApp) -> Subject:
    return Subject(connection=None, provider=app.provider, owner="", account_label="", workspace=app.workspace)


def configure_app(workspace, provider: str, *, client_id: str, client_secret: str = "",
                  options: dict | None = None, scopes=(), by) -> ConnectionApp:
    """Create or update the workspace's app for `provider`.

    A blank `client_secret` on an update keeps the stored one (rotating it is
    sending a new one; existing connections then refresh with it), except with
    a new client id, which needs its own secret. `options` are merged over the
    stored ones, so an update that leaves one out keeps it. While connections
    were made with the app, neither the client id nor an option that names the
    authorization server (`Provider.locked_options`; Salesforce's login host)
    may change (`app_locked`): their grants belong to that client at that
    server, so switching means removing the app, which disconnects them.
    Settings the provider refuses are `invalid_app`. Callers check who may do
    this; records `app_configured` (never the secret)."""
    cls = providers.get_class(provider)
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id:
        raise ConnectionError("invalid_app", "A client id is required")
    chosen = set(scopes or ())
    unknown = sorted(chosen - set(cls.scope_choices))
    if unknown:
        raise ConnectionError("invalid_app", f"{cls.title} offers no scope {', '.join(unknown)}")
    slug = _slug(workspace)
    # Two first saves at once (a double-clicked Save): no row exists for either to
    # lock, so the loser's insert hits the unique constraint; it retries as an update.
    for attempt in range(2):
        try:
            with transaction.atomic():
                app = _save_app(cls, slug, client_id, client_secret, options or {}, chosen, by)
            break
        except IntegrityError:
            if attempt:
                raise
    app = ConnectionApp.objects.select_related("workspace").get(pk=app.pk)
    record(_app_subject(app), "app_configured", by=by, workspace=app.workspace_id, client_id=app.client_id,
           summary=f"{_who(by) or 'someone'} configured {cls.title} for {app.workspace_id}")
    return app


def _save_app(cls, slug, client_id, client_secret, options, chosen, by) -> ConnectionApp:
    """`configure_app`'s write, inside its transaction, under the app row's lock."""
    app = ConnectionApp.objects.select_for_update().filter(workspace_id=slug, provider=cls.name).first()
    try:
        cleaned = cls.clean_options({**(app.options if app is not None else {}), **options})
    except ValueError as e:
        raise ConnectionError("invalid_app", str(e)) from None
    if app is None:
        if not client_secret:
            raise ConnectionError("invalid_app", "A client secret is required")
        app = ConnectionApp(workspace_id=slug, provider=cls.name)
    else:
        if client_id != app.client_id and not client_secret:
            raise ConnectionError("invalid_app", "A new client id needs its client secret")
        if not client_secret and not app.client_secret:
            # Stored, but unreadable (the encryption key changed): keeping it would
            # save an app that still cannot connect, and say nothing.
            raise ConnectionError("invalid_app", "The saved client secret can no longer be read; enter it again")
        # Compared as cleaned, so a stored value in another spelling of the same
        # setting (`https://login.salesforce.com/`) is not a change.
        try:
            stored = cls.clean_options(app.options)
        except ValueError:  # a row that bypassed `configure_app`: compare as stored
            stored = app.options
        changed = [name for name in cls.locked_options if cleaned.get(name) != stored.get(name)]
        if (client_id != app.client_id or changed) and app.connections.exists():
            labels = {f.name: f.label for f in cls.option_fields}
            what = " and ".join((["client id"] if client_id != app.client_id else [])
                                + [labels.get(n, n) for n in changed])
            raise ConnectionError("app_locked", f"Accounts are connected with this {cls.title} app, so its {what} "
                                                "cannot change; remove the app (which disconnects them) to switch.")
    app.client_id = client_id
    if client_secret:
        app.client_secret = client_secret
    app.options = cleaned
    app.scopes = " ".join(s for s in cls.scope_choices if s in chosen)
    app.configured_by = by
    app.save()
    return app


def remove_app(app: ConnectionApp, *, by) -> None:
    """Disconnect every connection made with `app` (each revoked best effort and
    recorded as `disconnected` by `by`), then delete the app.

    A connect that commits while this runs is caught too: the app row is locked
    for the delete, and whatever connections it holds then are deleted with it
    and revoked afterwards, outside the transaction."""
    # Revokes run in this request, one at a time (each bounded by HTTP_TIMEOUT):
    # fine for a workspace's handful of connections; a route that may face
    # hundreds should budget for it rather than parallelise blindly.
    for conn in list(app.connections.select_related("app__workspace", "agent__workspace").order_by("pk")):
        disconnect(conn, by=by)
    subject = _app_subject(app)
    title = getattr(providers.CLASSES.get(app.provider), "title", app.provider)
    client_id, slug = app.client_id, app.workspace_id
    late: list[tuple[Subject, str | None, list]] = []
    with transaction.atomic():
        locked = ConnectionApp.objects.select_for_update().filter(pk=app.pk).first()
        if locked is not None:
            for conn in locked.connections.select_related("agent__workspace").order_by("pk"):
                # Read only: an unreadable token is recorded after the transaction.
                late.append((Subject.of(conn), _decrypted_refresh(conn), _grantees(conn)))
                conn.delete()
            locked.delete()
    for late_subject, refresh, grantees in late:
        if refresh is None:
            record(late_subject, "revoke_failed", warn=True, error="undecryptable")
        _revoke(app, late_subject, refresh or "")
        record(late_subject, "disconnected", by=by,
               summary=f"{late_subject.owner} disconnected {late_subject.provider} ({late_subject.account_label})")
        for grantee in grantees:
            _record_grant_revoked(late_subject, grantee, by=by, during="disconnected")
    record(subject, "app_removed", by=by, workspace=slug, client_id=client_id,
           summary=f"{_who(by) or 'someone'} removed {title} from {slug}")


# ---- the audit trail and the log: one vocabulary ---------------------------------

# Verbs that are the owner's history: an Event for an agent's connection, a log
# line for a person's. The rest are canopy's operational trouble: always a log line.
AUDITED = frozenset({"connected", "disconnected", "granted", "grant_revoked", "token_issued",
                     "reconnect_required", "app_configured", "app_removed"})


@dataclass(frozen=True)
class Subject:
    """What a record says about a connection, captured while the row exists (a
    disconnect records after deleting it). Never a token."""

    connection: int | None  # None before a row exists (a connect that failed), and for an app
    provider: str
    owner: str  # "agent:<slug>" | "user:<id>"; "" for an app
    account_label: str
    workspace: Any  # the owning agent's (or the app's) Workspace, None for a person's connection

    @classmethod
    def of(cls, conn: Connection) -> Subject:
        agent = conn.agent if conn.agent_id is not None else None
        return cls(connection=conn.pk, provider=conn.provider,
                   owner=f"agent:{agent.slug}" if agent else f"user:{conn.user_id}",
                   account_label=conn.account_label, workspace=agent.workspace if agent else None)


def _parked_subject(provider: str, parked: dict) -> Subject:
    """A Subject for a connect that has no row yet: the would-be owner, by slug for an agent."""
    if parked["owner_kind"] == "agent":
        slug = Agent.objects.filter(pk=parked["owner_id"]).values_list("slug", flat=True).first()
        owner = f"agent:{slug or parked['owner_id']}"
    else:
        owner = f"user:{parked['owner_id']}"
    return Subject(connection=None, provider=provider, owner=owner, account_label="", workspace=None)


def _who(by) -> str:
    return (getattr(by, "email", "") or getattr(by, "username", "")) if by is not None else ""


def log_line(kind: str, payload: dict) -> str:
    """`connections.<kind> key=value …`, always one line. A value that is empty,
    not printable, or holds whitespace, `=` or `"` is a JSON string (printable
    non-ASCII kept as is, every non-printable character `\\uXXXX`);
    `parse_log_line` reverses it exactly."""
    def value(v) -> str:
        s = "" if v is None else str(v).lower() if isinstance(v, bool) else str(v)
        plain = s and s.isprintable() and not any(c.isspace() or c in '="' for c in s)
        if plain:
            return s
        # Readable non-ASCII, but every non-printable character escaped (\uXXXX),
        # not only the ones JSON must: U+2028/U+2029/U+0085 and friends would
        # otherwise reach a log shipper that splits lines on them.
        return "".join(c if c.isprintable() else json.dumps(c)[1:-1]
                       for c in json.dumps(s, ensure_ascii=False))

    return f"connections.{kind} " + " ".join(f"{k}={value(v)}" for k, v in payload.items())


_PAIR = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S*)')


def parse_log_line(line: str) -> tuple[str, dict[str, str]]:
    """`(kind, {key: value})` from a `log_line`; quoted values are decoded."""
    head, _, rest = line.partition(" ")
    pairs = {k: (json.loads(v) if v.startswith('"') else v) for k, v in _PAIR.findall(rest)}
    return head.removeprefix("connections."), pairs


def record(subject: Subject, kind: str, *, by=None, warn: bool = False, summary: str = "",
           key: str = "", **context) -> None:
    """Record one `connections.<kind>` fact — THE way this app audits and logs.

    An audited verb about an agent's connection, or about an app, is an Event
    in that workspace; otherwise a log line, at WARNING when `warn`. `context` adds the verb's own
    keys; none of them may ever be a secret."""
    payload = {"connection": subject.connection, "provider": subject.provider, "owner": subject.owner,
               "account_label": subject.account_label, "by": _who(by), **context}
    if kind not in AUDITED or subject.workspace is None:
        logger.log(logging.WARNING if warn else logging.INFO, log_line(kind, payload))
        return
    try:
        events.record([{
            "source": EVENT_SOURCE, "kind": f"connections.{kind}", "level": "warn" if warn else "info",
            "key": key, "summary": (summary or f"{subject.owner} {kind} {subject.provider}")[:500],
            "payload": payload,
        }], workspace=subject.workspace)
    except Exception:  # noqa: BLE001 - the audit row must never undo the change it records
        logger.exception("could not record connections.%s for %s", kind, subject.owner)


# ---- the authorization web flow ----------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    verifier = _secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def begin(session, *, app: ConnectionApp, owner: Owner, redirect_uri: str, return_to: str = "") -> str:
    """Park a PKCE verifier, a CSRF `state` and `app` in the session; return the
    authorize URL (with `app`'s client).

    The verifier never leaves the server; only its S256 challenge goes to the
    provider, so an intercepted `code` cannot be exchanged by anyone else.
    """
    p = _configured(app)
    kind, owner_id = owner
    _owner_filter(owner)  # validates the kind
    verifier, challenge = _pkce_pair()
    state = _secrets.token_urlsafe(32)
    pending = dict(session.get(SESSION_KEY) or {})
    while len(pending) >= MAX_PENDING:
        pending.pop(next(iter(pending)))
    pending[state] = {"app": app.pk, "provider": app.provider, "workspace": app.workspace_id,
                      "client_id": app.client_id,
                      "owner_kind": kind, "owner_id": owner_id, "verifier": verifier, "return_to": return_to}
    session[SESSION_KEY] = pending  # reassigned so a Django session marks itself modified
    owner_obj = (Agent if kind == "agent" else get_user_model()).objects.filter(pk=owner_id).first()
    return p.authorize_url(state, redirect_uri, challenge, owner_kind=kind, owner=owner_obj)


def pending_for(session, state: str) -> dict | None:
    """What `begin` parked under `state`, without consuming it (for a callback
    that must check the owner before completing)."""
    return (session.get(SESSION_KEY) or {}).get(state)


def spend(session, state: str) -> None:
    """Drop one parked flow without completing it (a callback refused before the exchange)."""
    session[SESSION_KEY] = {k: v for k, v in (session.get(SESSION_KEY) or {}).items() if k != state}


# An `?error=` the provider sends to the callback is the browser's query string:
# only a code-shaped one is written to the log.
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_.-]{0,63}")


def authorize_refused(session, *, provider: str, state: str, error: str, by) -> str:
    """The provider sent the browser back with `?error=` instead of a code: spend
    the state, and log it unless the person simply declined (`access_denied`).
    Returns the callback reason (`access_denied` or `provider_error`)."""
    parked = pending_for(session, state)
    spend(session, state)
    if error == "access_denied":
        return "access_denied"
    if parked:
        record(_parked_subject(provider, parked), "provider_error", by=by, warn=True, during="authorize",
               error=error if _ERROR_CODE.fullmatch(error or "") else "unrecognised")
    return "provider_error"


def complete(session, *, provider: str, workspace, code: str, state: str, redirect_uri: str, by) -> Connection:
    """Exchange `code` with the parked app, read who the account is, and upsert the grant.

    The state is popped first, so a failed exchange still spends it. A callback
    for another provider or workspace than the flow was begun with is
    `bad_state`, and nothing is exchanged. Nothing is stored unless the
    exchange, the identity lookup and the provider's `check_owner` succeed;
    a grant refused after the exchange is revoked at the provider (best effort).
    Connecting the same account again for the same owner with the same app
    updates that row (and clears any reconnect stamp) rather than adding a second.
    """
    pending = dict(session.get(SESSION_KEY) or {})
    parked = pending.pop(state, None) if state else None
    session[SESSION_KEY] = pending
    if not parked or parked.get("provider") != provider or parked.get("workspace") != _slug(workspace):
        raise ConnectionError("bad_state", "start the connection again")
    app = (ConnectionApp.objects.select_related("workspace")
           .filter(pk=parked.get("app"), provider=provider, workspace_id=parked["workspace"]).first())
    # Removed, or given another client, between begin and complete: the code was
    # issued to a client this app no longer is.
    if app is None or app.client_id != parked.get("client_id"):
        raise ConnectionError("bad_state", "that app has changed; start the connection again")

    p = _configured(app)
    if parked["owner_kind"] == "agent":
        agent = Agent.objects.filter(pk=parked["owner_id"]).first()
        if agent is None:  # deleted between begin and complete
            raise ConnectionError("bad_state", "that agent no longer exists")
        owner_kw, owner_obj = {"agent": agent}, agent
    else:
        owner_kw = {"user_id": parked["owner_id"]}
        owner_obj = get_user_model().objects.filter(pk=parked["owner_id"]).first()
    # No row yet: a refusal is logged against the would-be owner.
    subject = _parked_subject(provider, parked)
    try:
        token = p.exchange_code(code, redirect_uri, parked["verifier"])
    except providers.ProviderError as e:
        record(subject, "provider_error", by=by, warn=True, during="connect", error=_error_code(e))
        raise ConnectionError("provider_error", str(e)) from None
    # From here the provider has issued a grant: every refusal revokes it, so a
    # refused account (the admin's own, signed in by mistake) is not left holding
    # a live grant to this client that canopy has thrown away.
    try:
        ident = p.identity(token)
    except providers.ProviderError as e:
        record(subject, "provider_error", by=by, warn=True, during="connect", error=_error_code(e))
        _discard_grant(p, subject, token)
        raise ConnectionError("provider_error", str(e)) from None
    if not token.refresh_token:
        record(subject, "provider_error", by=by, warn=True, during="connect", error="no_refresh_token")
        _discard_grant(p, subject, token)
        raise ConnectionError("provider_error", f"{p.title} issued no refresh token")
    missing = p.missing_scopes(token.scopes)
    if missing:
        # The consent screen let the person untick a permission canopy needs.
        record(subject, "provider_error", by=by, warn=True, during="connect", error="insufficient_scope",
               missing=" ".join(missing))
        _discard_grant(p, subject, token)
        raise ConnectionError("insufficient_scope", f"{p.title} did not grant {', '.join(missing)}")
    try:
        p.check_owner(ident, owner_kind=parked["owner_kind"], owner=owner_obj)
    except providers.ProviderError as e:
        code = "wrong_account" if e.code == "wrong_account" else "provider_error"
        record(subject, "provider_error", by=by, warn=True, during="connect", error=code)
        _discard_grant(p, subject, token)
        raise ConnectionError(code, str(e)) from None

    lookup = {"app": app, "provider": provider, "account_id": ident.account_id, **owner_kw}
    defaults = {
        "account_label": ident.label,
        "instance_url": token.instance_url,
        "scopes": " ".join(token.scopes or p.requested_scopes()),
        "refresh_token_enc": encrypt_secret(token.refresh_token),
        "access_token_enc": encrypt_secret(token.access_token),
        "access_token_expires_at": token.expires_at,
        "access_token_seconds": int(token.lifetime.total_seconds()) or None,
        "refresh_failed_at": None,
        "connected_by": by,
    }
    with transaction.atomic():
        # The app row, locked, is still the client that issued this grant: a client
        # id change or a removal that committed during the exchange refuses it
        # (and either one, waiting on this lock, sees the new connection).
        changed = not ConnectionApp.objects.select_for_update().filter(
            pk=app.pk, client_id=parked["client_id"]).exists()
        if not changed:
            conn = _upsert_connection(lookup, defaults)
    if changed:
        # Revoked outside the transaction: a network call never holds a row lock.
        _discard_grant(p, subject, token)
        raise ConnectionError("bad_state", "that app has changed; start the connection again")
    record(Subject.of(conn), "connected", by=by,
           summary=f"{conn.owner_label} connected {p.title} as {conn.account_label}")
    return conn


def _upsert_connection(lookup: dict, defaults: dict) -> Connection:
    """Insert or update the grant's row; the caller holds the transaction."""
    try:
        with transaction.atomic():
            conn, _ = Connection.objects.update_or_create(**lookup, defaults=defaults)
    except IntegrityError:
        # A concurrent completion for the same account inserted the row between
        # our lookup and insert. It exists now: update it with this grant.
        conn = Connection.objects.select_for_update().get(**lookup)
        for k, v in defaults.items():
            setattr(conn, k, v)
        conn.save()
    return conn


def _error_code(e: providers.ProviderError) -> str:
    return e.code or ("provider_unavailable" if isinstance(e, providers.ProviderUnavailable) else "provider_error")


def _discard_grant(p: providers.Provider, subject: Subject, token: providers.TokenResponse) -> None:
    """Revoke a grant the provider issued but canopy refused — its refresh token,
    else its access token — best effort: a failure is a `revoke_failed` record,
    never raised, so the refusal the caller is about to raise is what surfaces."""
    secret, hint = ((token.refresh_token, "refresh_token") if token.refresh_token
                    else (token.access_token, "access_token"))
    try:
        p.revoke(secret, hint=hint)
    except Exception as e:  # noqa: BLE001
        record(subject, "revoke_failed", warn=True, during="connect",
               error=getattr(e, "code", "") or type(e).__name__)


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


def _handout(conn: Connection, token: str, *, refreshed: bool) -> AccessToken:
    return AccessToken(token=token, instance_url=conn.instance_url, expires_at=conn.access_token_expires_at,
                       account_label=conn.account_label, refreshed=refreshed)


def access_token_for(conn: Connection, *, min_life: timedelta = ACCESS_TOKEN_MARGIN,
                     refresh: bool = False) -> AccessToken:
    """A usable access token: the cached one while it has at least `min_life` left,
    else refreshed under the row lock. Records nothing about the handout — the
    door that hands it out calls `issue_token`, which does.

    `min_life` is what the caller needs (a runner that re-stages every few minutes
    asks for more than the default margin). It is capped at half a fresh token's
    lifetime: asking for more than a fresh token has would refresh on every call.
    `refresh` skips the cache entirely — for a caller whose token the provider
    already rejected (a 401 before the assumed expiry); it still refreshes under
    the lock, so it cannot race another refresh.
    """
    conn = Connection.objects.select_related("app__workspace").get(pk=conn.pk)
    # Half the life the last token actually had (the provider's `expires_in`), else
    # half the app's configured lifetime.
    lifetime = (timedelta(seconds=conn.access_token_seconds) if conn.access_token_seconds
                else providers.for_app(conn.app).access_token_lifetime())
    min_life = min(min_life, lifetime / 2)
    if conn.needs_reconnect:
        raise ReconnectRequired(f"reconnect {conn.provider}")
    cached = "" if refresh else _cached_token(conn, min_life)
    if cached:
        conn.last_used_at = timezone.now()
        Connection.objects.filter(pk=conn.pk).update(last_used_at=conn.last_used_at)
        return _handout(conn, cached, refreshed=False)

    p = _configured(conn.app)  # checked before the lock; rebuilt from the locked read below
    rejected: providers.ProviderError | None = None
    failed: tuple[str, providers.ProviderError] | None = None
    with transaction.atomic():
        # Re-read under the lock: another process may have refreshed (or had its
        # refresh rejected) while this one waited.
        conn = Connection.objects.select_for_update(of=("self",)).select_related("app__workspace").get(pk=conn.pk)
        # The app as it is now: a secret rotated while this waited is the one to send.
        p = _configured(conn.app)
        if conn.needs_reconnect:
            raise ReconnectRequired(f"reconnect {conn.provider}")
        cached = "" if refresh else _cached_token(conn, min_life)
        if cached:
            conn.last_used_at = timezone.now()
            conn.save(update_fields=["last_used_at", "updated_at"])
            return _handout(conn, cached, refreshed=False)

        try:
            token = p.refresh(decrypt_secret(conn.refresh_token_enc))
        except providers.ProviderError as e:
            if isinstance(e, providers.ProviderUnavailable) or e.code != "invalid_grant":
                # Not about the grant (unreachable; our client config; an unexpected
                # answer): reconnecting would not fix it, so nothing is stamped.
                code = "provider_unavailable" if isinstance(e, providers.ProviderUnavailable) else "provider_error"
                failed = (code, e)
            else:
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
            if token.scopes:  # the provider said what it granted this time
                conn.scopes = " ".join(token.scopes)
            conn.access_token_enc = encrypt_secret(token.access_token)
            conn.access_token_expires_at = token.expires_at
            conn.access_token_seconds = int(token.lifetime.total_seconds()) or None
            conn.refresh_failed_at = None
            conn.last_used_at = timezone.now()
            conn.save(update_fields=[
                "refresh_token_enc", "instance_url", "scopes", "access_token_enc", "access_token_expires_at",
                "access_token_seconds", "refresh_failed_at", "last_used_at", "updated_at",
            ])
            return _handout(conn, token.access_token, refreshed=True)

    # Records are written after the transaction, never inside it: an Event written
    # in the block would share its fate, and a record is about what committed.
    if failed is not None:
        code, e = failed
        record(Subject.of(conn), "provider_error", warn=True, during="refresh", error=e.code or code)
        raise ConnectionError(code, str(e))
    record(Subject.of(conn), "reconnect_required", warn=True, error=rejected.code,
           summary=f"{conn.owner_label}'s {p.title} connection was rejected; reconnect it")
    raise ReconnectRequired(f"{p.title} rejected the stored grant ({rejected.code})")


def issue_token(conn: Connection, *, to: Literal["cli", "turn"], by=None,
                min_life: timedelta = ACCESS_TOKEN_MARGIN, refresh: bool = False,
                agent: str = "", turn: str = "", runner: str = "", runner_name: str = "") -> AccessToken:
    """`access_token_for`, plus the `token_issued` record of handing it out.

    The one way a token leaves canopy-web: both token routes come through here,
    so no handout goes unrecorded. `to` is `cli` (the owner's own command line;
    `by` is that person) or `turn` (named by `agent`, `turn` and `runner`; one
    turn's repeated issues, its heartbeat re-stages, coalesce into one Event;
    `runner_name` is for the summary only)."""
    token = access_token_for(conn, min_life=min_life, refresh=refresh)
    title = getattr(providers.CLASSES.get(conn.provider), "title", conn.provider)
    context = {"agent": agent, "turn": turn, "runner": runner} if to == "turn" else {}
    record(Subject.of(conn), "token_issued", by=by, to=to, refreshed=token.refreshed, **context,
           key=f"token_issued:{conn.provider}:{agent}:{turn}" if to == "turn" else "",
           summary=(f"{conn.owner_label}'s {title} token for {agent}'s turn on {runner_name or runner}"
                    if to == "turn" else f"{conn.owner_label}'s {title} token for their CLI"))
    return token


def disconnect(conn: Connection, *, by) -> None:
    """Revoke at the provider with the connection's app (best effort) and delete the row.

    A failed revoke is logged, not raised: the person asked for canopy to stop
    holding the grant, and deleting it does that whatever the provider says.
    """
    subject = Subject.of(conn)
    grantees = _grantees(conn)
    _revoke(conn.app, subject, _refresh_token_of(conn, subject))
    conn.delete()  # its grants go with it (CASCADE)
    summary = f"{subject.owner} disconnected {subject.provider} ({subject.account_label})"
    record(subject, "disconnected", by=by, summary=summary)
    # A moved agent's connection belongs to the app's workspace as much as the
    # agent's: the admins removing that app see it go too.
    app_ws = conn.app.workspace
    if subject.workspace is not None and subject.workspace.pk != app_ws.pk:
        record(dataclasses.replace(subject, workspace=app_ws), "disconnected", by=by, summary=summary)
    for grantee in grantees:
        _record_grant_revoked(subject, grantee, by=by, during="disconnected")


def _decrypted_refresh(conn: Connection) -> str | None:
    """The plaintext refresh token, or None when it cannot be read."""
    try:
        return decrypt_secret(conn.refresh_token_enc)
    except Exception:  # noqa: BLE001 - an unreadable token cannot be revoked; still delete
        return None


def _refresh_token_of(conn: Connection, subject: Subject) -> str:
    """The plaintext refresh token, or "" (recorded) when it cannot be read."""
    refresh = _decrypted_refresh(conn)
    if refresh is None:
        record(subject, "revoke_failed", warn=True, error="undecryptable")
        return ""
    return refresh


def _revoke(app: ConnectionApp, subject: Subject, refresh: str) -> None:
    """Revoke `refresh` with `app`'s client, best effort: a failure is a log line."""
    if not refresh:
        return
    try:
        providers.for_app(app).revoke(refresh)
    except Exception as e:  # noqa: BLE001
        record(subject, "revoke_failed", warn=True, error=getattr(e, "code", "") or type(e).__name__)


def _grantees(conn: Connection) -> list:
    """The agents `conn` is shared with, read before a delete takes the grants."""
    if conn.agent_id is None:
        return []
    return [g.agent for g in conn.grants.select_related("agent__workspace").order_by("agent__slug")]


def _record_grant_revoked(subject: Subject, grantee, *, by, **context) -> None:
    """`grant_revoked` for one grantee: in the owner's workspace (whose connection
    it is) and, when the grantee lives elsewhere, in the grantee's too, so the
    workspace whose turns just lost it can see why."""
    summary = f"{subject.owner}'s {subject.provider} no longer granted to {grantee.slug}"
    record(subject, "grant_revoked", by=by, grantee=grantee.slug, summary=summary, **context)
    if subject.workspace is not None and grantee.workspace_id != subject.workspace.pk:
        record(dataclasses.replace(subject, workspace=grantee.workspace), "grant_revoked", by=by,
               grantee=grantee.slug, summary=summary, **context)


# ---- which connection an agent uses; grants ----------------------------------

def agent_connections_usable() -> dict:
    """The filter for agent-owned connections that still work: the owning agent is
    in the workspace whose app made it. THE rule for an agent at use time (connect
    already holds it; a move breaks it): a moved agent's old connection, and every
    grant of it, stop — never run on the old workspace's OAuth client unseen."""
    return {"agent__isnull": False, "agent__workspace_id": F("app__workspace_id")}


def agent_connection_usable(conn: Connection) -> bool:
    """`agent_connections_usable` for one row already loaded (with `agent`, `app`)."""
    return conn.agent_id is not None and conn.agent.workspace_id == conn.app.workspace_id


def connection_for_agent(agent, provider: str) -> Connection:
    """The agent's own connection for `provider`, else one granted to it — each
    only while usable (`agent_connections_usable`).

    Never a person's connection. More than one candidate at the level that
    answers is `ambiguous` rather than a guess.
    """
    usable = Connection.objects.filter(provider=provider, **agent_connections_usable())
    own = list(usable.filter(agent=agent)[:2])
    if len(own) == 1:
        return own[0]
    if len(own) > 1:
        raise ConnectionError("ambiguous", f"{agent.slug} holds more than one {provider} connection")
    granted = list(usable.filter(grants__agent=agent).distinct()[:2])
    if len(granted) == 1:
        return granted[0]
    if len(granted) > 1:
        raise ConnectionError("ambiguous", f"{agent.slug} is granted more than one {provider} connection")
    raise NotConnected(f"{agent.slug} has no {provider} connection")


def person_connections(user, provider: str, *, workspace: str | None = None) -> list[Connection]:
    """The person's own connections for `provider` that are usable: made with an
    app in a workspace they can still read (and in `workspace`, when given)."""
    readable = readable_workspaces(user)
    if workspace is not None:
        readable &= {workspace}
    return list(connections_for(("user", user.pk)).filter(provider=provider, app__workspace_id__in=readable)
                .select_related("app__workspace"))


def connection_for_person(user, provider: str, *, workspace: str | None = None) -> Connection:
    """The person's own connection for `provider` — never an agent's — made with
    an app in a workspace they can still read: someone who left a workspace no
    longer gets a token from its app (`not_connected`). More than one is
    `ambiguous` (a token route returns one token, not a guess) unless
    `workspace` narrows it to one."""
    rows = person_connections(user, provider, workspace=workspace)
    if not rows:
        raise NotConnected(f"user {user.pk} has no {provider} connection")
    if len(rows) > 1:
        raise ConnectionError("ambiguous", f"user {user.pk} holds more than one {provider} connection")
    return rows[0]


def providers_for_agent(agent) -> list[str]:
    """The providers `agent` has a usable connection for — its own or one granted
    to it — sorted. What a runner stages for a turn; never a person's connection."""
    usable = Connection.objects.filter(**agent_connections_usable())
    own = usable.filter(agent=agent).values_list("provider", flat=True)
    granted = usable.filter(grants__agent=agent).values_list("provider", flat=True)
    # A row for a provider no longer registered here has nothing to stage.
    return sorted(p for p in set(own) | set(granted) if p in providers.CLASSES)


def grant(conn: Connection, agent, *, by) -> ConnectionGrant:
    """Let `agent` use another agent's connection. Idempotent."""
    if conn.agent_id is None:
        raise ConnectionError("not_grantable", "a person's connection is never granted to an agent")
    if conn.agent_id == agent.pk:
        raise ConnectionError("own_connection", "an agent already uses its own connection")
    if not (conn.agent.is_admin(by) and agent.is_admin(by)):
        raise PermissionDenied("granting a connection needs admin of both agents")
    if not agent_connection_usable(conn):
        # A moved owner's connection is dead; a grant of it would be too.
        raise ConnectionError("not_usable", f"{conn.agent.slug} has moved out of the workspace that made "
                                            "this connection; connect it again there first")
    g, created = ConnectionGrant.objects.get_or_create(
        connection=conn, agent=agent, defaults={"granted_by": by},
    )
    if created:
        record(Subject.of(conn), "granted", by=by, grantee=agent.slug,
               summary=f"{conn.owner_label}'s {conn.provider} granted to {agent.slug}")
    return g


def revoke_grant(g: ConnectionGrant, *, by) -> None:
    """Stop `g.agent` using the connection. Admin of EITHER agent may: taking
    access away should never need more standing than either side has."""
    conn, agent = g.connection, g.agent
    if not (conn.agent.is_admin(by) or agent.is_admin(by)):
        raise PermissionDenied("revoking a grant needs admin of either agent")
    g.delete()
    _record_grant_revoked(Subject.of(conn), agent, by=by)

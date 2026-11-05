"""Connect and disconnect — the one implementation every surface calls. Lending
(a signed grant statement) is `sharing.py`; who may open a connection is `recipients.py`.

Request-free, like `apps.github_connect.github_app`: takes a `session` mapping
where OAuth state must live between the two legs, raises domain exceptions, and
does no HTTP-framework work. The settings views (person and agent) and the
runner routes all come through here, so they cannot disagree about what a
usable connection is. Nothing here names a provider: what differs between
providers lives in `providers/`.

A CONNECTION IS SEALED, AND canopy-web STORES NOTHING READABLE. Here the only plaintext token is
the one in hand during the callback's code exchange. Machines the owner allows refresh and revoke,
with the app's client; and, only for a connection sealed to its own KMS key (the `server_use`
switch), so does canopy-web, in `server_tokens`.

CONNECTING SEALS TO THE OWNER. `complete` exchanges the code, runs every policy
check, then seals the refresh token (the `refresh` plaintext of
`docs/vault/zk-v1/connections_v1_reference.py`) to the owner's member vault key
only — the person's, or for an agent the admin connecting it (`owner_keyset`) —
drops the access token and keeps no readable copy. The row is `unshared` until
the owner's browser signs a grant statement and re-seals it to the machines
that statement allows. Without a vault key there is nothing to seal to:
`no_vault_key`, before anything is exchanged. When `server_use` is on (a new row:
the provider's default, if canopy-web's key can be read) it is sealed to
canopy-web's key too (`server_key`, spec "Connecting"); a KMS failure never costs the
sign-in, which then seals to the owner alone. A connect naming limits (`lend_minutes`,
`owner_turns_only`) is a person lending narrowly: it is sealed to the owner alone
whatever the provider's default, and turns an on row off.

THE APP. Every flow runs against one workspace's `ConnectionApp` (`app_for`):
its client id and secret, its options, its chosen scopes. A connection keeps the
app that minted it, and machines refresh and revoke it with that app's client,
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
import datetime as dt
import hashlib
import json
import logging
import re
import secrets as _secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.db.models import F, QuerySet
from django.utils import timezone

from apps.agents.credentials import zk_seal
from apps.agents.models import Agent
from apps.events import services as events
from apps.harness import device_keys
from apps.harness import initiator as who
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces import permissions as perms
from apps.workspaces import services as workspaces

from . import declarations, providers, server_key, wire
from . import recipients as rcpt
from .models import Connection, ConnectionApp, ConnectionSurfaceShare

logger = logging.getLogger(__name__)

Owner = tuple[Literal["user", "agent"], int]

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
    `not_configured`, `not_connected`, `ambiguous`,
    `insufficient_scope`, `provider_error`, `provider_unavailable`, `bad_state`,
    `no_vault_key` (the
    owner has no vault key to seal a connection to), `owner_changed` (the account's
    row is sealed to another vault key, and a row's key never changes: disconnect it
    first), `app_locked` (an app's client id, or an
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


def _owner_filter(owner: Owner) -> dict:
    kind, pk = owner
    if kind == "agent":
        return {"agent_id": pk}
    if kind == "user":
        return {"user_id": pk}
    raise ValueError(f"unknown owner kind {kind!r}")


# Where a person sets up their vault key (`apps.agents.setup` points there too).
VAULT_KEY_SETUP = "/settings#vault-key"


def owner_keyset(owner_kind: str, owner, by) -> UserVaultKeyset:
    """The member vault key a new connection is sealed to: the person's own, or for
    an agent's the admin connecting it (`by`). `no_vault_key` when they have none:
    there is nothing to seal to, and no fallback."""
    user = by if owner_kind == "agent" else owner
    keyset = (UserVaultKeyset.objects.filter(user_id=getattr(user, "pk", None)).first()
              if user is not None else None)
    if keyset is None:
        raise ConnectionError("no_vault_key", "Set up your vault key first (Settings → Security): a connection "
                                              "is sealed to it, and canopy-web keeps no copy it can read.")
    return keyset


def connections_for(owner: Owner) -> QuerySet[Connection]:
    """The owner's connections, as listed: never one being revoked (it left every
    list when it was disconnected)."""
    return (Connection.objects.filter(**_owner_filter(owner)).exclude(state=Connection.REVOKING)
            .order_by("provider", "account_label", "pk"))


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
           .filter(workspace_id=_slug(workspace), provider=provider, removed_at__isnull=True).first())
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
            .filter(provider=provider, workspace_id__in=readable_workspaces(user), removed_at__isnull=True)
            .order_by("workspace_id"))
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
    # Set up again after a removal: back in use, with the connections still being
    # revoked through it (their client cannot change: `app_locked` above).
    app.removed_at = None
    app.options = cleaned
    app.scopes = " ".join(s for s in cls.scope_choices if s in chosen)
    app.configured_by = by
    app.save()
    return app


def remove_app(app: ConnectionApp, *, by) -> None:
    """Remove `app`: hide it at once (`removed_at`: no new connect, not listed, not
    set up), then disconnect every connection made with it (each recorded as
    `disconnected` by `by`). Rows a cloud box holds are `revoking` and still need
    the app's client to be revoked with, so the app stays (hidden) until they are
    gone and the sweep (`keeper.sweep`) deletes it (R12); with none left it is
    deleted here. Records `app_removed` once.

    `removed_at` is set under the app's lock first: a connect that completes after
    that is refused (`complete` re-checks it under the same lock), and one that
    committed before is among the rows disconnected next."""
    with transaction.atomic():
        locked = ConnectionApp.objects.select_for_update().filter(pk=app.pk).first()
        if locked is None:
            return
        newly = locked.removed_at is None
        if newly:
            locked.removed_at = timezone.now()
            locked.save(update_fields=["removed_at", "updated_at"])
    for conn in list(Connection.objects.filter(app_id=app.pk).exclude(state=Connection.REVOKING)
                     .select_related("app__workspace", "agent__workspace").order_by("pk")):
        # Not one provider call per connection in an admin's request: the sweep revokes them.
        disconnect(conn, by=by, revoke_now=False)
    delete_removed_app(app)
    if newly:
        title = getattr(providers.CLASSES.get(app.provider), "title", app.provider)
        record(_app_subject(app), "app_removed", by=by, workspace=app.workspace_id, client_id=app.client_id,
               summary=f"{_who(by) or 'someone'} removed {title} from {app.workspace_id}")


def delete_removed_app(app: ConnectionApp) -> bool:
    """Delete `app` if it was removed and no connection made with it remains (R12).
    Under the app's lock, so a disconnect that leaves a `revoking` row behind is
    seen. True when it was deleted."""
    with transaction.atomic():
        locked = ConnectionApp.objects.select_for_update().filter(pk=app.pk, removed_at__isnull=False).first()
        if locked is None or Connection.objects.filter(app_id=app.pk).exists():
            return False
        locked.delete()
    return True


# ---- the audit trail and the log: one vocabulary ---------------------------------

# Verbs that are the owner's history: an Event for an agent's connection, a log
# line for a person's. The rest are canopy's operational trouble: always a log line.
AUDITED = frozenset({"connected", "disconnected", "shared", "granted", "grant_revoked", "token_issued",
                     "refreshed", "server_refreshed", "server_write_failed", "server_use_on", "server_use_off",
                     "lease_expired_unwritten",
                     "reconnect_required", "resealed", "revoked", "revoke_unconfirmed", "app_configured",
                     "app_removed", "connection_call", "surface_shared", "surface_unshared"})


@dataclass(frozen=True)
class Subject:
    """What a record says about a connection, captured while the row exists (a
    disconnect records after deleting it). Never a token."""

    connection: str | None  # the row's uuid; None before a row exists (a connect that failed), and for an app
    provider: str
    owner: str  # "agent:<slug>" | "user:<id>"; "" for an app
    account_label: str
    workspace: Any  # the owning agent's (or the app's) Workspace, None for a person's connection

    @classmethod
    def of(cls, conn: Connection) -> Subject:
        agent = conn.agent if conn.agent_id is not None else None
        return cls(connection=str(conn.pk), provider=conn.provider,
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


def begin(session, *, app: ConnectionApp, owner: Owner, redirect_uri: str, return_to: str = "",
          lend_minutes: int | None = None, owner_turns_only: bool = False, for_agent: str = "",
          temporary: bool = False) -> str:
    """Park a PKCE verifier, a CSRF `state` and `app` in the session; return the
    authorize URL (with `app`'s client). `lend_minutes` and `owner_turns_only` are the
    limits the connection will carry (`complete`); `for_agent` is the slug the share
    screen ticks for the owner. `temporary` asks the provider for no refresh token, and
    the callback finishes it with `temporary.complete`, for the agent `for_agent` names.

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
                      "owner_kind": kind, "owner_id": owner_id, "verifier": verifier, "return_to": return_to,
                      "lend_minutes": lend_minutes, "owner_turns_only": owner_turns_only, "for_agent": for_agent,
                      "temporary": temporary}
    session[SESSION_KEY] = pending  # reassigned so a Django session marks itself modified
    owner_obj = (Agent if kind == "agent" else get_user_model()).objects.filter(pk=owner_id).first()
    return p.authorize_url(state, redirect_uri, challenge, owner_kind=kind, owner=owner_obj, temporary=temporary)


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


def take_parked(session, *, provider: str, workspace, state: str) -> tuple[dict, ConnectionApp]:
    """Spend the flow parked under `state`, and return it with its app. `bad_state` for a callback of
    another provider or workspace than it was begun with, or an app no longer the one it began with."""
    pending = dict(session.get(SESSION_KEY) or {})
    parked = pending.pop(state, None) if state else None
    session[SESSION_KEY] = pending
    if not parked or parked.get("provider") != provider or parked.get("workspace") != _slug(workspace):
        raise ConnectionError("bad_state", "start the connection again")
    app = (ConnectionApp.objects.select_related("workspace")
           .filter(pk=parked.get("app"), provider=provider, workspace_id=parked["workspace"],
                   removed_at__isnull=True).first())
    # Removed, or given another client, between begin and complete: the code was
    # issued to a client this app no longer is.
    if app is None or app.client_id != parked.get("client_id"):
        raise ConnectionError("bad_state", "that app has changed; start the connection again")
    return parked, app


def complete(session, *, provider: str, workspace, code: str, state: str, redirect_uri: str, by) -> Connection:
    """Exchange `code` with the parked app, check who the account is, and seal its
    refresh token to the owner.

    The state is popped first, so a failed exchange still spends it. A callback
    for another provider or workspace than the flow was begun with is
    `bad_state`, and nothing is exchanged; so is an owner with no vault key
    (`no_vault_key`). Nothing is stored unless the exchange, the identity lookup,
    the required scopes, the provider's `check_owner` and its `sealed_hosts`
    succeed; a grant refused after the exchange is discarded (`_discard_grant`). On
    success the refresh plaintext is sealed to the owner's member key only
    (`owner_keyset`), and to canopy-web's key when the switch is on
    (`_server_key_to_seal_to`), the access token is dropped, and the row (inserted, or the
    same account's row for this owner and app, replaced) is `unshared`; records
    `connected`. A connect naming limits starts a new row off and turns an on row off (recorded
    `server_use_off`), so it is sealed to the owner alone and KMS is not asked; one naming none
    leaves an existing row's switch as its owner chose. An existing row sealed to another key is left as it is
    (`owner_changed`): boxes pin a connection's owner key, and only that pin catches
    a self-consistent forgery, so it never changes.
    """
    parked, app = take_parked(session, provider=provider, workspace=workspace, state=state)
    p = _configured(app)
    if parked["owner_kind"] == "agent":
        agent = Agent.objects.filter(pk=parked["owner_id"]).first()
        if agent is None:  # deleted between begin and complete
            raise ConnectionError("bad_state", "that agent no longer exists")
        owner_obj, owner_kw = agent, {"agent": agent}
    else:
        owner_obj = get_user_model().objects.filter(pk=parked["owner_id"]).first()
        owner_kw = {"user": owner_obj}
    # Checked before the exchange: without it there is nothing to seal to, and a
    # code never exchanged leaves no grant behind.
    keyset = owner_keyset(parked["owner_kind"], owner_obj, by)
    # No row yet: a refusal is logged against the would-be owner.
    subject = _parked_subject(provider, parked)
    try:
        token = p.exchange_code(code, redirect_uri, parked["verifier"])
    except providers.ProviderError as e:
        record(subject, "provider_error", by=by, warn=True, during="connect", error=_error_code(e))
        raise ConnectionError("provider_error", str(e)) from None
    # From here the provider has issued a grant: every refusal discards it, so a
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
    try:
        login_host, instance_url = p.sealed_hosts(token)
    except providers.ProviderError as e:
        # Every machine would refuse it, so it could never be used or even revoked.
        record(subject, "provider_error", by=by, warn=True, during="connect", error=_error_code(e), reason=str(e))
        _discard_grant(p, subject, token)
        raise ConnectionError("provider_error", str(e)) from None

    scopes = " ".join(token.scopes or p.requested_scopes())
    owner_key = device_keys.thumbprint(keyset.public_jwk)
    plaintext = refresh_plaintext(app, token, account_id=ident.account_id, owner_key=owner_key, scopes=scopes,
                                  login_host=login_host, instance_url=instance_url)
    lookup = {"app": app, "provider": provider, "account_id": ident.account_id, **owner_kw}
    defaults = {
        "account_label": ident.label,
        "instance_url": instance_url,
        "scopes": scopes,
        "state": Connection.UNSHARED,
        # A first seal carries no writer MACs: no machine reads a row until the owner shares it, and
        # only the owner's browser opens this one.
        "sealed_sig": None,
        "access_sealed": "",
        "access_sig": None,
        "access_expires_at": None,
        "owner_key": owner_key,
        "owner_public_jwk": keyset.public_jwk,
        "owner_user_id": str(keyset.user_id),
        "signer": keyset.user,
        "refresh_failed_at": None,
        "connected_by": by,
        # From the moment it connected, and replaced with everything else on a reconnect: a
        # connect that names no limits makes a standing connection.
        "expires_at": (timezone.now() + dt.timedelta(minutes=parked["lend_minutes"])
                       if parked.get("lend_minutes") else None),
        "owner_turns_only": bool(parked.get("owner_turns_only")),
    }

    # A lend with limits is the owner's alone, so the switch can't stay on or start on.
    limited = bool(defaults["expires_at"] or defaults["owner_turns_only"])
    # Read now: the transaction below holds row locks, and a network call never does.
    server_jwk = None if limited else _server_key_to_seal_to(p, lookup, subject, by)
    turned_off = False

    def seal(connection_id, server_use) -> dict:
        """The envelope fields for the row. `server_use`: the locked row's switch, None for a
        new row, whose own is decided here and returned with them. An existing row's is returned
        only when limits turn it off: a reconnect naming none keeps the owner's choice. Its key
        failing this time seals to the owner only and leaves an on row on, as the owner chose;
        the next reconnect or share reaches canopy-web again."""
        nonlocal turned_off
        new = server_use is None
        on = False if limited else (p.server_use_default and server_jwk is not None) if new else server_use
        turned_off = bool(server_use) and not on
        to = [("member", str(keyset.user_id), keyset.public_jwk)]
        if on and server_jwk is not None:
            to.append(("device", device_keys.thumbprint(server_jwk), server_jwk))
        sealed = zk_seal.seal_to(plaintext, agent_id=wire.binding_id(connection_id), name=wire.REFRESH_NAME,
                                 kv=0, recipients=to)
        return {"sealed": sealed, "sealed_digest": hashlib.sha256(sealed.encode()).hexdigest(),
                **({"server_use": on} if new or limited else {})}

    with transaction.atomic():
        # The app row, locked, is still the client that issued this grant: a client
        # id change or a removal that committed during the exchange refuses it
        # (and either one, waiting on this lock, sees the new connection).
        changed = not ConnectionApp.objects.select_for_update().filter(
            pk=app.pk, client_id=parked["client_id"], removed_at__isnull=True).exists()
        # The same account's row being revoked is never resurrected: replacing its
        # envelope would orphan the refresh token its holder is about to revoke.
        # Decided on the row `_upsert_connection` locks, so a disconnect that
        # commits during the exchange is seen.
        revoking = owner_changed = False
        if not changed:
            try:
                conn = _upsert_connection(lookup, defaults, seal)
            except _RowRevoking:
                revoking = True
            except _OwnerChanged:
                owner_changed = True
    if changed or revoking or owner_changed:
        # Discarded outside the transaction: a network call never holds a row lock.
        _discard_grant(p, subject, token)
        if revoking:
            raise ConnectionError("revoking", f"That {p.title} account is still being disconnected; connect it "
                                              "again once that finishes.")
        if owner_changed:
            raise ConnectionError("owner_changed", f"That {p.title} account is connected with another vault key, "
                                                   "which never changes. Disconnect it first, then connect again.")
        raise ConnectionError("bad_state", "that app has changed; start the connection again")
    record(Subject.of(conn), "connected", by=by,
           summary=f"{conn.owner_label} connected {p.title} as {conn.account_label}")
    if turned_off:
        record(Subject.of(conn), "server_use_off", by=by,
               summary=f"{conn.owner_label} lent {p.title} ({conn.account_label}) with limits, which turned off "
                       "canopy-web's use of it")
    return conn


def _server_key_to_seal_to(p: providers.Provider, lookup: dict, subject: Subject, by) -> dict | None:
    """canopy-web's public key, if this connect may seal to it: a new row the provider turns on
    by default, or the same account's row already on. None otherwise, when no key is configured,
    and when KMS fails: that is a warning, never a failed sign-in. Google's connect asks KMS nothing."""
    if not server_key.available():
        return None
    if not p.server_use_default and not Connection.objects.filter(**lookup, server_use=True).exists():
        return None
    try:
        return server_key.public_jwk()
    except server_key.Unavailable:
        record(subject, "server_key_unavailable", by=by, warn=True, during="connect")
        return None


def refresh_plaintext(app: ConnectionApp, token: providers.TokenResponse, *, account_id: str,
                      owner_key: str, scopes: str, login_host: str, instance_url: str) -> str:
    """The `refresh` plaintext (contract §0.2), compact JSON. Never the client secret:
    a machine gets that from the app with the material. `login_host` and `instance_url`
    are the provider's `sealed_hosts`. `statement_floor` is 0: no statement exists yet,
    and the owner's browser raises it to the statement's version at each share."""
    return json.dumps({
        "v": 1, "provider": app.provider, "refresh_token": token.refresh_token, "client_id": app.client_id,
        "login_host": login_host,
        "instance_url": instance_url, "account_id": account_id, "owner_key": owner_key,
        "issued_at": int(time.time()), "scopes": scopes, "statement_floor": 0,
    }, separators=(",", ":"))


class _RowRevoking(Exception):  # noqa: N818 - a signal inside `complete`, never raised out of it
    """The row `_upsert_connection` locked is being revoked: refuse, change nothing."""


class _OwnerChanged(Exception):  # noqa: N818 - a signal inside `complete`, never raised out of it
    """The row `_upsert_connection` locked is sealed to another key: refuse, change nothing."""


def _upsert_connection(lookup: dict, defaults: dict, seal) -> Connection:
    """Insert or update the grant's row with its envelope; the caller holds the
    transaction. `seal(connection_id, server_use)` gives the envelope fields: the binding
    is the row's id, so the row is always sealed to the id it ends up with — its own when
    the same owner, app and account already has one, else the id it is created with. It is
    told the locked row's `server_use` (None for a new row, whose fields then carry one, as
    they do when limits turn an existing row off), and `defaults` never holds it, so a
    reconnect does not reset the owner's choice.
    Raises, writing nothing, `_RowRevoking` when the row it locks is `revoking` and
    `_OwnerChanged` when its `owner_key` is not the key about to be sealed to."""
    conn = Connection.objects.select_for_update().filter(**lookup).first()
    if conn is None:
        pk = uuid.uuid4()
        try:
            with transaction.atomic():
                return Connection.objects.create(id=pk, **lookup, **defaults, **seal(pk, None))
        except IntegrityError:
            # A concurrent completion for the same account inserted the row between
            # our lookup and insert. It exists now: update it with this grant,
            # sealed to ITS binding.
            conn = Connection.objects.select_for_update().get(**lookup)
    if conn.state == Connection.REVOKING:
        raise _RowRevoking
    if conn.owner_key != defaults["owner_key"]:
        raise _OwnerChanged
    for k, v in {**defaults, **seal(conn.pk, conn.server_use)}.items():
        setattr(conn, k, v)
    conn.save()
    return conn


def _error_code(e: providers.ProviderError) -> str:
    return e.code or ("provider_unavailable" if isinstance(e, providers.ProviderUnavailable) else "provider_error")


def _discard_grant(p: providers.Provider, subject: Subject, token: providers.TokenResponse) -> None:
    """Drop a grant the provider issued but canopy refused. Revoked at the provider
    — its refresh token, else its access token — unless the provider says not to
    (`revokes_refused_grant`; Google, where a revoke ends every token of that
    account on the client). Best effort: a failure is a `revoke_failed` record,
    never raised, so the refusal the caller is about to raise is what surfaces."""
    if not p.revokes_refused_grant:
        return
    secret, hint = ((token.refresh_token, "refresh_token") if token.refresh_token
                    else (token.access_token, "access_token"))
    try:
        p.revoke(secret, hint=hint)
    except Exception as e:  # noqa: BLE001
        record(subject, "revoke_failed", warn=True, during="connect",
               error=getattr(e, "code", "") or type(e).__name__)


# ---- disconnecting -----------------------------------------------------------

def disconnect(conn: Connection, *, by, revoke_now: bool = True, lapsed: bool = False,
               unfinished: bool = False) -> Literal["revoking", "deleted", "revoked"]:
    """Disconnect `conn`, and say which of the outcomes below happened (spec §4.5).

    When canopy-web's own key can open it (`server_tokens.may_revoke`) or a cloud box still heard
    from holds it (`recipients.revokers`; only cloud runners revoke), the row becomes `revoking` at
    once: it leaves every list, is handed to no machine, and its holders are told to revoke it
    (`keeper.work`) — at the provider only when no other connection of that account through that
    app remains (`revoke_at_provider`: revoking at Google ends every token of the account on the
    client). canopy-web then revokes it itself, after the commit (`server_tokens.try_revoke`),
    finishing as a holder's confirmation does. This answers `revoked` when that ended it at the
    provider, and `deleted` when the row went without doing so (another connection of the account
    needs the sign-in, or a revoke that can never work); when it can't, the row stays
    `revoking`, and the sweep retries while any holder may confirm first. The sweep deletes one
    nobody finishes. When neither can (never shared and not sealed to canopy-web, only laptops, or
    every holder stopped), nothing is there to revoke it, so it is deleted now and recorded
    `revoke_unconfirmed` (R7): the provider's own idle expiry finishes it.

    `revoke_now` false: the caller is in a transaction, where a network call must not be, or is
    disconnecting many at once, so canopy-web's revoke waits for the sweep. A failed revoke never raises
    out of here. `lapsed`: nobody asked, its time ran out; only the sweep says so, and it passes
    `revoke_now` false too, then retries the revoke itself (`keeper._server_revokes`). `unfinished`:
    an agent's connect nobody completed; only the sweep says so, in the same way.

    Records `disconnected` (in the app's workspace too, for a moved agent's) and
    `grant_revoked` for every agent it was lent to. Disconnecting a row already
    `revoking` changes nothing."""
    from . import server_tokens  # noqa: PLC0415 — it builds on this module

    if revoke_now:  # under the row lock only the cache is read
        rcpt.warm_server_key(conn)
    with transaction.atomic():
        row = (Connection.objects.select_for_update(of=("self",)).select_related("app__workspace", "agent__workspace")
               .filter(pk=conn.pk).first())
        if row is None:
            return "deleted"
        if row.state == Connection.REVOKING:
            return "revoking"
        subject = Subject.of(row)
        grantees = _grantees(row)
        held = rcpt.revokers(row)
        ours = server_tokens.may_revoke(row)
        if held or ours:
            row.state, row.revoking_since = Connection.REVOKING, timezone.now()
            row.revoke_at_provider = not other_live_connections(row).exists()
            # A lease in flight ends here: its write-back is refused (`revoking`).
            row.lease_id, row.lease_holder, row.lease_expires_at = None, "", None
            row.save(update_fields=["state", "revoking_since", "revoke_at_provider", "lease_id", "lease_holder",
                                    "lease_expires_at", "updated_at"])
        else:
            row.delete()  # its grants go with it (CASCADE)
    summary = (f"{subject.owner}'s {subject.provider} ({subject.account_label}) ended: its time ran out" if lapsed
               else f"{subject.owner}'s {subject.provider} ({subject.account_label}) ended: the connect never finished"
               if unfinished else f"{subject.owner} disconnected {subject.provider} ({subject.account_label})")
    record(subject, "disconnected", by=by, summary=summary)
    # A moved agent's connection belongs to the app's workspace as much as the
    # agent's: the admins removing that app see it go too.
    app_ws = row.app.workspace
    if subject.workspace is not None and subject.workspace.pk != app_ws.pk:
        record(dataclasses.replace(subject, workspace=app_ws), "disconnected", by=by, summary=summary)
    for grantee in grantees:
        record_grant_revoked(subject, grantee, by=by, during="disconnected")
    if not held and not ours:
        record(subject, "revoke_unconfirmed", by=by, reason="no_holder",
               summary=f"{subject.owner}'s {subject.provider} ({subject.account_label}) deleted: no machine "
                       "holds it to revoke it")
        return "deleted"
    if ours and revoke_now:
        result = server_tokens.try_revoke(row)
        if result is not None:
            return "revoked" if result == server_tokens.REVOKED else "deleted"
    return "revoking"


def finish_revoking(conn: Connection, *, machine: str, revoked: bool, error: str = "", check=None) -> None:
    """Whoever held `conn` did what it could: delete the row and record it. `revoked`: it revoked
    at the provider, or was told not to because another connection of that account remains; else it
    tried and failed (`error`), the operational `revoke_failed`. `check(row)` may refuse by raising,
    under the row's lock. 409 `not_revoking` unless it is being disconnected. Gone already: nothing."""
    with transaction.atomic():
        row = (Connection.objects.select_for_update(of=("self",)).select_related("app", "agent__workspace", "user")
               .filter(pk=conn.pk).first())
        if row is None:  # another holder confirmed a moment ago
            return
        if row.state != Connection.REVOKING:
            raise wire.WireError(409, "not_revoking", "This connection is not being disconnected.")
        if check is not None:
            check(row)
        subject = Subject.of(row)
        title = getattr(providers.CLASSES.get(row.provider), "title", row.provider)
        at_provider = not other_live_connections(row).exists()
        row.delete()
    if revoked:
        record(subject, "revoked", machine=machine, at_provider=at_provider,
               summary=f"{subject.owner}'s {title} ({subject.account_label}) revoked by {machine}")
    else:
        record(subject, "revoke_failed", warn=True, during="disconnect", machine=machine,
               error=error if _ERROR_CODE.fullmatch(error or "") else "unrecognised")


def other_live_connections(conn: Connection) -> QuerySet[Connection]:
    """Other connections of `conn`'s account through `conn`'s app that are not being
    revoked: while one remains, revoking `conn` at the provider would end it too."""
    return (Connection.objects.filter(app_id=conn.app_id, account_id=conn.account_id)
            .exclude(pk=conn.pk).exclude(state=Connection.REVOKING))


def _grantees(conn: Connection) -> list:
    """The agents `conn` is shared with, read before a delete takes the grants."""
    return [g.agent for g in conn.grants.select_related("agent__workspace").order_by("agent__slug")]


def _record_for_grantee(subject: Subject, kind: str, grantee, *, by, summary: str, **context) -> None:
    """`kind` (`granted` | `grant_revoked`) for one grantee: in the owner's workspace
    (whose connection it is; a log line for a person's) and, when the grantee lives
    in another workspace, in the grantee's too, so the workspace whose turns just
    gained or lost the connection can see why. A person's connection lent to an
    agent therefore always leaves an Event in the agent's workspace."""
    record(subject, kind, by=by, grantee=grantee.slug, summary=summary, **context)
    if subject.workspace is None or grantee.workspace_id != subject.workspace.pk:
        record(dataclasses.replace(subject, workspace=grantee.workspace), kind, by=by,
               grantee=grantee.slug, summary=summary, **context)


def record_granted(subject: Subject, grantee, *, by, **context) -> None:
    _record_for_grantee(subject, "granted", grantee, by=by, **context,
                        summary=f"{subject.owner}'s {subject.provider} granted to {grantee.slug}")


def record_grant_revoked(subject: Subject, grantee, *, by, **context) -> None:
    _record_for_grantee(subject, "grant_revoked", grantee, by=by, **context,
                        summary=f"{subject.owner}'s {subject.provider} no longer granted to {grantee.slug}")


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


#: How an `owner_turns_only` connection's owner must have asked: signed in to canopy, or their
#: own token. Not email or a host's signature, which say less about who is asking.
_OWNER_TURN_ASSURANCE = frozenset({who.SESSION, who.PAT})
#: canopy's own follow-up to an agent task, still accountable to the person who started the task.
_OWNER_TURN_SYSTEM_VIA = "agent-task"


def started_by_owner(conn: Connection, turn) -> bool:
    """Whether `conn`'s owner asked for `turn`, for an `owner_turns_only` connection. A person
    only: `turn.initiator_user` on a schedule or a trigger is who set it up, so canopy's own
    turns count only as a follow-up to the task they started, and no turn is no."""
    if turn is None or conn.user_id is None or turn.initiator_user_id != conn.user_id:
        return False
    if turn.initiator_kind == who.USER:
        return turn.initiator_assurance in _OWNER_TURN_ASSURANCE
    return turn.initiator_kind == who.SYSTEM and turn.initiator_via == _OWNER_TURN_SYSTEM_VIA


def _in_force_for(agent, rows, turn=None) -> list[Connection]:
    """Those of `rows` that `agent` may use now, on `turn`: shared (`active` — an `unshared`
    row reaches no machine, a `revoking` one is never handed out), its time not run out, for its
    owner's turn if it is `owner_turns_only`, and with `agent` among the statement's grants
    still in force (`recipients.grants_in_force`: in the app's workspace and, for a person's,
    still administered by them; the owner agent counts as granted to itself)."""
    return [c for c in rows if c.state == Connection.ACTIVE and not c.expired
            and (not c.owner_turns_only or started_by_owner(c, turn))
            and any(a.pk == agent.pk for a in rcpt.grants_in_force(c))]


def _agent_candidates(agent, provider: str | None = None, *, turn=None) -> tuple[list[Connection], list[Connection]]:
    """`(own, granted)`: the connections `agent` may use now, on `turn` (`_in_force_for`), its
    own and those lent to it (by another agent or a person), for `provider` when given."""
    base = Connection.objects.filter(state=Connection.ACTIVE).select_related("app", "agent", "user")
    if provider is not None:
        base = base.filter(provider=provider)
    own = _in_force_for(agent, base.filter(agent=agent).order_by("pk"), turn)
    granted = _in_force_for(agent, base.filter(grants__agent=agent).distinct().order_by("pk"), turn)
    return own, granted


def connection_for_agent(agent, provider: str, *, turn=None) -> Connection:
    """The agent's own connection for `provider`, else one granted to it (another
    agent's, or a person's lent to it) — each only while the agent may use it now, on
    `turn` (`_in_force_for`: shared, not run out, the owner's turn if it asks for that,
    and the grant still in force). A stale grant (a lender who stopped administering
    the agent, an owner that moved), one kept for its owner's turns on anyone else's, or an
    own connection not shared yet is no candidate, so it neither answers nor makes the
    answer `ambiguous`.

    Never a person's connection that was not lent to it. More than one candidate
    at the level that answers is `ambiguous` rather than a guess.
    """
    own, granted = _agent_candidates(agent, provider, turn=turn)
    if len(own) == 1:
        return own[0]
    if len(own) > 1:
        raise ConnectionError("ambiguous", f"{agent.slug} holds more than one {provider} connection")
    if len(granted) == 1:
        return granted[0]
    if len(granted) > 1:
        raise ConnectionError("ambiguous", f"{agent.slug} is granted more than one {provider} connection")
    raise NotConnected(f"{agent.slug} has no {provider} connection")


# ---- an agent's connection serving a project's or an app's screens ---------------

def surface_share_target(conn: Connection, kind: str, ref: str) -> declarations.ShareTarget:
    """What `conn` could be shared with under this name. `ValueError`, a sentence, when it can't be:
    a person's connection, a lend with limits, or a surface outside the agent's workspace, said alike
    for one that doesn't exist so that ids can't be probed."""
    if conn.agent_id is None:
        raise ValueError("A person's own connection can't serve other people's screens; only an agent's can.")
    if conn.limited:
        raise ValueError("This connection was lent with limits, so screens can't use it.")
    if conn.state == Connection.REVOKING:
        raise ConnectionError("revoking", "This connection is being disconnected.")
    agent = conn.agent
    target = declarations.share_target(kind, ref)
    if target is None or target.workspace != agent.workspace_id:
        raise ValueError(f"There is no such project or app in {agent.slug}'s workspace.")
    if conn.app.workspace_id != target.workspace:
        raise ValueError(f"This connection was made in another workspace; connect {agent.slug}'s account again here.")
    return target


def surface_share_preview(conn: Connection, kind: str, ref: str) -> dict:
    """What approving would approve, for the person to look at and send back."""
    target = surface_share_target(conn, kind, ref)
    reads, writes = declarations.shared_lines(target.declared, conn.provider, conn.agent.slug)
    return {"kind": kind, "ref": target.ref, "title": target.title, "reads": reads, "writes": writes}


def share_with_surface(conn: Connection, kind: str, ref: str, *, expected_reads: list[dict],
                       expected_writes: list[dict], by) -> ConnectionSurfaceShare:
    """Let every screen of this project or app that can be opened use `conn`, for what it declares
    of the agent now. `expected_*` are the lines the person saw: what is stored is read here and must
    be those, or `declaration_changed`, so nobody approves what they never looked at, and what is
    approved is never a client's list. Again on the same surface replaces what was approved.
    Refused as `surface_share_target` says. Records `surface_shared`."""
    target = surface_share_target(conn, kind, ref)
    reads, writes = declarations.shared_lines(target.declared, conn.provider, conn.agent.slug)
    if not (declarations.same_lines(reads, expected_reads) and declarations.same_lines(writes, expected_writes)):
        raise ConnectionError("declaration_changed", "What these screens ask for has changed since you looked. Look "
                                                     "again, then approve what is there now.")
    share, _ = ConnectionSurfaceShare.objects.update_or_create(
        connection=conn, kind=kind, ref=target.ref,
        defaults={"approved_reads": reads, "approved_writes": writes, "approved_by": by})
    record(Subject.of(conn), "surface_shared", by=by, surface=f"{kind}:{target.ref}", approved_reads=reads,
           approved_writes=writes, summary=f"{conn.owner_label}'s {conn.provider} shared with {kind} {target.title}")
    return share


def stop_surface_share(share: ConnectionSurfaceShare, *, by) -> None:
    """Those screens stop using the connection at once. Records `surface_unshared`."""
    conn, surface = share.connection, f"{share.kind}:{share.ref}"
    share.delete()
    record(Subject.of(conn), "surface_unshared", by=by, surface=surface,
           summary=f"{conn.owner_label}'s {conn.provider} no longer shared with {surface}")


def surface_removed(kind: str, ref: str) -> None:
    """A project or app is gone for good (`declarations.on_share_target_removed`): what was approved
    for it must not pass to the next thing of that name."""
    for share in (ConnectionSurfaceShare.objects.filter(kind=kind, ref=ref)
                  .select_related("connection__agent__workspace")):
        stop_surface_share(share, by=None)


def surface_share_state(share: ConnectionSurfaceShare) -> dict:
    """A share as it stands: what was approved against what is declared now. A surface that no
    longer exists has no title and nothing waiting."""
    conn = share.connection
    target = declarations.share_target(share.kind, share.ref)
    reads, writes = (declarations.shared_lines(target.declared, conn.provider, conn.agent.slug) if target is not None
                     else ([], []))
    pending_reads = declarations.not_covered(reads, share.approved_reads)
    pending_writes = declarations.not_covered(writes, share.approved_writes)
    return {"id": share.pk, "kind": share.kind, "ref": share.ref, "title": target.title if target is not None else None,
            "account_label": conn.account_label,
            "approved_reads": share.approved_reads, "approved_writes": share.approved_writes,
            "pending_reads": pending_reads, "pending_writes": pending_writes,
            "needs_approval": bool(pending_reads or pending_writes)}


def person_connections(user, provider: str, *, workspace: str | None = None) -> list[Connection]:
    """The person's own connections for `provider` that are usable: made with an
    app in a workspace they can still read (and in `workspace`, when given)."""
    readable = readable_workspaces(user)
    if workspace is not None:
        readable &= {workspace}
    return list(connections_for(("user", user.pk)).filter(provider=provider, app__workspace_id__in=readable)
                .exclude(state=Connection.REVOKING)
                .exclude(expires_at__lte=timezone.now()).select_related("app__workspace"))


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


def providers_for_agent(agent, *, turn=None) -> list[str]:
    """The providers `agent` may use a connection for now, on `turn` — its own or one lent
    to it, by the same rule as `connection_for_agent` — sorted. What a runner stages
    for a turn; never a person's connection that was not lent to it."""
    own, granted = _agent_candidates(agent, turn=turn)
    # A row for a provider no longer registered here has nothing to stage.
    return sorted({c.provider for c in own + granted if c.provider in providers.CLASSES})

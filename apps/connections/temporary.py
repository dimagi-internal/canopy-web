"""Lending an account for a while: a grant that ends by itself, with no vault key and no passkey.

A connect started with `temporary=1` needs no vault key and no passkey, and has no share screen: the
provider's consent screen is the person's yes. The token is kept encrypted with canopy-web's key
(`TemporaryAccess`). It ends when the person ends it, which revokes it at the provider, or at the end
it was given, when the sweep revokes and deletes it.

Two shapes, by provider:

- Google asks for no refresh token (`Provider.temporary_params`), so nothing can renew it and it dies
  with its access token: an hour at most (`MAX_LIFETIME`). Lent to one agent (`for_agent=<slug>`, handed
  only to the runner holding a turn that person started), or, `for_screens=1`, for the person's own calls
  from screens in that workspace: the one way a screen calls Google.
- A provider with `temporary_renews` (Salesforce) is allowed for the person's own screens only, for as
  long as they choose, up to `LONGEST_GRANT` (`grant_length`). Its access token lives only the
  Connected App's session timeout, so the grant holds the refresh token too, encrypted the same way,
  renews the access token when it runs out (`access`, `renewed`), and revokes the refresh token at its
  end. It is never handed to a turn: a refresh token never leaves canopy-web.

Everything `apps.connections.services` says about sealing does not apply here, on purpose:
canopy-web can read this token, until the end the person chose. See `TemporaryAccess` for what that
costs and buys.
"""
from __future__ import annotations

import logging
from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.common.encryption import decrypt_secret, encrypt_secret

from . import providers, services
from .models import TemporaryAccess

logger = logging.getLogger(__name__)

#: Longest canopy-web keeps a grant nothing renews (Google), whatever the provider says the token lives.
MAX_LIFETIME = timedelta(hours=1)
#: Longest a person may allow a provider that renews (`renews`: Salesforce) for their screens.
LONGEST_GRANT = timedelta(days=30)
#: Shortest a person may choose, and what a grant is when they choose nothing.
SHORTEST_GRANT = timedelta(minutes=5)
DEFAULT_GRANT = timedelta(hours=1)
#: An access token this close to its end is renewed before it is used, so a call never starts on a dying one.
RENEW_MARGIN = timedelta(seconds=60)
#: A grant whose end passed but whose refresh token could not be revoked (the provider unreachable) is kept,
#: used by nothing, so the next sweep tries again; after this long it is dropped anyway.
REVOKE_RETRY_FOR = timedelta(days=1)


def _subject(row: TemporaryAccess) -> services.Subject:
    return services.Subject(connection=str(row.pk), provider=row.provider, owner=f"user:{row.user_id}",
                            account_label=row.account_label, workspace=None)


def supported(provider: str) -> bool:
    cls = providers.CLASSES.get(provider)
    return cls is not None and bool(cls.temporary_access)


def renews(provider: str) -> bool:
    """Whether a grant of `provider` holds a refresh token until its end, so it may last as long as the
    person chooses (`LONGEST_GRANT`) rather than one access token."""
    cls = providers.CLASSES.get(provider)
    return cls is not None and bool(cls.temporary_access) and bool(getattr(cls, "temporary_renews", False))


def longest(provider: str) -> timedelta:
    return LONGEST_GRANT if renews(provider) else MAX_LIFETIME


def grant_length(provider: str, minutes) -> timedelta | None:
    """The length a person chose, in whole minutes, when it is one `provider` may be allowed for: between
    `SHORTEST_GRANT` and `longest(provider)`. None for anything else. An empty choice is `DEFAULT_GRANT`."""
    if minutes in (None, ""):
        return min(DEFAULT_GRANT, longest(provider))
    try:
        n = int(minutes)
    except (TypeError, ValueError):
        return None
    chosen = timedelta(minutes=n)
    return chosen if SHORTEST_GRANT <= chosen <= longest(provider) else None


def agent_for(workspace, slug: str) -> Agent | None:
    """The agent a temporary connect may name: one in the workspace whose app it signs in with."""
    return Agent.objects.filter(slug=slug, workspace_id=services._slug(workspace)).first() if slug else None


def complete(session, *, provider: str, workspace, code: str, state: str, redirect_uri: str, by) -> TemporaryAccess:
    """Exchange `code`, check the account, and keep the access for the parked agent or screens. The same
    checks as `services.complete`, and a refused grant is discarded the same way. A provider that does not
    renew and answers with a refresh token anyway is refused, so nothing renewable is held for it. A new
    sign-in replaces this person's last one for that agent and provider, or for their screens."""
    parked, app = services.take_parked(session, provider=provider, workspace=workspace, state=state)
    p = services._configured(app)
    screens = bool(parked.get("for_screens"))
    keeps_refresh = screens and renews(provider)
    agent = None if screens else agent_for(workspace, parked.get("for_agent") or "")
    if agent is None and not screens:
        raise services.ConnectionError("bad_state", "that agent is no longer in this workspace")
    length = grant_length(provider, parked.get("grant_minutes"))
    if length is None:
        raise services.ConnectionError("bad_duration", "that length can't be allowed")
    subject = services._parked_subject(provider, parked)
    try:
        token = p.exchange_code(code, redirect_uri, parked["verifier"])
    except providers.ProviderError as e:
        services.record(subject, "provider_error", by=by, warn=True, during="temporary", error=services._error_code(e))
        raise services.ConnectionError("provider_error", str(e)) from None

    def refuse(code: str, why: str, **context):
        services.record(subject, "provider_error", by=by, warn=True, during="temporary", error=code, **context)
        services._discard_grant(p, subject, token)
        return services.ConnectionError(code, why)

    if token.refresh_token and not keeps_refresh:
        raise refuse("provider_error", f"{p.title} issued a refresh token when asked for none")
    try:
        ident = p.identity(token)
        p.check_owner(ident, owner_kind="user", owner=by)
        instance_url = p.sealed_hosts(token)[1] if keeps_refresh else ""
    except providers.ProviderError as e:
        code = "wrong_account" if e.code == "wrong_account" else "provider_error"
        raise refuse(code, str(e)) from None
    missing = p.missing_scopes(token.scopes)
    if missing:
        raise refuse("insufficient_scope", f"{p.title} did not grant {', '.join(missing)}", missing=" ".join(missing))

    now = timezone.now()
    # With a refresh token the grant lasts the length chosen; without one, no longer than its token.
    ends = now + length if token.refresh_token else min(token.expires_at, now + length)
    with transaction.atomic():
        # A person's screens: one per workspace app. An agent's: one per agent and provider.
        key = {"user": by, "agent": None, "app": app} if screens else {"user": by, "agent": agent,
                                                                       "provider": provider}
        before = TemporaryAccess.objects.select_for_update().filter(**key).first()
        old = None
        if before is not None and before.refresh_token:
            # Its own grant at the provider: revoking it leaves the new one alone.
            old = (decrypt_secret(before.refresh_token), "refresh_token")
        elif before is not None and before.account_id != ident.account_id:
            old = (decrypt_secret(before.token), "access_token")
        row, _ = TemporaryAccess.objects.update_or_create(
            **key,
            defaults={"app": app, "provider": provider, "account_id": ident.account_id, "account_label": ident.label,
                      "scopes": " ".join(token.scopes or p.requested_scopes()),
                      "token": encrypt_secret(token.access_token),
                      "refresh_token": encrypt_secret(token.refresh_token) if token.refresh_token else "",
                      "instance_url": instance_url,
                      "access_expires_at": token.expires_at if token.refresh_token else None,
                      "expires_at": ends})
    # The same Google account's old token is left to run out: at Google, revoking it would end the new one too.
    if old:
        _revoke(p, subject, old[0], hint=old[1])
    services.record(_subject(row), "temporary_connected", by=by, agent=agent.slug if agent else None,
                    screens=screens, expires_at=row.expires_at.isoformat(), renews=bool(token.refresh_token))
    return row


def _revoke(p, subject: services.Subject, token: str, *, hint: str = "access_token") -> bool:
    """Best effort, and never raises: an access token that can't be revoked still runs out by itself."""
    try:
        p.revoke(token, hint=hint)
        return True
    except providers.ProviderError as e:
        services.record(subject, "revoke_failed", warn=True, during="temporary", error=services._error_code(e))
        return False


def _revoke_row(row: TemporaryAccess, subject: services.Subject) -> bool:
    """Revoke what `row` holds at the provider: its refresh token, which ends its access tokens with it at
    Salesforce, else its access token."""
    p = providers.for_app(row.app)
    if row.refresh_token:
        return _revoke(p, subject, decrypt_secret(row.refresh_token), hint="refresh_token")
    return _revoke(p, subject, decrypt_secret(row.token))


def mine(user) -> list[TemporaryAccess]:
    """The person's, while they last: what the page shows. One that ran out is not listed."""
    return list(TemporaryAccess.objects.filter(user=user, expires_at__gt=timezone.now())
                .select_related("app", "agent").order_by("created_at"))


def end(row: TemporaryAccess, *, by) -> str:
    """End it now: `revoked` at the provider, or `ended` when the provider could not be reached (it
    is gone from here either way; an access token runs out by itself, and a refresh token nobody holds
    any more is never used)."""
    subject = _subject(row)
    row.delete()
    revoked = _revoke_row(row, subject)
    services.record(subject, "temporary_ended", by=by, revoked=revoked)
    return "revoked" if revoked else "ended"


def for_turn(agent, provider: str, turn) -> TemporaryAccess | None:
    """What this `turn` of `agent` may be handed for `provider`: the token of the person who started
    the turn, while it lasts and while they may still use that workspace. Nobody else's turn gets one."""
    if turn is None or turn.initiator_user_id is None:
        return None
    row = (TemporaryAccess.objects.select_related("app", "user")
           .filter(agent=agent, provider=provider, user_id=turn.initiator_user_id, expires_at__gt=timezone.now())
           .first())
    if row is None or not services.started_by_owner(row, turn):
        return None
    if not services.person_may_use(row.user, row.app.workspace_id) or agent.workspace_id != row.app.workspace_id:
        return None
    return row


def providers_for_turn(agent, turn) -> list[str]:
    """The providers `turn` of `agent` has a temporary token for, sorted."""
    if turn is None or turn.initiator_user_id is None:
        return []
    names = (TemporaryAccess.objects.filter(agent=agent, user_id=turn.initiator_user_id, expires_at__gt=timezone.now())
             .values_list("provider", flat=True))
    return sorted(n for n in names if n in providers.CLASSES and for_turn(agent, n, turn) is not None)


def token_for(row: TemporaryAccess, *, turn, runner) -> str:
    """The plaintext, for the runner holding `turn`. Each read is recorded, never its value."""
    services.record(_subject(row), "temporary_issued", turn=str(turn.pk), runner=str(runner.pk))
    return decrypt_secret(row.token)


def sweep(now=None) -> int:
    """Revoke and delete what has run out. A grant holding a refresh token is revoked at the provider
    first; one the provider could not be reached for stays (used by nothing: every read filters on
    `expires_at`) for the next sweep, for `REVOKE_RETRY_FOR`. Counted for the connections sweep."""
    now = now or timezone.now()
    done = 0
    for row in TemporaryAccess.objects.filter(expires_at__lte=now).exclude(refresh_token="").select_related("app"):
        subject = _subject(row)
        revoked = _revoke_row(row, subject)
        if revoked or row.expires_at <= now - REVOKE_RETRY_FOR:
            TemporaryAccess.objects.filter(pk=row.pk).delete()
            services.record(subject, "temporary_ended", by=None, revoked=revoked, swept=True)
            done += 1
    deleted, _ = TemporaryAccess.objects.filter(expires_at__lte=now, refresh_token="").delete()
    return done + deleted


def for_screens(user, provider: str, workspace: str) -> TemporaryAccess | None:
    """The person's own access for screens in `workspace`, while it lasts and while they may still use
    that workspace: what a screen's call runs on first (`call_service._on_temporary`). It is the grant
    made with the app serving `workspace` (`services.app_serving`: its own, or a parent's it shares), so
    one grant serves every workspace that app serves."""
    try:
        app = services.app_serving(workspace, provider)
    except services.NotConfigured:
        return None
    row = (TemporaryAccess.objects.select_related("app")
           .filter(user=user, agent__isnull=True, provider=provider, app=app, expires_at__gt=timezone.now())
           .first())
    if row is None or not services.person_may_use(user, workspace):
        return None
    return row


def token_of(row: TemporaryAccess) -> str:
    return decrypt_secret(row.token)


class Ended(services.ConnectionError):
    """The provider no longer renews this grant (its refresh token was revoked there): it is deleted."""

    def __init__(self) -> None:
        super().__init__("session_ended", "")


def current(row: TemporaryAccess) -> TemporaryAccess:
    """`row` as a call should use it: renewed first when its access token has run out (or nearly) and the
    grant holds a refresh token. Raises `Ended`, or a `try_again` `ConnectionError`."""
    runs_out = row.access_expires_at
    if row.refresh_token and runs_out is not None and runs_out <= timezone.now() + RENEW_MARGIN:
        return renewed(row, row.token)
    return row


def renewed(row: TemporaryAccess, seen: str) -> TemporaryAccess:
    """`row` with a new access token, unless the one it holds is no longer `seen` (the encrypted token a
    caller used: another call renewed it meanwhile). One renewal at a time per grant: the row is locked
    across the provider's answer, so a refresh token the provider rotates is never spent twice."""
    if not row.refresh_token:
        raise Ended()
    refused = None
    with transaction.atomic():
        locked = TemporaryAccess.objects.select_for_update(of=("self",)).select_related("app").filter(pk=row.pk).first()
        if locked is None or locked.expires_at <= timezone.now():
            raise Ended()
        if locked.token != seen:
            return locked
        p = providers.for_app(locked.app)
        try:
            token = p.refresh(decrypt_secret(locked.refresh_token))
        except providers.ProviderUnavailable:
            raise services.ConnectionError("try_again", f"{p.title} could not be reached; try again.") from None
        except providers.ProviderError as e:
            refused = services._error_code(e)
        else:
            locked.token = encrypt_secret(token.access_token)
            locked.access_expires_at = token.expires_at
            fields = ["token", "access_expires_at"]
            if token.refresh_token:  # rotated: the old one is spent
                locked.refresh_token = encrypt_secret(token.refresh_token)
                fields.append("refresh_token")
            locked.save(update_fields=fields)
            return locked
    # Refused at the provider (revoked there, or the person's session policy): nothing renews it now.
    TemporaryAccess.objects.filter(pk=locked.pk).delete()
    services.record(_subject(locked), "temporary_ended", by=None, revoked=False, error=refused, during="renew")
    raise Ended()

"""Lending an account for an hour: an access token and nothing that can renew it.

A connect started with `temporary=1` asks the provider for no refresh token
(`Provider.temporary_params`), needs no vault key and no passkey, and has no share screen: the
provider's consent screen is the person's yes. The token is kept encrypted with canopy-web's key
(`TemporaryAccess`), for one agent, and handed only to the runner holding a turn that person
started. It ends when they end it, which revokes it at the provider, or when the token runs out.

Everything `apps.connections.services` says about sealing does not apply here, on purpose:
canopy-web can read this token. See `TemporaryAccess` for what that costs and buys.
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

#: Longest canopy-web keeps one, whatever the provider says the token lives.
MAX_LIFETIME = timedelta(hours=1)


def _subject(row: TemporaryAccess) -> services.Subject:
    return services.Subject(connection=str(row.pk), provider=row.provider, owner=f"user:{row.user_id}",
                            account_label=row.account_label, workspace=None)


def supported(provider: str) -> bool:
    cls = providers.CLASSES.get(provider)
    return cls is not None and bool(cls.temporary_access)


def agent_for(workspace, slug: str) -> Agent | None:
    """The agent a temporary connect may name: one in the workspace whose app it signs in with."""
    return Agent.objects.filter(slug=slug, workspace_id=services._slug(workspace)).first() if slug else None


def complete(session, *, provider: str, workspace, code: str, state: str, redirect_uri: str, by) -> TemporaryAccess:
    """Exchange `code`, check the account, and keep the access token for the parked agent. The same
    checks as `services.complete`, and a refused grant is discarded the same way. A provider that
    answers with a refresh token anyway is refused, so nothing renewable is ever held. A new sign-in
    replaces this person's last one for that agent and provider."""
    parked, app = services.take_parked(session, provider=provider, workspace=workspace, state=state)
    p = services._configured(app)
    agent = agent_for(workspace, parked.get("for_agent") or "")
    if agent is None:
        raise services.ConnectionError("bad_state", "that agent is no longer in this workspace")
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

    if token.refresh_token:
        raise refuse("provider_error", f"{p.title} issued a refresh token when asked for none")
    try:
        ident = p.identity(token)
        p.check_owner(ident, owner_kind="user", owner=by)
    except providers.ProviderError as e:
        code = "wrong_account" if e.code == "wrong_account" else "provider_error"
        raise refuse(code, str(e)) from None
    missing = p.missing_scopes(token.scopes)
    if missing:
        raise refuse("insufficient_scope", f"{p.title} did not grant {', '.join(missing)}", missing=" ".join(missing))

    now = timezone.now()
    with transaction.atomic():
        before = TemporaryAccess.objects.select_for_update().filter(user=by, agent=agent, provider=provider).first()
        other_account = before is not None and before.account_id != ident.account_id
        old_token = decrypt_secret(before.token) if other_account else ""
        row, _ = TemporaryAccess.objects.update_or_create(
            user=by, agent=agent, provider=provider,
            defaults={"app": app, "account_id": ident.account_id, "account_label": ident.label,
                      "scopes": " ".join(token.scopes or p.requested_scopes()),
                      "token": encrypt_secret(token.access_token),
                      "expires_at": min(token.expires_at, now + MAX_LIFETIME)})
    # The same account's old token is left to run out: at Google, revoking it would end the new one too.
    if old_token:
        _revoke(p, subject, old_token)
    services.record(_subject(row), "temporary_connected", by=by, agent=agent.slug,
                    expires_at=row.expires_at.isoformat())
    return row


def _revoke(p, subject: services.Subject, token: str) -> bool:
    """Best effort, and never raises: an access token that can't be revoked still runs out by itself."""
    try:
        p.revoke(token, hint="access_token")
        return True
    except providers.ProviderError as e:
        services.record(subject, "revoke_failed", warn=True, during="temporary", error=services._error_code(e))
        return False


def mine(user) -> list[TemporaryAccess]:
    """The person's, while they last: what the page shows. One that ran out is not listed."""
    return list(TemporaryAccess.objects.filter(user=user, expires_at__gt=timezone.now())
                .select_related("app", "agent").order_by("created_at"))


def end(row: TemporaryAccess, *, by) -> str:
    """End it now: `revoked` at the provider, or `ended` when the provider could not be reached (it
    is gone from here either way, and runs out by itself)."""
    subject, token = _subject(row), decrypt_secret(row.token)
    p = providers.for_app(row.app)
    row.delete()
    revoked = _revoke(p, subject, token)
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
    """Delete what has run out: a dead token, kept by nobody. Counted for the connections sweep."""
    deleted, _ = TemporaryAccess.objects.filter(expires_at__lte=now or timezone.now()).delete()
    return deleted

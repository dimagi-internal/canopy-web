"""Licenses: who uses an agent DIRECTLY, in their own Claude Code (canopy-web#1704).

The caller list decides who MAY — `access.decide`, asked as the person on a signed-in
request. A license records who DOES, since when, from which private runner, and what
they read. The rules, in order (`request`):

1. A REVOKED license stays revoked. An admin revoked it on purpose, per person,
   without editing the rule that would otherwise grant it, so neither a re-request
   nor the caller list brings it back — only an admin's `approve`.
2. `decide` gives the person the WHOLE agent → the license is ACTIVE at once.
   Its basis is `decide`'s: `owner`/`admin` (an owner holds one implicitly — it is
   written on first use, never asked for), `admin`, or a line on its access list (`grant:<line>`).
3. Anything less (a viewer, a contact: confined or none) → a REQUESTED row the
   agent's admins see, and a refusal that says what to ask for. Approving it
   re-asks `decide`, so an admin cannot license someone the caller list does not
   give the whole agent: raise their role on its access list first.

What a license admits: a direct turn (`apps/harness/local.py`) and the credential
reads made during it (`/credentials/resolve` with that turn's `X-Canopy-Turn-Id`,
`credential_principal`). Usage and cost roll up to it later.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.harness.models import Turn

from .. import access
from ..models import AgentLicense


class LicenseRefusedError(Exception):
    """No license: `str()` says why and what to ask for."""


def _decision(agent, user):

    return access.decide(agent, user, verified=True, origin=Turn.ORIGIN_LOCAL)


def _row(agent, user) -> AgentLicense:
    try:
        with transaction.atomic():
            lic, _ = AgentLicense.objects.get_or_create(agent=agent, user=user)
    except IntegrityError:  # a concurrent first use
        lic = AgentLicense.objects.get(agent=agent, user=user)
    return lic


def request(agent, user) -> AgentLicense:
    """The person's ACTIVE license for `agent`, issuing it when the caller list allows.
    Raises LicenseRefusedError otherwise (the row is left REQUESTED or REVOKED)."""

    lic = _row(agent, user)
    if lic.status == AgentLicense.REVOKED:
        who = getattr(lic.revoked_by, "email", "") or "an admin"
        raise LicenseRefusedError(
            f"your license to use {agent.slug} directly was revoked by {who}"
            + (f" ({lic.note})" if lic.note else "")
            + f"; ask an admin of {agent.slug} to approve it again")
    decision = _decision(agent, user)
    if decision.access != access.FULL:
        why = decision.reason or (f"{agent.slug}'s caller list gives you "
                                  f"{decision.capability or 'no'} access, not the whole agent")
        AgentLicense.objects.filter(pk=lic.pk, status=AgentLicense.REQUESTED).update(note=why[:300])
        raise LicenseRefusedError(
            f"you may not use {agent.slug} directly: {why}. Your request is recorded for "
            f"{agent.slug}'s admins; they can make you an editor on its access list.")
    if lic.status != AgentLicense.ACTIVE:
        lic.status, lic.basis, lic.issued_at, lic.note = (
            AgentLicense.ACTIVE, decision.basis[:200], timezone.now(), "")
        lic.save(update_fields=["status", "basis", "issued_at", "note"])
    return lic


def active(agent, user) -> AgentLicense | None:
    """The person's license if it is ACTIVE and the caller list still gives them the
    whole agent — re-asked every time, so a demotion takes effect at once."""

    lic = AgentLicense.objects.filter(agent=agent, user=user, status=AgentLicense.ACTIVE).first()
    if lic is None or _decision(agent, user).access != access.FULL:
        return None
    return lic


def touch(lic: AgentLicense, *, private_runner=None) -> None:
    fields = {"last_used_at": timezone.now()}
    if private_runner is not None:
        fields["last_private_runner"] = private_runner
    AgentLicense.objects.filter(pk=lic.pk).update(**fields)


def revoke(agent, user, *, by, note: str = "") -> AgentLicense:
    """Revoke `user`'s license (creating a revoked row if they never had one, so a
    first use cannot slip past it). Their open direct turns keep running, but read
    nothing more: `credential_principal` asks for an ACTIVE license on every read."""
    lic = _row(agent, user)
    lic.status, lic.revoked_at, lic.revoked_by, lic.note = (
        AgentLicense.REVOKED, timezone.now(), by, (note or "")[:300])
    lic.save(update_fields=["status", "revoked_at", "revoked_by", "note"])
    return lic


def approve(agent, user, *, by) -> AgentLicense:
    """An admin issues (or reinstates) `user`'s license. Re-asks the caller list:
    refuses (LicenseRefusedError) when it does not give them the whole agent."""

    decision = _decision(agent, user)
    if decision.access != access.FULL:
        raise LicenseRefusedError(
            f"{getattr(user, 'email', 'they')} does not get the whole of {agent.slug} from its "
            "caller list — put them on its access list as an editor, then approve")
    lic = _row(agent, user)
    lic.status, lic.issued_at, lic.issued_by, lic.revoked_at, lic.revoked_by, lic.note = (
        AgentLicense.ACTIVE, timezone.now(), by, None, None, "")
    lic.basis = f"approved by {getattr(by, 'email', '') or 'an admin'} ({decision.basis})"[:200]
    lic.save()
    return lic


def credential_principal(agent, user, turn_id: str, *, runner=None):
    """The license a credential read may go out under, or None.

    Only during one of the person's OWN live turns on this agent (the read names it in
    `X-Canopy-Turn-Id`), and only on an ACTIVE license the caller list still backs.
    No turn, no secrets: reads always go against a turn. The turn is their open local
    turn, or — when the read comes from their PRIVATE runner (`runner`, its runner
    token) — a turn of theirs that runner claimed (canopy-web#1705)."""
    import uuid

    from django.db.models import Q

    try:
        tid = uuid.UUID(str(turn_id))
    except (TypeError, ValueError):
        return None
    mine = Q(origin=Turn.ORIGIN_LOCAL, status=Turn.RUNNING)
    if runner is not None and runner.private and runner.owner_id == getattr(user, "pk", None):
        mine |= Q(claimed_by=runner, status__in=(Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN))
    turn = (Turn.objects.filter(pk=tid, initiator_user_id=getattr(user, "pk", None))
            .filter(Q(agent=agent) | Q(agent__isnull=True, chat_session__agent=agent))
            .filter(mine).first())
    if turn is None:
        return None
    return active(agent, user)

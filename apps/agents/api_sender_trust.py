"""/api/agents/{slug}/sender-trust — a temporary exception to email alignment for a member's address.
"""
from __future__ import annotations

import datetime as dt

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.harness.channels.email import address_holder

from .gates import (
    agent_for_admin,
    agent_for_read,
)
from .models import (
    SenderTrust,
)
from .schemas import (
    SenderTrustIn,
    SenderTrustOut,
)

router = Router(auth=session_auth, tags=["agents"])

# ---- sender trust: a temporary exception to email alignment ----------------------
# See agents.models.SenderTrust. Agent-admin to write: it decides who an email
# is, and so what the agent's full profile answers to.

def _sender_trust_out(rule) -> SenderTrustOut:
    from django.utils import timezone

    return SenderTrustOut(
        email=rule.email, reason=rule.reason, expires_at=rule.expires_at,
        active=rule.expires_at > timezone.now(),
        created_by=rule.created_by.email if rule.created_by_id else None,
        created_at=rule.created_at,
    )


@router.get("/{slug}/sender-trust", response=list[SenderTrustOut],
            summary="List temporary trust rules for members' unaligned email")
@gate("member")
def list_sender_trust(request: HttpRequest, slug: str) -> list[SenderTrustOut]:
    """Addresses whose mail is tied to their member account although it is not
    DMARC- or DKIM-aligned — each with why, and when it lapses (expired rules
    are listed with `active: false` until removed)."""

    agent = agent_for_read(request, slug)
    return [_sender_trust_out(r) for r in
            SenderTrust.objects.filter(agent=agent).select_related("created_by")]


@router.put("/{slug}/sender-trust/{email}", response=SenderTrustOut,
            summary="Temporarily trust one member's unaligned email")
@gate("agent-admin", setting=True)
def set_sender_trust(request: HttpRequest, slug: str, email: str,
                     payload: SenderTrustIn) -> SenderTrustOut:
    """For `days` (1–90), mail From: this exact address is tied to its member
    account although it is not DMARC- or DKIM-aligned — as long as it still
    carries a passing DKIM signature, and the address still belongs to exactly
    one member of the agent's workspace. For a member whose domain signs with
    its provider's key (no DMARC, no own-domain DKIM) while that is fixed.
    Re-PUT to extend or reword; DELETE to end it early."""
    from django.utils import timezone

    agent = agent_for_admin(request, slug)
    address = (email or "").strip().lower()
    if "@" not in address:
        raise HttpError(422, f"not an email address: {email!r}")
    user, role = address_holder(address, agent.workspace_id)
    if user is None or role is None:
        raise HttpError(422, f"{address} does not belong to a member of this agent's "
                             "workspace; a trust rule only ties mail to a member")
    rule, _ = SenderTrust.objects.update_or_create(
        agent=agent, email=address,
        defaults={"reason": payload.reason.strip(),
                  "expires_at": timezone.now() + dt.timedelta(days=payload.days),
                  "created_by": request.user},
    )
    return _sender_trust_out(rule)


@router.delete("/{slug}/sender-trust/{email}", response={204: None},
               summary="End a temporary email trust rule")
@gate("agent-admin", setting=True)
def delete_sender_trust(request: HttpRequest, slug: str, email: str):
    """Their mail goes back to needing DMARC or own-domain DKIM. Idempotent."""

    agent = agent_for_admin(request, slug)
    SenderTrust.objects.filter(agent=agent, email=(email or "").strip().lower()).delete()
    return Status(204, None)



"""Letting an agent review your sessions (a coach), read-only.

The person's own act, on their memory page or the first-run screen: they name an
agent of a workspace they belong to, and that agent's OWN canopy login may then
read their sessions — the chats they started and the runner-discovered sessions
on their own boxes — through the one session ACL (`canopy_sessions.access`, leg
5). Never a sensitive conversation, never a write. Revoking ends it at once.

Every grant and revoke is on the person's audit (`sessionReviewer.granted` /
`.revoked`). The agent lists who let it in (`granted_to`); nobody else can.

Which agent the product offers as THE coach is a setting
(`settings.COACHING_AGENT_SLUG`, `coach_offer`), never named in logic.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import lifecycle
from apps.canopy_sessions.models import SessionReviewGrant
from apps.workspaces import access_requests

from . import hcp
from .models import Person


def live(user):
    return (SessionReviewGrant.objects.filter(user=user, revoked_at__isnull=True)
            .select_related("agent").order_by("created_at"))


def grant(person: Person, agent, *, user) -> SessionReviewGrant:
    """Let `agent` review `user`'s sessions. Granting again is a no-op. The
    caller has already checked `agent` is one of `user`'s workspaces'."""
    with transaction.atomic():
        current = live(user).filter(agent=agent).first()
        if current is not None:
            return current
        row = SessionReviewGrant.objects.create(user=user, agent=agent)
        _sharing_changed(user, agent, granted=True)
        hcp.audit(person, "sessionReviewer.granted", actor=hcp.user_actor(user),
                  purpose=f"let {agent.name or agent.slug} review your sessions, read-only")
    return row


def revoke(person: Person, agent, *, user) -> bool:
    """Stop `agent` reviewing `user`'s sessions. False when it could not."""
    with transaction.atomic():
        n = live(user).filter(agent=agent).update(revoked_at=timezone.now())
        if n:
            _sharing_changed(user, agent, granted=False)
            hcp.audit(person, "sessionReviewer.revoked", actor=hcp.user_actor(user),
                      purpose=f"stop {agent.name or agent.slug} reviewing your sessions")
    return bool(n)


def _sharing_changed(user, agent, *, granted: bool) -> None:
    """A coach's read of a person's sessions began or ended: one event in the agent's
    workspace (`canopy_sessions.lifecycle`)."""
    lifecycle.record(lifecycle.SHARING_CHANGED, None, by=user, workspace_id=agent.workspace_id,
                     change={"field": "review_grant", "user_id": user.pk, "agent": agent.slug,
                             "before": not granted, "after": granted})


def granted_to(agent) -> list[SessionReviewGrant]:
    """Who let `agent` review their sessions, oldest grant first."""
    return list(SessionReviewGrant.objects.filter(agent=agent, revoked_at__isnull=True)
                .select_related("user").order_by("created_at"))


def agent_named(slug: str):
    return Agent.objects.filter(slug=slug).first()


def coaching_agent():
    """The agent the product offers as a coach (`COACHING_AGENT_SLUG`), or None
    when the setting is blank or names no agent."""
    slug = (getattr(settings, "COACHING_AGENT_SLUG", "") or "").strip()
    if not slug:
        return None
    return agent_named(slug)


def coach_offer(user, member_slugs) -> dict | None:
    """The coach to offer `user`, or None: only when it is an agent of a
    workspace they belong to (`member_slugs`) or may ask to join — so the offer
    never names an agent, or a workspace, they could not otherwise reach."""
    agent = coaching_agent()
    if agent is None:
        return None
    member = agent.workspace_id in set(member_slugs)
    if not member:
        requestable = {ws.slug for ws, _domain, _pending in access_requests.requestable_workspaces(user)}
        if agent.workspace_id not in requestable:
            return None
    return {"agent": agent.slug, "name": agent.name or agent.slug, "workspace": agent.workspace_id,
            "member": member, "on": member and live(user).filter(agent=agent).exists()}

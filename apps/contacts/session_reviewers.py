"""Opting into coaching, and who coaches (read-only).

COACHING IS CANOPY'S, NOT AN AGENT'S. The person's own act, on their memory page or the first-run
screen, is to opt in to coaching: `CoachingOptIn`, naming no agent. Who coaches them is a separate fact
(`CoachingRole`: an agent or a person designated for a workspace and everything below it, by that
workspace's owner), so the program can change hands without anyone opting in again: today Trellis and
Ada, later a division's director and their agents. The person is always shown who coaches them now
(`coaches_for`) and the session ACL (`canopy_sessions.access`, leg 5) enforces it: coaches read the
chats the person started and the runner-discovered sessions on their own boxes, never a sensitive
conversation, never a write. Revoking ends it at once.

Every opt-in and opt-out is on the person's audit (`sessionReviewer.granted` / `.revoked`) and in the
event log of each workspace that coaches them; every coach designated or removed is in that workspace's
event log. The coach lists who opted in within its reach (`granted_to`); nobody else can.

The agent the product offers for one-session reviews and the weekly note's sender is still a setting
(`settings.COACHING_AGENT_SLUG`, `coach_offer`), never named in logic.
"""
from __future__ import annotations

from django.conf import settings
from django.db import transaction
from django.dispatch import receiver
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import lifecycle
from apps.canopy_sessions.models import CoachingOptIn, CoachingRole
from apps.harness.signals import session_content_deleted
from apps.workspaces import access_requests

from apps.brain import consent as brain_consent

from . import hcp
from .models import Person
from .services import person_for


def live(user):
    return CoachingOptIn.objects.filter(user=user, revoked_at__isnull=True)


def coaches_for(user) -> list[dict]:
    """Who coaches `user` now: every live role that covers a workspace they are in (that workspace or
    one above it). Agents by slug and name, people by name. Shown to the person before they opt in
    and after, because this list is what they are agreeing to."""
    from apps.workspaces import services as wsvc  # noqa: PLC0415 — as the other lazy imports here

    mine = wsvc.user_workspace_slugs(user)
    if not mine:
        return []
    out = []
    roles = (CoachingRole.objects.filter(revoked_at__isnull=True, workspace_id__in=wsvc.with_ancestors(mine))
             .select_related("agent", "user", "workspace").order_by("created_at"))
    for r in roles:
        if r.agent_id:
            out.append({"kind": "agent", "slug": r.agent.slug, "name": r.agent.name or r.agent.slug,
                        "workspace": r.workspace_id})
        else:
            out.append({"kind": "person", "slug": "", "name": r.user.get_full_name() or r.user.email
                        or r.user.username, "workspace": r.workspace_id})
    return out


def opt_in(person: Person, *, user) -> CoachingOptIn:
    """Opt `user` into coaching. Doing it again is a no-op."""
    with transaction.atomic():
        current = live(user).first()
        if current is not None:
            return current
        row = CoachingOptIn.objects.create(user=user)
        _sharing_changed(user, granted=True)
        hcp.audit(person, "sessionReviewer.granted", actor=hcp.user_actor(user),
                  purpose="opt in to coaching: canopy's coaches may review your sessions, read-only")
        brain_consent.mirror_coaching(user, on=True)
    return row


def opt_out(person: Person, *, user) -> bool:
    """Opt `user` out. False when they were not opted in."""
    with transaction.atomic():
        n = live(user).update(revoked_at=timezone.now())
        if n:
            _sharing_changed(user, granted=False)
            hcp.audit(person, "sessionReviewer.revoked", actor=hcp.user_actor(user),
                      purpose="opt out of coaching: no coach may review your sessions")
            brain_consent.mirror_coaching(user, on=False)
    return bool(n)


def _sharing_changed(user, *, granted: bool) -> None:
    """Coaching of a person began or ended: one event in each workspace whose coaches reach them
    (`canopy_sessions.lifecycle`)."""
    for ws in sorted({c["workspace"] for c in coaches_for(user)}):
        lifecycle.record(lifecycle.SHARING_CHANGED, None, by=user, workspace_id=ws,
                         change={"field": "coaching", "user_id": user.pk, "before": not granted,
                                 "after": granted})


@receiver(session_content_deleted, dispatch_uid="person_audit_session_content_deleted")
def _on_content_deleted(sender, session, by, **kwargs) -> None:
    """A person deleted one of their sessions' words: on their own audit, the session's id
    and nothing else. In the deleting transaction, so there is no unrecorded delete."""
    if by is None:
        return
    hcp.audit(person_for(user=by), "session.contentDeleted", actor=hcp.user_actor(by),
              workspace_slug=session.workspace_id, purpose="delete a session's stored content",
              detail=str(session.pk))


def granted_to(user) -> list[CoachingOptIn]:
    """Who opted into coaching, among the people `user`'s request (a coach: an agent's own login or a
    person with a role) may read now: the same rule as the sessions themselves
    (`access.review_grantors`), so a turn asked by one person is not told who else enrolled."""
    from apps.canopy_sessions import access  # noqa: PLC0415 — as this module's other lazy imports

    return list(access.review_grantors(user).select_related("user").order_by("created_at"))


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
            "member": member, "on": member and live(user).exists()}

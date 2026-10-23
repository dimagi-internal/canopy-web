"""Asking to be added to a PRIVATE conversation (a sensitive one, `harness.sensitivity`).

Someone the session ACL lets see that a conversation exists (an agent's admin, say)
but who is not one of its people cannot read it: its words are sealed to the people
in it. They can ask its owner to add them. The request carries no conversation
content, only who asked and which conversation, and it waits on Home for the
people who may add someone (`may_decide`: the chat's owner, in it).

Approval is not a route of its own. Adding someone to a sensitive conversation is
a browser act (the owner's browser wraps the conversation's key to the
requester's vault key, `POST /{id}/participants`), so the participant row that
flow writes is what approves the request (`settle_on_add`). Declining and
withdrawing are routes.
"""
from __future__ import annotations

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.attention.types import ASKING, AttentionRow
from apps.harness import sensitivity
from apps.workspaces.links import scoped_path

from . import access
from .control import person_name
from .models import JoinRequest, Session, SessionParticipant


def may_decide(user, session) -> bool:
    """Whoever may add someone to it: its owner (`access.can_share`), who is in it."""
    return access.can_share(user, session) and sensitivity.is_conversation_person(session, user=user)


def ask(session: Session, user) -> JoinRequest:
    """`user`'s request to be added to `session`. The open one if they already
    asked. ValueError when there is nothing to ask: an ordinary conversation, or
    one they are already in."""
    if not sensitivity.is_sensitive(session):
        raise ValueError("only a private conversation is asked into; this one is open to you")
    if sensitivity.is_conversation_person(session, user=user):
        raise ValueError("you are already in this conversation")
    if session.created_by_id is None:
        # A visitor's or an agent's own: no member of canopy is in it to add you.
        raise ValueError("nobody in this conversation can add you")
    open_ = JoinRequest.objects.filter(session=session, requested_by=user,
                                       status=JoinRequest.PENDING).first()
    if open_ is not None:
        return open_
    try:
        with transaction.atomic():
            return JoinRequest.objects.create(session=session, requested_by=user)
    except IntegrityError:  # asked twice at once
        return JoinRequest.objects.get(session=session, requested_by=user,
                                       status=JoinRequest.PENDING)


def latest_of(session: Session, user) -> JoinRequest | None:
    """`user`'s most recent request into `session`, if any."""
    if user is None or not getattr(user, "is_authenticated", False):
        return None
    return JoinRequest.objects.filter(session=session, requested_by=user).first()


def pending_for(session: Session):
    return (JoinRequest.objects.filter(session=session, status=JoinRequest.PENDING)
            .select_related("requested_by").order_by("created_at"))


def _decide(req: JoinRequest, status: str, by) -> JoinRequest:
    if req.status != JoinRequest.PENDING:
        raise ValueError(f"this request is already {req.status}")
    req.status, req.decided_by, req.decided_at = status, by, timezone.now()
    req.save(update_fields=["status", "decided_by", "decided_at"])
    return req


def decline(req: JoinRequest, user) -> JoinRequest:
    if not may_decide(user, req.session):
        raise PermissionError("only the conversation's owner, in it, decides who joins")
    return _decide(req, JoinRequest.DECLINED, user)


def cancel(req: JoinRequest, user) -> JoinRequest:
    if req.requested_by_id != user.pk:
        raise PermissionError("only whoever asked can withdraw a request")
    return _decide(req, JoinRequest.CANCELLED, user)


def settle_on_add(session: Session, user, *, by) -> int:
    """`user` was just added to `session`: their open request, if any, is approved."""
    return JoinRequest.objects.filter(
        session=session, requested_by=user, status=JoinRequest.PENDING,
    ).update(status=JoinRequest.APPROVED, decided_by=by, decided_at=timezone.now())


def _owned_q(user) -> Q:
    """Sessions `user` owns: started them, or was made an owner."""
    return Q(session__created_by=user) | Q(
        session__participants__user=user, session__participants__role=SessionParticipant.OWNER)


def visible_to(user):
    """Requests `user` made, or may decide."""
    return JoinRequest.objects.filter(Q(requested_by=user) | _owned_q(user)).distinct()


# ── Home (`apps.attention.sources`) ─────────────────────────────────────────────

def items(request, *, slugs: set[str]) -> list[AttentionRow]:
    """Requests waiting on this person: open requests into conversations they own
    and are in, in the workspaces Home is reading."""
    user = request.user
    rows = (JoinRequest.objects.filter(_owned_q(user), status=JoinRequest.PENDING,
                                       session__workspace_id__in=slugs)
            .select_related("session", "session__agent", "requested_by").distinct()[:200])
    out = []
    for r in rows:
        session = r.session
        if not may_decide(user, session):
            continue
        who = person_name(r.requested_by) or "Someone"
        agent = session.agent.name if session.agent_id else "an agent"
        out.append(AttentionRow(
            key=f"join:{r.pk}",
            kind="join_request",
            state=ASKING,
            reason="Wants to join",
            title=f"{who} asked to join your private conversation with {agent}",
            workspace=session.workspace_id,
            agent_slug=session.agent.slug if session.agent_id else "",
            url=scoped_path(session.workspace_id, f"/chat/{session.pk}"),
            last_activity_at=r.created_at,
            actions=("open",),
            target={"session": str(session.pk), "request": str(r.pk)},
            private=True,
        ))
    return out

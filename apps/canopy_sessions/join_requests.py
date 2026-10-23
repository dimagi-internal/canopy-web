"""Asking to be added to a PRIVATE conversation (a sensitive one, `harness.sensitivity`).

Someone the session ACL lets see that a conversation exists (an agent's admin, say)
but who is not one of its people cannot read it: its words are sealed to the people
in it. They can ask its owner to add them. The request carries no conversation
content, only who asked and which conversation. It is shown IN the conversation,
to its people (`may_see`), never on Home: every change to the open list rings
the session's socket group with the whole list (`session.join_requests`, `publish`),
and only the conversation's people are on that socket (`sensitivity.reads_content`).
Who may answer it is narrower (`may_decide`: the chat's owner, in it).

Approval is not a route of its own. Adding someone to a sensitive conversation is
a browser act (the owner's browser wraps the conversation's key to the
requester's vault key, `POST /{id}/participants`), so the participant row that
flow writes is what approves the request (`settle_on_add`). Declining and
withdrawing are routes.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from apps.harness import sensitivity
from apps.realtime.groups import publish as _publish
from apps.realtime.groups import session_group

from . import access
from .control import person_name
from .models import JoinRequest, Session, SessionParticipant


def may_see(user, session) -> bool:
    """Whoever sees the open requests: the conversation's own people."""
    return sensitivity.is_conversation_person(session, user=user)


def may_decide(user, session) -> bool:
    """Whoever may add someone to it: its owner (`access.can_share`), who is in it."""
    return access.can_share(user, session) and sensitivity.is_conversation_person(session, user=user)


#: How long after asking someone waits before asking the same conversation again.
REASK_SECONDS_DEFAULT = 3600


def reask_after() -> timedelta:
    return timedelta(seconds=int(getattr(settings, "CANOPY_JOIN_REQUEST_REASK_SECONDS",
                                         REASK_SECONDS_DEFAULT)))


class TooSoon(ValueError):
    """Asked this conversation again within `reask_after` of the last ask."""

    def __init__(self):
        minutes = max(1, int(reask_after().total_seconds() // 60))
        super().__init__(f"you asked to join this conversation in the last {minutes} minutes; "
                         "ask again later")


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
    since = timezone.now() - reask_after()
    if JoinRequest.objects.filter(session=session, requested_by=user, created_at__gte=since).exists():
        # Declined (or withdrawn) moments ago: asking again rings the owner's chat
        # again, so once per `reask_after` per conversation.
        raise TooSoon()
    try:
        with transaction.atomic():
            req = JoinRequest.objects.create(session=session, requested_by=user)
    except IntegrityError:  # asked twice at once
        return JoinRequest.objects.get(session=session, requested_by=user,
                                       status=JoinRequest.PENDING)
    publish(session)
    return req


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
    publish(req.session)
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
    n = JoinRequest.objects.filter(
        session=session, requested_by=user, status=JoinRequest.PENDING,
    ).update(status=JoinRequest.APPROVED, decided_by=by, decided_at=timezone.now())
    if n:
        publish(session)
    return n


def _owned_q(user) -> Q:
    """Sessions `user` owns: started them, or was made an owner."""
    return Q(session__created_by=user) | Q(
        session__participants__user=user, session__participants__role=SessionParticipant.OWNER)


def visible_to(user):
    """Requests `user` made, or may decide."""
    return JoinRequest.objects.filter(Q(requested_by=user) | _owned_q(user)).distinct()


def dto(req: JoinRequest) -> dict:
    """`JoinRequestOut`: who asked and how it was decided, nothing the conversation said."""
    return {
        "id": req.id, "session_id": req.session_id,
        "requested_by": req.requested_by.email, "requested_by_name": person_name(req.requested_by),
        "status": req.status, "decided_by": getattr(req.decided_by, "email", "") or "",
        "decided_at": req.decided_at, "created_at": req.created_at,
    }


def publish(session: Session) -> None:
    """Ring the conversation's socket with its whole open list, once the write
    commits. Whole, never a delta (the `session.queued` rule): a just-connected
    client has no prior to apply one to. Only its people hear it — the socket of a
    sensitive conversation admits nobody else (`sensitivity.reads_content`)."""
    session_id = session.pk

    def _send():
        rows = [dto(r) for r in JoinRequest.objects.filter(
            session_id=session_id, status=JoinRequest.PENDING,
        ).select_related("requested_by").order_by("created_at")]
        _publish(session_group(session_id), {
            "type": "session.join_requests",
            "requests": [{**r, "id": str(r["id"]), "session_id": str(r["session_id"]),
                          "created_at": r["created_at"].isoformat(),
                          "decided_at": r["decided_at"].isoformat() if r["decided_at"] else None}
                         for r in rows],
        })

    transaction.on_commit(_send)

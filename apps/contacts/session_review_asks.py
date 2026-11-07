"""Asking a coach to review ONE session, from that session's page, read-only.

The person's own act, once, with no passkey and no standing grant. It writes a
`SessionReviewAsk` and starts a chat with the coach, whose first message is this ask and
whose asker is that person; the coach replies there. Nothing that reads a
`SessionReviewGrant` reads this table: it enrols nobody in the weekly pass, uploads no other
session, and appears on no roster (`tests/test_session_review_asks.py` pins each).

The turn runs confined in canopy's `published_profiles.SESSION_REVIEW` profile, whoever asks
(`turn_context.resolve_turn_context`), because the transcript is text someone may have aimed at whoever reads it:
no agent login, no GitHub, no Bash, no canopy tool but `read_asked_session`, which reads the
session as the person through the session ACL (`access.turn_reach`). Anyone with any access to
the agent may ask, since this adds nothing to what they could already make it do.

Withdrawing stops the turn (`control.stop_session`, the durable stop) and refuses its next
read. It cannot take back what the turn already read. The window running out refuses the read.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.db import transaction
from django.utils import timezone

from apps.agents import turn_context
from apps.agents import published_profiles as profiles_mod
from apps.api.errors import TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.canopy_sessions import access, control, lifecycle, send_paths, services
from apps.canopy_sessions.models import REVIEW_METADATA_KEY, SessionReviewAsk
from apps.harness import initiator as who
from apps.harness import sensitivity
from apps.harness.models import Turn
from apps.workspaces import services as wsvc

from . import hcp, session_reviewers
from .models import Person

#: How long the coach may read the session after the person asks.
WINDOW = dt.timedelta(days=7)
VIA = "session-review"


def _name(agent) -> str:
    return agent.name or agent.slug


def prompt(session) -> str:
    """The first message of the review conversation, in the person's words. Names the
    session by id only: its title and content are data the agent reads, not instructions."""
    return (f"Please review one of my sessions: {session.pk}. I asked from its page, which lets "
            "you read this one session and nothing else of mine, with `read_asked_session`. Reply "
            "here in this conversation. I have not turned coaching on by asking.")


def _reason(agent, user, session) -> str:
    """Why `agent` cannot be asked to review `session` now, in plain words, or ""."""
    name = _name(agent)
    if access.is_content_deleted(session):
        return f"This session's content was deleted, so {name} has nothing to review."
    if sensitivity.is_sensitive(session):
        return f"This conversation is sensitive, so {name} cannot be asked to review it."
    if not wsvc.is_member(user, agent.workspace_id):
        return (f"{name} is in the {agent.workspace_id} workspace, which you are not in, "
                "so you could not read its reply.")
    decision = turn_context.for_asker(agent, user, verified=True, channel=Turn.ORIGIN_CANOPY_WEB_CHAT)
    if decision.refused:
        return decision.refusal.message
    if not session.messages.exists():
        return (f"canopy does not hold this session's messages, so {name} would have nothing to read. "
                "Load its full history on this page first.")
    return ""


def _note(session) -> str:
    """What the person should know before asking: a part of the session is missing."""
    first = session.messages.order_by("turn_index").values_list("turn_index", flat=True).first()
    if first:
        return f"canopy holds this session from message {first} on, so the review sees only that part."
    return ""


def _is_live(ask) -> bool:
    return (ask is not None and ask.revoked_at is None and ask.expires_at > timezone.now()
            and ask.review_session_id is not None)


def _ask_of(agent, user, session):
    return SessionReviewAsk.objects.filter(session=session, agent=agent, user=user).first()


def state(user, session) -> dict | None:
    """What this person may do about reviewing `session`, or None when nothing is on offer:
    it is not their own session, or there is no coach they could reach."""
    if not access.is_own_session(user, session) or session.contact_id is not None:
        return None
    offer = session_reviewers.coach_offer(user, wsvc.user_workspace_slugs(user))
    if offer is None:
        return None
    agent = session_reviewers.coaching_agent()
    ask = _ask_of(agent, user, session)
    reason = _reason(agent, user, session)
    return {"agent": agent.slug, "name": _name(agent), "available": not reason, "reason": reason,
            "note": "" if reason else _note(session),
            "ask": ({"expires_at": ask.expires_at, "conversation": ask.review_session_id,
                     "workspace": agent.workspace_id}
                    if _is_live(ask) else None)}


def ask(request, person: Person, session) -> SessionReviewAsk:
    """Open or renew the ask and start the coach's turn. A turn still in flight in its
    conversation stands: pressing twice starts one. Refused (422) with `_reason`'s words."""
    user = request.user
    current = state(user, session)
    if current is None:
        raise ProblemError(404, "There is no coach you can ask to review this session", type_=TYPE_NOT_FOUND)
    if not current["available"]:
        raise ProblemError(422, current["reason"], type_=TYPE_VALIDATION)
    agent = session_reviewers.coaching_agent()
    with transaction.atomic():
        row = _ask_of(agent, user, session)
        was_live = _is_live(row)
        conversation = row.review_session if row is not None else None
        if conversation is None:
            conversation = services.create_session(
                workspace=agent.workspace, created_by=user, agent=agent,
                title=f"{_name(agent)} reviews a session", metadata={REVIEW_METADATA_KEY: str(session.pk)})
        if was_live and Turn.objects.filter(chat_session=conversation, status__in=list(Turn.NON_TERMINAL)).exists():
            return row
        row, _ = SessionReviewAsk.objects.update_or_create(
            session=session, agent=agent, user=user,
            defaults={"review_session": conversation, "expires_at": timezone.now() + WINDOW, "revoked_at": None})
        _changed(session, user, agent, after=True, before=was_live)
        hcp.audit(person, "sessionReviewAsk.asked", actor=hcp.user_actor(user),
                  purpose=f"asked {_name(agent)} to review one session, read-only")
    _message, turn = send_paths.send_from(
        "rest", session=conversation, text=prompt(session), user=user,
        client_id=f"{VIA}-{uuid.uuid4().hex}",
        runner_requirements=getattr(request, "runner_requirements", ()),
        chat_surface=sensitivity.request_is_chat_surface(request),
        initiator=who.for_request(request, via=VIA))
    if turn is not None and (turn.status == Turn.CANCELLED or turn.capability != profiles_mod.SESSION_REVIEW):
        # the rule changed its mind between the check and the stamp: nothing may read.
        withdraw(person, session, user=user)
        raise ProblemError(422, turn.result_note or f"{_name(agent)} could not take this", type_=TYPE_VALIDATION)
    return row


def withdraw(person: Person, session, *, user) -> bool:
    """End this person's live asks about `session` and stop any turn running under them, at
    once: its next read is refused, and the stop is the durable one a lost frame cannot lose
    (`control.stop_session`). What it already read stays in its conversation. False when there
    were none."""
    with transaction.atomic():
        rows = list(SessionReviewAsk.objects.filter(session=session, user=user, revoked_at__isnull=True,
                                                    expires_at__gt=timezone.now())
                    .select_related("agent", "review_session"))
        SessionReviewAsk.objects.filter(pk__in=[r.pk for r in rows]).update(revoked_at=timezone.now())
        for r in rows:
            _changed(session, user, r.agent, after=False, before=True)
            hcp.audit(person, "sessionReviewAsk.withdrawn", actor=hcp.user_actor(user),
                      purpose=f"withdrew {_name(r.agent)}'s review of one session")
    for r in rows:
        if r.review_session is not None:
            control.stop_session(r.review_session, by=control.person_name(user))
    return bool(rows)


def _changed(session, user, agent, *, before: bool, after: bool) -> None:
    lifecycle.record(lifecycle.SHARING_CHANGED, session, by=user,
                     change={"field": "review_ask", "user_id": user.pk, "agent": agent.slug,
                             "before": before, "after": after})

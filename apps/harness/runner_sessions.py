"""The sessions open on one runner, as FACTS ONLY — for the people who keep that box.

An admin of a box (`RunnerAdmin`) can already move every session off it
(`access.may_move`), but `access.visible_session_q` shows them only the sessions a
runner discovered on its own: a chat a person started from the web is that
person's conversation, so it never appears. The open count on the runner row
did, so an admin could see "110 open" and not one of the 110 (2026-10-26).

This is the other half: every session `staleness.active` counts on the box, with
what it takes to understand and tidy it — who started it, how, when it last did
anything, which check holds it open — and none of what was said in it. No title,
no prompt, no message. A conversation's words stay with the people in it
(`access`); its existence and shape are the box's business.

Who started it is read off the FIRST turn that drives it (`staleness.driving_turns`),
because that is where canopy records it (`Turn.initiator_*`, see `initiator.py`).
A person signed in as an agent's own login is reported as that agent: that is an
agent at work, not a person in a conversation.
"""
from __future__ import annotations

from django.db.models import Count, IntegerField, Subquery, Value
from django.db.models.functions import Coalesce

from apps.agents.models import Agent
from apps.canopy_sessions import transcript_read
from apps.canopy_sessions.models import Session
from apps.canopy_sessions.staleness import active, driving_turns, open_reason

from . import runner_requirements as rr
from .models import Turn

#: Open reasons that mean someone is mid-conversation: an admin's close refuses them.
IN_USE = ("turn_running", "dialog_up", "transferring")

LIMIT = 500


def open_sessions(runner) -> list[dict]:
    """The open sessions on `runner`, longest-quiet first (what to tidy comes first)."""
    first_turn = driving_turns().order_by("created_at").values("pk")[:1]
    turn_count = (
        driving_turns().order_by().annotate(_g=Value(1)).values("_g")
        .annotate(n=Count("pk")).values("n")[:1]
    )
    sessions = list(
        transcript_read.with_activity(
            active(Session.objects.filter(runner_binding__runner=runner))
        )
        .annotate(
            _open_reason=open_reason(),
            _first_turn=Subquery(first_turn),
            _turns=Coalesce(Subquery(turn_count, output_field=IntegerField()), 0),
        )
        .select_related("agent", "workspace", "created_by", "runner_binding")
        .order_by("_activity")[:LIMIT]
    )
    turns = {
        t.pk: t
        for t in Turn.objects.filter(pk__in=[s._first_turn for s in sessions if s._first_turn])
        .select_related("initiator_user", "initiator_contact")
    }
    logins = {
        a.user_id: a.slug
        for a in Agent.objects.filter(
            user_id__in=[t.initiator_user_id for t in turns.values() if t.initiator_user_id]
        )
    }
    return [_row(s, turns.get(s._first_turn), logins) for s in sessions]


def _row(session, turn, logins) -> dict:
    binding = session.runner_binding
    return {
        "id": session.pk,
        "agent": session.agent.slug if session.agent_id else "",
        "workspace": session.workspace.slug,
        "origin": session.origin,
        "open_reason": session._open_reason,
        "started_by": _started_by(session, turn, logins),
        "first_turn_origin": turn.origin if turn else "",
        "first_turn_mode": turn.turn_mode if turn else "",
        "turns": session._turns,
        "question_waiting": bool(binding.pending_question),
        "transferred_at": binding.transferred_at,
        "sensitive": rr.METADATA_KEY in (session.metadata or {}),
        "created_at": session.created_at,
        "last_activity_at": session._activity,
    }


def _started_by(session, turn, logins) -> dict | None:
    """{kind, agent, email, name}: the first driving turn's initiator; else the web
    chat's creator; else None (a session a runner found with no turn behind it, such
    as a terminal opened on a laptop — its box's owner started it)."""
    if turn is not None and turn.initiator_kind:
        user, contact = turn.initiator_user, turn.initiator_contact
        if user is not None and user.pk in logins:
            return {"kind": "agent", "agent": logins[user.pk], "email": user.email, "name": ""}
        if user is not None:
            return {"kind": turn.initiator_kind, "agent": turn.initiator_agent,
                    "email": user.email, "name": user.get_full_name()}
        if contact is not None:
            return {"kind": turn.initiator_kind, "agent": "", "email": contact.email,
                    "name": contact.display_name}
        return {"kind": turn.initiator_kind, "agent": turn.initiator_agent, "email": "", "name": ""}
    if session.created_by_id:
        user = session.created_by
        return {"kind": "user", "agent": "", "email": user.email, "name": user.get_full_name()}
    return None


def open_session(runner, session_id):
    """(session, open reason) for one session open on `runner`, or (None, "")."""
    session = (
        active(Session.objects.filter(runner_binding__runner=runner, pk=session_id))
        .annotate(_open_reason=open_reason())
        .select_related("runner_binding__runner")
        .first()
    )
    return (session, session._open_reason) if session else (None, "")


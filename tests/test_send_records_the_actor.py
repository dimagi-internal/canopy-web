"""A send must record WHO sent it, or actor routing has nothing to route on.

`Turn.enqueued_by` is set in `harness/api.py` for turns POSTed to the API, and was
never set on the chat/session-send path — so every `canopy_web_chat` turn in
production carries no actor, and so does every `ace_web` turn, because ace-web's
run dispatcher goes through this same path
(`ace-web apps/canopy/run_dispatch.py` -> `POST /api/canopy-sessions/{id}/send`).

That makes this the load-bearing half of the ace-web leg: without it, an
`ace_web` actor rule can never match anything.

Spec: docs/superpowers/specs/2026-09-05-actor-aware-runner-routing-design.md
"""
from __future__ import annotations

import pytest

from apps.agents.models import Agent
from apps.canopy_sessions import services as chat
from apps.canopy_sessions.models import Session
from apps.harness import actors
from apps.harness.models import Turn
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _setup(django_user_model, *, metadata=None, email="sarvesh@dimagi.com"):
    ws = a_workspace()
    user = django_user_model.objects.create(username=email, email=email)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws)
    session = Session.objects.create(
        workspace=ws, agent=agent, created_by=user, title="t", metadata=metadata or {},
    )
    return user, session


def test_a_chat_send_records_the_sender_as_the_turns_actor(django_user_model):
    user, session = _setup(django_user_model)
    _msg, turn = chat.send_message(session=session, text="hi", user=user)

    assert turn.origin == Turn.ORIGIN_CANOPY_WEB_CHAT
    assert turn.enqueued_by_id == user.id
    assert actors.actor_of(turn) == "sarvesh@dimagi.com"


def test_an_ace_web_send_records_the_ace_web_user_as_the_actor(django_user_model):
    """The whole point. ace-web exchanges a delegated token for the signed-in
    user's email, so canopy knows exactly which human triggered the run — and an
    `(ace, ace_web, sarvesh@dimagi.com)` rule can finally match it."""
    user, session = _setup(django_user_model, metadata={"source": "ace-web"})
    _msg, turn = chat.send_message(session=session, text="/ace:run bednet", user=user)

    assert turn.origin == Turn.ORIGIN_ACE_WEB
    assert actors.actor_of(turn) == "sarvesh@dimagi.com"


def test_the_actor_is_normalized_so_a_rule_matches_regardless_of_casing(django_user_model):
    user, session = _setup(django_user_model, email="Sarvesh@Dimagi.com")
    _msg, turn = chat.send_message(session=session, text="hi", user=user)
    assert actors.actor_of(turn) == "sarvesh@dimagi.com"


def test_a_send_with_no_authenticated_user_records_no_actor(django_user_model):
    """`enqueue_turn` only stores an authenticated user. No actor means the turn
    falls through to the source rule — never matches one by accident."""
    _user, session = _setup(django_user_model)
    _msg, turn = chat.send_message(session=session, text="hi", user=None)
    assert turn.enqueued_by_id is None
    assert actors.actor_of(turn) == ""

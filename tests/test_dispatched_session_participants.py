"""A session an agent dispatches from inside a conversation belongs to that
conversation's people too: they join it as participants, so the person who
asked can see (and steer) what they asked for. The rule: `services._inherit_participants`.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import Client

from apps.agents.models import Agent
from apps.canopy_sessions.models import Session, SessionParticipant
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _fresh_rate_limit():
    cache.clear()


def _user(name):
    return User.objects.create_user(name, f"{name}@dimagi.com", "pw")


@pytest.fixture()
def world():
    agent_login, jj, mate, watcher, outsider = (_user(n) for n in ("ace", "jj", "mate", "watcher", "outsider"))
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    for u in (agent_login, jj, mate, watcher):
        WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj)
    # The conversation the agent dispatches from: the agent's login owns it,
    # the people talking to it are participants.
    parent = Session.objects.create(workspace=ws, agent=agent, created_by=agent_login)
    SessionParticipant.objects.create(session=parent, user=agent_login, role=SessionParticipant.OWNER)
    SessionParticipant.objects.create(session=parent, user=jj, role=SessionParticipant.EDITOR)
    SessionParticipant.objects.create(session=parent, user=watcher, role=SessionParticipant.VIEWER)
    # Someone who is no longer a member of the workspace.
    SessionParticipant.objects.create(session=parent, user=outsider, role=SessionParticipant.EDITOR)
    raw, _pat = PersonalToken.create_for_user(user=agent_login, label="cloud-ec2-1")
    return {"agent_login": agent_login, "jj": jj, "mate": mate, "watcher": watcher,
            "outsider": outsider, "parent": parent, "raw": raw}


def _dispatch(raw, **headers):
    r = Client(HTTP_AUTHORIZATION=f"Bearer {raw}", **headers).post(
        "/api/canopy-sessions/", {"project": "connect-labs", "title": "dispatched"},
        content_type="application/json")
    assert r.status_code == 200, r.content
    return Session.objects.get(pk=r.json()["id"])


def _roles(session):
    return {p.user.username: p.role for p in session.participants.select_related("user")}


def test_the_parent_conversation_s_people_join_the_dispatched_session(world):
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(world["parent"].pk))
    assert child.parent_session == world["parent"]
    # The dispatcher owns it; the asker edits; a viewer stays a viewer; a
    # former member is not let back in through a child session.
    assert _roles(child) == {"ace": "owner", "jj": "editor", "watcher": "viewer"}

    listed = Client()
    listed.force_login(world["jj"])
    ids = [s["id"] for s in listed.get("/api/canopy-sessions/").json()]
    assert str(child.pk) in ids


def test_no_parent_means_no_one_else_joins(world):
    child = _dispatch(world["raw"])
    assert _roles(child) == {"ace": "owner"}


def test_naming_a_session_you_cannot_read_pulls_no_one_in(world):
    private = Session.objects.create(workspace_id="dimagi", created_by=world["mate"])
    SessionParticipant.objects.create(session=private, user=world["mate"], role=SessionParticipant.OWNER)
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(private.pk))
    assert child.parent_session == private  # still recorded: provenance is a record
    assert _roles(child) == {"ace": "owner"}


def _emdash_session(world):
    """A session started in emdash: runner-discovered, no creator, no participant
    rows — its person reads it through the tenant leg, and typing into it from
    the web never writes a row (#926)."""
    import uuid

    from apps.canopy_sessions.models import RunnerBinding
    from apps.harness.models import Runner, Turn

    ws = world["parent"].workspace
    runner = Runner.objects.create(name="acedimagi-mbp", workspace=ws, kind=Runner.EMDASH,
                                   location=Runner.LOCAL, status=Runner.ONLINE,
                                   owner=world["jj"], host="ace@mbp")
    fi = Session.objects.create(workspace=ws, agent=world["parent"].agent, origin=Session.ORIGIN_RUNNER,
                                title="fi")
    RunnerBinding.objects.create(session=fi, runner=runner, session_key="fi-sid",
                                 emdash_project="ace", host=runner.host, thread_key="emdash:fi")
    turn = Turn.objects.create(chat_session=fi, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="hand it over",
                               idempotency_key=uuid.uuid4().hex, initiator_user=world["jj"])
    return fi, turn


def test_the_person_who_asked_in_an_emdash_session_joins_what_it_dispatched(world):
    fi, turn = _emdash_session(world)
    assert not fi.participants.exists()
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(fi.pk),
                      HTTP_X_CANOPY_PARENT_TURN=str(turn.pk))
    assert child.parent_session == fi and child.parent_turn == turn
    assert _roles(child) == {"ace": "owner", "jj": "editor"}

    listed = Client()
    listed.force_login(world["jj"])
    assert str(child.pk) in [s["id"] for s in listed.get("/api/canopy-sessions/").json()]


def test_a_turn_from_another_session_does_not_bring_its_asker(world):
    fi, _turn = _emdash_session(world)
    import uuid

    from apps.harness.models import Turn

    elsewhere = Session.objects.create(workspace_id="dimagi", created_by=world["mate"])
    stray = Turn.objects.create(chat_session=elsewhere, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="x",
                                idempotency_key=uuid.uuid4().hex, initiator_user=world["mate"])
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(fi.pk),
                      HTTP_X_CANOPY_PARENT_TURN=str(stray.pk))
    assert _roles(child) == {"ace": "owner"}

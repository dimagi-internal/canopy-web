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

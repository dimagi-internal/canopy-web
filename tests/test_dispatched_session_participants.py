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
    # `ace` is the agent's own login (`Agent.user`), which reads the agent's own
    # runner-discovered work (`access._own_work_q`).
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj, user=agent_login)
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
    rows — the box owner and the agent's own login read it, and typing into it from
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


def _web_chat(world, *, status="running", agent=None):
    """A web chat a PERSON opened with the agent: jj created it, the agent's login
    is not a participant, so it cannot read it (no agent-login leg in
    `visible_session_q`). The agent answers it in a turn jj asked for."""
    import uuid

    from apps.harness.models import Turn

    chat = Session.objects.create(workspace_id="dimagi", agent=agent or world["parent"].agent,
                                  created_by=world["jj"])
    SessionParticipant.objects.create(session=chat, user=world["jj"], role=SessionParticipant.OWNER)
    SessionParticipant.objects.create(session=chat, user=world["watcher"], role=SessionParticipant.VIEWER)
    turn = Turn.objects.create(chat_session=chat, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="go do it",
                               idempotency_key=uuid.uuid4().hex, initiator_user=world["jj"], status=status)
    return chat, turn


def _agent_has_login(world):
    agent = world["parent"].agent
    agent.user = world["agent_login"]
    agent.save(update_fields=["user"])
    return agent


def test_an_agent_dispatching_from_a_web_chat_it_is_answering_brings_its_people(world):
    from apps.canopy_sessions import access

    _agent_has_login(world)
    chat, turn = _web_chat(world)
    assert not access.can_read(world["agent_login"], chat)
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(chat.pk),
                      HTTP_X_CANOPY_PARENT_TURN=str(turn.pk))
    assert _roles(child) == {"ace": "owner", "jj": "editor", "watcher": "viewer"}


@pytest.mark.parametrize("status", ["queued", "succeeded", "failed"])
def test_a_turn_that_is_not_in_flight_brings_no_one(world, status):
    _agent_has_login(world)
    chat, turn = _web_chat(world, status=status)
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(chat.pk),
                      HTTP_X_CANOPY_PARENT_TURN=str(turn.pk))
    assert _roles(child) == {"ace": "owner"}


def test_a_live_turn_in_another_session_brings_no_one(world):
    _agent_has_login(world)
    chat, _turn = _web_chat(world)
    _other, elsewhere = _web_chat(world)
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(chat.pk),
                      HTTP_X_CANOPY_PARENT_TURN=str(elsewhere.pk))
    assert _roles(child) == {"ace": "owner"}


def test_another_agent_s_live_turn_brings_no_one(world):
    _agent_has_login(world)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace_id="dimagi", owner=world["jj"],
                               user=_user("eva-login"))
    chat, turn = _web_chat(world, agent=eva)
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(chat.pk),
                      HTTP_X_CANOPY_PARENT_TURN=str(turn.pk))
    assert _roles(child) == {"ace": "owner"}


def test_a_sensitive_web_chat_still_brings_no_one(world):
    from apps.harness import runner_requirements as rr

    _agent_has_login(world)
    chat, turn = _web_chat(world)
    chat.metadata = {rr.METADATA_KEY: [rr.SENSITIVE_DATA]}
    chat.save(update_fields=["metadata"])
    child = _dispatch(world["raw"], HTTP_X_CANOPY_PARENT_SESSION=str(chat.pk),
                      HTTP_X_CANOPY_PARENT_TURN=str(turn.pk))
    assert _roles(child) == {"ace": "owner"}


def test_the_runner_made_session_for_a_dispatched_turn_brings_them_too(world):
    """`session_sync._adopt_turn_parent` asks the same rule with the turn's dispatcher."""
    import uuid

    from apps.harness.models import Turn
    from apps.harness.session_sync import _adopt_turn_parent

    agent = _agent_has_login(world)
    chat, turn = _web_chat(world)
    work = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="the work",
                               idempotency_key=uuid.uuid4().hex, enqueued_by=world["agent_login"],
                               parent_session=chat, parent_turn=turn)
    session = Session.objects.create(workspace_id="dimagi", agent=agent, origin=Session.ORIGIN_RUNNER,
                                     parent_session=chat, parent_turn=turn)
    _adopt_turn_parent(session, work)
    assert _roles(session) == {"jj": "editor", "watcher": "viewer"}

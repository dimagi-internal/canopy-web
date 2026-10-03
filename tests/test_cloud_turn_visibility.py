"""A turn that ran on the cloud runner, or reached an agent by email, can be found.

canopy-web#1087 (2026-10-03): an ACE email turn ran on cloud-ec2-1 and finished with
a reply drafted for approval. Two things hid it:

* its session dropped out of `state=active` (and so out of Supervisor) 3 minutes
  after it STARTED, because the cloud runner never re-reports a session and the
  runner-origin staleness leg had no observer gate;
* `GET /api/harness/turns/?agent=ace` filtered on `Turn.agent`, which an email turn
  never carries — enqueue_turn moves it onto the thread's session — so no ACE email
  turn had been listed since 2026-09-10, for any caller.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import Message, RunnerBinding
from apps.canopy_sessions.staleness import CLOUD_SESSION_LIVE_WINDOW, SESSION_LIVE_WINDOW
from apps.harness import services
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    return user, ws, agent


def _runner(ws, user, name, *, reports, kind=Runner.EMDASH):
    r = Runner.objects.create(
        name=name, kind=kind, workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), owner=user, host=name,
        capabilities={"sessions": True},
    )
    if reports:
        Runner.objects.filter(pk=r.pk).update(sessions_reported_at=timezone.now())
        r.refresh_from_db()
    return r


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _recorded(agent, runner, key, *, started_ago, said=None, said_ago=None):
    """A turn's session as a runner records it (`ace:<turn id>`), started
    `started_ago`; optionally the agent's last word, `said_ago`."""
    binding = services.record_session(
        agent, f"ace:{key}", runner=runner, session_key=key, title=f"turn {key}",
    )
    RunnerBinding.objects.filter(pk=binding.pk).update(live_seen_at=timezone.now() - started_ago)
    if said is not None:
        m = Message.objects.create(session=binding.session, turn_index=1,
                                   role=Message.ASSISTANT, plaintext=said, content={})
        Message.objects.filter(pk=m.pk).update(created_at=timezone.now() - said_ago)
    return binding.session


def _active(user, **params):
    return {r["title"]: r for r in _client(user).get("/api/canopy-sessions/", params).json()}


def test_a_finished_cloud_turn_stays_listed_with_its_reply():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    _recorded(agent, cloud, "c1", started_ago=dt.timedelta(minutes=10),
              said="Draft reply to Ali — waiting for your approval.",
              said_ago=dt.timedelta(minutes=1))

    rows = _active(user, reply="true")
    assert "turn c1" in rows, "the cloud runner never re-reports; that is not staleness"
    assert rows["turn c1"]["last_reply"].startswith("Draft reply to Ali")
    assert rows["turn c1"]["agent_spoke_last"] is True


def test_a_just_started_cloud_turn_is_listed_before_it_says_anything():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    _recorded(agent, cloud, "c1", started_ago=dt.timedelta(seconds=5))
    assert "turn c1" in _active(user)


def test_a_cloud_turn_silent_past_its_window_leaves_the_list():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    old = CLOUD_SESSION_LIVE_WINDOW + dt.timedelta(hours=1)
    _recorded(agent, cloud, "c1", started_ago=old, said="done", said_ago=old)
    assert "turn c1" not in _active(user)
    assert "turn c1" in _active(user, state="archived")


def test_a_laptop_session_still_retires_on_the_short_window():
    """The observer leg is unchanged: a reporting box that stopped reporting a
    session retired it, however recently it spoke."""
    user, ws, agent = _ctx()
    laptop = _runner(ws, user, "jj-air", reports=True)
    _recorded(agent, laptop, "l1", started_ago=SESSION_LIVE_WINDOW + dt.timedelta(minutes=1),
              said="hi", said_ago=dt.timedelta(seconds=30))
    assert "turn l1" not in _active(user)


def test_a_laptop_that_never_reported_gets_no_cloud_grace():
    """No stamp is not the cloud marker: a laptop that went quiet before reports
    were stamped looks the same, and its sessions must still retire."""
    user, ws, agent = _ctx()
    laptop = _runner(ws, user, "old-air", reports=False)
    _recorded(agent, laptop, "l2", started_ago=SESSION_LIVE_WINDOW + dt.timedelta(minutes=1),
              said="hi", said_ago=dt.timedelta(seconds=30))
    assert "turn l2" not in _active(user)


def test_agent_filter_lists_an_email_turn():
    user, ws, agent = _ctx()
    turn, created = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="email-ace-t1-1",
        prompt="/ace:turn --thread t1",
        origin_ref={"thread_id": "t1", "subject": "Latest on workflows",
                    "from": "Ali <ali@example.org>"},
    )
    assert created and turn.agent_id is None and turn.chat_session_id, \
        "precondition: an email turn targets the thread's session"
    direct, _ = services.enqueue_turn(agent=agent, origin=Turn.ORIGIN_API,
                                      idempotency_key="api-1", prompt="x")

    ids = {t["id"] for t in _client(user).get("/api/harness/turns/", {"agent": "ace"}).json()}
    assert {str(turn.id), str(direct.id)} <= ids


def test_agent_filter_does_not_pull_in_another_agents_session_turns():
    user, ws, agent = _ctx()
    other = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    turn, _ = services.enqueue_turn(
        agent=other, origin=Turn.ORIGIN_EMAIL, idempotency_key="email-echo-t2-1",
        origin_ref={"thread_id": "t2", "subject": "s", "from": "x@example.org"},
    )
    ids = {t["id"] for t in _client(user).get("/api/harness/turns/", {"agent": "ace"}).json()}
    assert str(turn.id) not in ids

"""A turn that ran on the cloud runner, or reached an agent by email, can be found —
and a cloud session stays open after its turn finishes, until it is closed or its
runner is retired.

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
from apps.canopy_sessions.staleness import SESSION_LIVE_WINDOW
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


def _turn(runner, key, status, *, session=None, agent=None):
    """A turn the cloud runner claimed (`session_key` = its Claude session id), or a
    chat reply queued on `session`."""
    return Turn.objects.create(
        agent=agent, chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
        idempotency_key=f"{key}-{status}", status=status, session_key=key,
        claimed_by=runner if status != Turn.QUEUED else None,
    )


def test_a_cloud_session_is_listed_while_its_turn_runs():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    _recorded(agent, cloud, "c1", started_ago=dt.timedelta(minutes=10))
    _turn(cloud, "c1", Turn.RUNNING, agent=agent)
    assert "turn c1" in _active(user)


def test_a_cloud_session_waiting_for_approval_stays_listed_after_its_turn():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    _recorded(agent, cloud, "c1", started_ago=dt.timedelta(days=2),
              said="Draft reply to Ali — waiting for your approval.",
              said_ago=dt.timedelta(days=2))
    _turn(cloud, "c1", Turn.DONE, agent=agent)
    rows = _active(user, reply="true")
    assert rows["turn c1"]["last_reply"].startswith("Draft reply to Ali")


def test_closing_a_cloud_session_takes_it_off_the_list():
    from apps.canopy_sessions.models import Session

    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    session = _recorded(agent, cloud, "c1", started_ago=dt.timedelta(minutes=10))
    _turn(cloud, "c1", Turn.DONE, agent=agent)
    Session.objects.filter(pk=session.pk).update(status=Session.ARCHIVED)
    assert "turn c1" not in _active(user)


def test_a_retired_cloud_runners_sessions_are_closed():
    """Nothing can continue a session on a box that is gone."""
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    _recorded(agent, cloud, "c1", started_ago=dt.timedelta(days=60))
    Runner.objects.filter(pk=cloud.pk).update(status=Runner.RETIRED)
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


def _member(ws, name):
    u = User.objects.create_user(name, f"{name}@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.EDITOR)
    return u


def test_a_member_sees_that_an_agents_email_turn_ran_but_not_what_it_said():
    """hal, a member of `connect` and not an ACE admin, saw no ACE email turn."""
    user, ws, agent = _ctx()
    turn, _ = services.enqueue_turn(
        agent=agent, origin=Turn.ORIGIN_EMAIL, idempotency_key="email-ace-t3-1",
        prompt="/ace:turn --thread t3",
        origin_ref={"thread_id": "t3", "subject": "Private", "from": "ali@example.org"},
    )
    hal = _member(ws, "hal")
    rows = {t["id"]: t for t in _client(hal).get("/api/harness/turns/", {"agent": "ace"}).json()}
    assert str(turn.id) in rows
    assert rows[str(turn.id)]["content_hidden"] is True
    assert rows[str(turn.id)]["prompt"] == "" and rows[str(turn.id)]["origin_ref"] == {}


def test_a_members_private_chat_turn_stays_hidden_from_other_members():
    from apps.canopy_sessions.models import Session

    user, ws, agent = _ctx()
    chat = Session.objects.create(workspace=ws, agent=agent, created_by=user,
                                  origin=Session.ORIGIN_WEB, title="mine")
    turn, _ = services.enqueue_turn(session=chat, origin=Turn.ORIGIN_CANOPY_WEB_CHAT,
                                    idempotency_key="chat-1", prompt="secret")
    hal = _member(ws, "hal")
    ids = {t["id"] for t in _client(hal).get("/api/harness/turns/").json()}
    assert str(turn.id) not in ids


def _web_chat_on(runner, user, agent, title, *, seen_ago, said_ago=None):
    """A chat started in the app (origin=web) whose turns a runner ran."""
    from apps.canopy_sessions.models import Session

    session = Session.objects.create(workspace=agent.workspace, agent=agent, created_by=user,
                                     title=title, origin=Session.ORIGIN_WEB)
    RunnerBinding.objects.create(session=session, runner=runner, session_key=title,
                                 live_seen_at=timezone.now() - seen_ago)
    if said_ago is not None:
        m = Message.objects.create(session=session, turn_index=1, role=Message.ASSISTANT,
                                   plaintext="ok", content={})
        Message.objects.filter(pk=m.pk).update(created_at=timezone.now() - said_ago)
    return session


def test_a_web_chat_the_cloud_ran_stays_open_until_its_runner_is_retired():
    """Labs, 2026-10-05: a hal web chat was listed 67 days after its last word, on a
    cloud runner long since gone. Idle is not gone; a retired runner is."""
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)
    idle = _web_chat_on(cloud, user, agent, "idle web chat", seen_ago=dt.timedelta(days=5),
                        said_ago=dt.timedelta(days=5))
    assert "idle web chat" in _active(user)
    Runner.objects.filter(pk=cloud.pk).update(status=Runner.RETIRED)
    assert "idle web chat" not in _active(user)
    assert "idle web chat" in _active(user, state="archived")
    idle.refresh_from_db()
    assert idle.status == "active"  # derived, never written


# --- #1396: a cloud session that RAN ON ITS OWN closes when its turn is done ------
#
# 2026-10-11: 56 of Jonathan's 62 "active" sessions were finished auto turns on the
# cloud (huddle rounds, thread messages, scheduled turns), 0 waiting on him. Nobody
# waits on an auto turn, so once nothing on it is in flight it is done. Manual and
# chat-started sessions keep the open-until-closed rule (#1162 / #1277).


def _driven(runner, agent, key, status, *, mode, origin=Turn.ORIGIN_CANOPY_SCHEDULER,
            created_ago=dt.timedelta(minutes=5)):
    """A turn the cloud claimed for the session keyed `key`, its mode stamped at claim."""
    t = Turn.objects.create(
        agent=agent, origin=origin, idempotency_key=f"{key}-{status}-{mode}-{created_ago}",
        status=status, session_key=key, turn_mode=mode,
        claimed_by=runner if status != Turn.QUEUED else None,
    )
    Turn.objects.filter(pk=t.pk).update(created_at=timezone.now() - created_ago)
    return t


def _cloud(ws, user):
    return _runner(ws, user, "cloud-ec2-1", reports=False, kind=Runner.CLOUD)


def test_a_finished_auto_cloud_session_leaves_the_active_list():
    from apps.canopy_sessions.models import Session

    user, ws, agent = _ctx()
    cloud = _cloud(ws, user)
    session = _recorded(agent, cloud, "h1", started_ago=dt.timedelta(hours=2),
                        said="Round 2: proposing X.", said_ago=dt.timedelta(hours=2))
    _driven(cloud, agent, "h1", Turn.DONE, mode="auto")
    assert "turn h1" not in _active(user)
    assert "turn h1" in _active(user, state="archived")
    session.refresh_from_db()
    assert session.status == Session.ACTIVE  # derived on read, never written


def test_an_auto_cloud_session_is_listed_while_its_turn_runs():
    user, ws, agent = _ctx()
    cloud = _cloud(ws, user)
    _recorded(agent, cloud, "h2", started_ago=dt.timedelta(minutes=10))
    _driven(cloud, agent, "h2", Turn.RUNNING, mode="auto")
    assert "turn h2" in _active(user)


def test_an_auto_cloud_session_with_a_later_turn_in_flight_stays_listed():
    user, ws, agent = _ctx()
    cloud = _cloud(ws, user)
    _recorded(agent, cloud, "h3", started_ago=dt.timedelta(hours=1))
    _driven(cloud, agent, "h3", Turn.DONE, mode="auto", created_ago=dt.timedelta(hours=1))
    _driven(cloud, agent, "h3", Turn.QUEUED, mode="", created_ago=dt.timedelta(minutes=1))
    assert "turn h3" in _active(user)


def test_a_manual_cloud_turn_that_ends_asking_for_approval_stays_active():
    """The #1162 regression, pinned: a manual turn ends by asking, and its session
    is exactly the one you need to find."""
    user, ws, agent = _ctx()
    cloud = _cloud(ws, user)
    _recorded(agent, cloud, "m1", started_ago=dt.timedelta(days=2),
              said="Draft reply to Ali. Approve to send?", said_ago=dt.timedelta(days=2))
    _driven(cloud, agent, "m1", Turn.DONE, mode="manual", origin=Turn.ORIGIN_EMAIL)
    rows = _active(user, reply="true")
    assert "turn m1" in rows
    assert rows["turn m1"]["turn_mode"] == "manual"
    assert rows["turn m1"]["last_reply"].startswith("Draft reply to Ali")


def test_an_auto_chat_started_session_keeps_the_open_until_closed_rule():
    """`canopy_web_chat` is a person's chat: auto mode alone does not mean nobody
    is waiting."""
    user, ws, agent = _ctx()
    cloud = _cloud(ws, user)
    _recorded(agent, cloud, "c2", started_ago=dt.timedelta(days=1))
    _driven(cloud, agent, "c2", Turn.DONE, mode="auto", origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    assert "turn c2" in _active(user)


def test_an_auto_cloud_session_with_a_dialog_up_is_never_retired():
    user, ws, agent = _ctx()
    cloud = _cloud(ws, user)
    session = _recorded(agent, cloud, "q1", started_ago=dt.timedelta(hours=1))
    _driven(cloud, agent, "q1", Turn.DONE, mode="auto")
    RunnerBinding.objects.filter(session=session).update(
        pending_question={"question": "Proceed?", "options": [{"label": "Yes"}]},
    )
    rows = _active(user)
    assert "turn q1" in rows
    assert rows["turn q1"]["waiting_on_you"] is True


def test_a_finished_auto_web_chat_the_cloud_ran_closes_too():
    """The web-origin leg gets the same rule: an app-started session whose first
    turn was an auto dispatch (not a chat send)."""
    user, ws, agent = _ctx()
    cloud = _cloud(ws, user)
    session = _web_chat_on(cloud, user, agent, "dispatch", seen_ago=dt.timedelta(hours=1))
    Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_API,
                        idempotency_key="d1", status=Turn.DONE, turn_mode="auto",
                        claimed_by=cloud)
    assert "dispatch" not in _active(user)

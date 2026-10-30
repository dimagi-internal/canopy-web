"""Why an open session is open — `staleness.open_reason`, and the runner row's
`open_sessions_by_reason` built from it.

The reason is read off the same named checks `unseen_q` excludes on, so it is a
fact about stored rows, never a guess. These tests pin one session per reason, and
the invariant that makes the breakdown trustworthy: every session `active()` keeps
gets a named reason, never `unclassified`, and the reasons sum to `open_sessions`.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.canopy_sessions.staleness import OPEN_REASONS, UNCLASSIFIED, active, open_reason
from apps.harness import session_sync
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


def _ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    return user, ws, agent


def _runner(ws, user, name, *, kind, reports):
    r = Runner.objects.create(
        name=name, kind=kind, workspace=ws, location=Runner.LOCAL, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), owner=user, host=name,
        capabilities={"sessions": True},
    )
    if reports:
        Runner.objects.filter(pk=r.pk).update(sessions_reported_at=timezone.now())
        r.refresh_from_db()
    return r


def _recorded(agent, runner, key, *, seen_ago=dt.timedelta(minutes=1)):
    binding = session_sync.record_session(
        agent, f"ace:{key}", runner=runner, session_key=key, title=key,
    )
    RunnerBinding.objects.filter(pk=binding.pk).update(live_seen_at=timezone.now() - seen_ago)
    return binding.session


def _driven(runner, agent, key, status, *, mode, origin=Turn.ORIGIN_CANOPY_SCHEDULER):
    return Turn.objects.create(
        agent=agent, origin=origin, idempotency_key=f"{key}-{status}-{mode}",
        status=status, session_key=key, turn_mode=mode,
        claimed_by=runner if status != Turn.QUEUED else None,
    )


def _web_chat(user, agent, title, runner=None, *, seen_ago=dt.timedelta(hours=1)):
    session = Session.objects.create(workspace=agent.workspace, agent=agent, created_by=user,
                                     title=title, origin=Session.ORIGIN_WEB)
    if runner is not None:
        RunnerBinding.objects.create(session=session, runner=runner, session_key=title,
                                     live_seen_at=timezone.now() - seen_ago)
    return session


def _reasons():
    return dict(
        active(Session.objects.all()).annotate(r=open_reason()).values_list("title", "r")
    )


def _fleet():
    """One open session per reason, plus finished ones that must not be open."""
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", kind=Runner.CLOUD, reports=False)
    laptop = _runner(ws, user, "jj-mbp", kind=Runner.EMDASH, reports=True)
    quiet_laptop = _runner(ws, user, "old-mbp", kind=Runner.EMDASH, reports=False)

    moved = _recorded(agent, cloud, "transferring", seen_ago=dt.timedelta(hours=2))
    RunnerBinding.objects.filter(session=moved).update(transferred_at=timezone.now())

    _recorded(agent, laptop, "reported_recently")

    _recorded(agent, cloud, "turn_running", seen_ago=dt.timedelta(hours=1))
    _driven(cloud, agent, "turn_running", Turn.RUNNING, mode="auto")

    asking = _recorded(agent, cloud, "dialog_up", seen_ago=dt.timedelta(hours=1))
    _driven(cloud, agent, "dialog_up", Turn.DONE, mode="auto")
    RunnerBinding.objects.filter(session=asking).update(
        pending_question={"question": "Proceed?", "options": [{"label": "Yes"}]},
    )

    _recorded(agent, cloud, "manual_turn", seen_ago=dt.timedelta(days=1))
    _driven(cloud, agent, "manual_turn", Turn.DONE, mode="manual", origin=Turn.ORIGIN_EMAIL)

    _recorded(agent, cloud, "person_started", seen_ago=dt.timedelta(days=1))
    _driven(cloud, agent, "person_started", Turn.DONE, mode="auto",
            origin=Turn.ORIGIN_CANOPY_WEB_CHAT)

    _recorded(agent, cloud, "no_turn_found", seen_ago=dt.timedelta(days=3))

    _web_chat(user, agent, "unbound")
    _web_chat(user, agent, "unobservable", quiet_laptop)

    # Not open: a finished auto turn on the cloud, and a laptop session gone quiet.
    _recorded(agent, cloud, "done", seen_ago=dt.timedelta(hours=2))
    _driven(cloud, agent, "done", Turn.DONE, mode="auto")
    _recorded(agent, laptop, "gone-quiet", seen_ago=dt.timedelta(hours=2))
    return user, cloud, laptop, quiet_laptop


def test_each_open_session_is_labelled_with_the_check_that_holds_it_open():
    _fleet()
    reasons = _reasons()
    assert reasons == {reason: reason for reason in OPEN_REASONS}
    assert UNCLASSIFIED not in reasons.values()


def test_the_runner_breakdown_sums_to_its_open_count():
    user, cloud, laptop, quiet_laptop = _fleet()
    c = Client()
    c.force_login(user)
    rows = {r["name"]: r for r in c.get("/api/harness/runners/").json()}

    cloud_row = rows[cloud.name]
    assert {k: v["count"] for k, v in cloud_row["open_sessions_by_reason"].items()} == {
        "dialog_up": 1, "manual_turn": 1, "no_turn_found": 1, "person_started": 1,
        "transferring": 1, "turn_running": 1,
    }
    assert rows[laptop.name]["open_sessions_by_reason"]["reported_recently"]["count"] == 1
    assert rows[quiet_laptop.name]["open_sessions_by_reason"]["unobservable"]["count"] == 1
    for row in rows.values():
        breakdown = row["open_sessions_by_reason"]
        assert row["open_sessions"] == sum(v["count"] for v in breakdown.values())
        assert UNCLASSIFIED not in breakdown


def test_the_oldest_activity_is_the_longest_quiet_session_for_that_reason():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", kind=Runner.CLOUD, reports=False)
    old = _recorded(agent, cloud, "old", seen_ago=dt.timedelta(days=9))
    _recorded(agent, cloud, "new", seen_ago=dt.timedelta(hours=1))
    long_ago = timezone.now() - dt.timedelta(days=9)
    RunnerBinding.objects.filter(session=old).update(last_interacted_at=long_ago)
    c = Client()
    c.force_login(user)
    row = next(r for r in c.get("/api/harness/runners/").json() if r["name"] == cloud.name)
    held = row["open_sessions_by_reason"]["no_turn_found"]
    assert held["count"] == 2
    assert dt.datetime.fromisoformat(held["oldest_activity_at"]) == long_ago


def test_a_persons_web_chat_on_the_cloud_closes_after_a_day_idle():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", kind=Runner.CLOUD, reports=False)
    _recorded(agent, cloud, "fresh", seen_ago=dt.timedelta(hours=2))
    _driven(cloud, agent, "fresh", Turn.DONE, mode="auto", origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    _recorded(agent, cloud, "abandoned", seen_ago=dt.timedelta(days=2))
    old = _driven(cloud, agent, "abandoned", Turn.DONE, mode="auto",
                  origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    two_days_ago = timezone.now() - dt.timedelta(days=2)
    Turn.objects.filter(pk=old.pk).update(created_at=two_days_ago, finished_at=two_days_ago)
    _recorded(agent, cloud, "asking", seen_ago=dt.timedelta(days=2))
    asking = _driven(cloud, agent, "asking", Turn.DONE, mode="auto",
                     origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
    Turn.objects.filter(pk=asking.pk).update(created_at=two_days_ago, finished_at=two_days_ago)
    RunnerBinding.objects.filter(session_key="asking").update(
        pending_question={"question": "Proceed?", "options": [{"label": "Yes"}]},
    )
    assert _reasons() == {"fresh": "person_started", "asking": "dialog_up"}


def test_a_web_chat_an_agent_started_closes_once_nothing_runs_on_it():
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", kind=Runner.CLOUD, reports=False)
    ace_login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    Agent.objects.filter(pk=agent.pk).update(user=ace_login)
    for key, starter in (("by-login", {"initiator_kind": "user", "initiator_user": ace_login}),
                         ("by-agent", {"initiator_kind": "agent", "initiator_agent": "hal"}),
                         ("by-person", {"initiator_kind": "user", "initiator_user": user})):
        _recorded(agent, cloud, key)
        turn = _driven(cloud, agent, key, Turn.DONE, mode="auto",
                       origin=Turn.ORIGIN_CANOPY_WEB_CHAT)
        Turn.objects.filter(pk=turn.pk).update(**starter)
    assert _reasons() == {"by-person": "person_started"}


def test_a_conversation_in_a_slack_thread_stays_open_like_a_web_chat():
    """Someone asked an agent in Slack (or typed `/agent read-channel`): replies are new turns on
    the same session, so it is a person's chat — open until idle for a day — not work that ran
    on its own. An agent-started one, and any other auto session, still closes with its turn."""
    user, ws, agent = _ctx()
    cloud = _runner(ws, user, "cloud-ec2-1", kind=Runner.CLOUD, reports=False)
    ace_login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    Agent.objects.filter(pk=agent.pk).update(user=ace_login)
    two_days_ago = timezone.now() - dt.timedelta(days=2)

    def slack_session(key, *, origin=Turn.ORIGIN_SLACK, starter=None, when=None):
        session = _recorded(agent, cloud, key)
        Session.objects.filter(pk=session.pk).update(metadata={"slack_thread": f"T1:C1:{key}"})
        turn = _driven(cloud, agent, key, Turn.DONE, mode="auto", origin=origin)
        updates = dict(starter or {})
        if when is not None:
            updates.update(created_at=when, finished_at=when)
        if updates:
            Turn.objects.filter(pk=turn.pk).update(**updates)

    slack_session("slack-fresh", starter={"initiator_kind": "user", "initiator_user": user})
    slack_session("slack-abandoned", starter={"initiator_kind": "user", "initiator_user": user},
                  when=two_days_ago)
    slack_session("slack-by-agent", starter={"initiator_kind": "user", "initiator_user": ace_login})
    # Not a conversation: a scheduled turn that happens to be auto.
    _recorded(agent, cloud, "scheduled")
    _driven(cloud, agent, "scheduled", Turn.DONE, mode="auto")

    assert _reasons() == {"slack-fresh": "person_started"}

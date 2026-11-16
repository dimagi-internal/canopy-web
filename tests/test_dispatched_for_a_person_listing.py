"""A cloud session an agent started FOR a person (`origin_ref.on_behalf_of`) stays listed
like that person's conversation once its turn ends; one an agent started for itself
retires as before. The feed's `ran_on_its_own` is unchanged by design (staleness.py)."""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions import feed
from apps.canopy_sessions.models import Session
from apps.canopy_sessions.staleness import CLOUD_CHAT_IDLE, active, open_reason
from apps.harness import session_sync
from apps.harness.models import Runner, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

THREAD = "dispatch-ace-84e6857999c1"


@pytest.fixture
def ctx():
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    agent = Agent.objects.create(slug="ace", name="ACE", workspace=ws, user=login)
    cloud = Runner.objects.create(
        name="cloud-ec2-live", kind=Runner.CLOUD, workspace=ws, location=Runner.LOCAL,
        status=Runner.ONLINE, last_heartbeat_at=timezone.now(), owner=user, host="cloud-ec2-2",
        capabilities={"sessions": True})

    class Ctx:
        pass

    c = Ctx()
    c.user, c.login, c.agent, c.cloud = user, login, agent, cloud
    return c


def _dispatch(ctx, *, for_person: bool):
    ref = {"thread_key": THREAD, "dispatched_by": "ace"}
    if for_person:
        ref["on_behalf_of"] = {"user_id": ctx.user.pk, "email": ctx.user.email,
                               "turn": "f019eecb", "agent": "ace", "decided": []}
    turn = Turn.objects.create(
        agent=ctx.agent, origin=Turn.ORIGIN_API, idempotency_key="d1", status=Turn.RUNNING,
        turn_mode="auto", claimed_by=ctx.cloud, initiator_kind="user",
        initiator_user=ctx.login, origin_ref=ref)
    session = session_sync.record_session(
        ctx.agent, THREAD, runner=ctx.cloud, session_key="591ecd6a", title="Start a NEW session",
        turn=turn).session
    return turn, session


def _finish(turn, when=None):
    when = when or timezone.now()
    Turn.objects.filter(pk=turn.pk).update(status=Turn.DONE, finished_at=when, created_at=when)


def _listed():
    return dict(active(Session.objects.all()).annotate(r=open_reason()).values_list("pk", "r"))


def test_an_agents_dispatch_for_a_person_stays_listed_after_its_turn(ctx):
    turn, session = _dispatch(ctx, for_person=True)
    assert _listed() == {session.pk: "turn_running"}
    _finish(turn)
    assert _listed() == {session.pk: "person_started"}


def test_it_retires_once_idle_for_a_day_like_a_web_chat(ctx):
    turn, session = _dispatch(ctx, for_person=True)
    _finish(turn, when=timezone.now() - CLOUD_CHAT_IDLE - dt.timedelta(minutes=1))
    assert _listed() == {}


def test_an_agents_own_dispatch_still_retires_when_it_finishes(ctx):
    turn, session = _dispatch(ctx, for_person=False)
    assert _listed() == {session.pk: "turn_running"}
    _finish(turn)
    assert _listed() == {}


def test_the_feed_still_counts_it_as_run_on_its_own(ctx):
    turn, session = _dispatch(ctx, for_person=True)
    _finish(turn)
    assert feed.ran_on_its_own(Session.objects.get(pk=session.pk)) is True

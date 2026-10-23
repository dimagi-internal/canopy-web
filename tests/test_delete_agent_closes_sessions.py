"""Deleting an agent closes its open sessions. Before, the delete only SET_NULLed
Session.agent, so e2e test agents' sessions stayed listed on the cloud runner
with no agent behind them."""
from datetime import timedelta

import pytest
from unittest.mock import patch
from django.contrib.auth.models import User
from django.utils import timezone

from apps.agents.models import Agent
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def ctx(client):
    user = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w1", display_name="W1", created_by=user)
    WorkspaceMembership.objects.create(user=user, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="e2e-agent-9", name="E2E", workspace=ws, turn_mode="manual")
    client.force_login(user)
    return user, ws, agent, client


def _session(user, ws, agent, *, runner=None, key="t1"):
    s = Session.objects.create(
        workspace=ws, created_by=user, origin=Session.ORIGIN_RUNNER, title=key,
        agent=agent,
    )
    if runner:
        RunnerBinding.objects.create(
            session=s, runner=runner, host=runner.host, session_key=key,
            thread_key=f"emdash:{key}", live_seen_at=timezone.now(), reported_at=timezone.now(),
        )
    return s


def _runner(user, ws, name, *, offline=False):
    # A lapsed heartbeat reads STALE through live_status: an offline box. (Not a
    # pause — a paused daemon still polls and drains closes, so it is reachable.)
    beat = timezone.now() - timedelta(hours=2) if offline else timezone.now()
    return Runner.objects.create(
        name=name, kind=Runner.EMDASH, host=name, owner=user, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=beat,
    )


def test_deleting_an_agent_closes_each_open_session(ctx):
    user, ws, agent, client = ctx
    unbound = _session(user, ws, agent, key="web")
    online = _session(user, ws, agent, runner=_runner(user, ws, "box-on"), key="on")
    offline = _session(user, ws, agent, runner=_runner(user, ws, "box-off", offline=True), key="off")
    other = Agent.objects.create(slug="keeper", name="Keeper", workspace=ws, turn_mode="manual")
    untouched = _session(user, ws, other, key="keep")

    with patch("apps.realtime.groups.publish") as pub:
        res = client.delete("/api/agents/e2e-agent-9/")
    assert res.status_code == 204, res.content
    assert not Agent.objects.filter(slug="e2e-agent-9").exists()

    unbound.refresh_from_db()
    assert unbound.status == Session.ARCHIVED
    # Online: relayed to the runner, which deletes the emdash task.
    assert RunnerBinding.objects.get(session=online).close_requested
    assert [c.args[1]["session_key"] for c in pub.call_args_list] == ["on"]
    # Offline: recorded durably, drained when the box is back.
    assert RunnerBinding.objects.get(session=offline).close_requested
    untouched.refresh_from_db()
    assert untouched.status == Session.ACTIVE


def test_a_queued_close_still_names_its_project_after_the_agent_is_gone(ctx):
    """The runner refuses a close it cannot place in a project, and the session's
    project is derived from the agent's slug, so it blanks on delete. The drain
    reads the binding's own copy."""
    user, ws, agent, client = ctx
    runner = _runner(user, ws, "box-off", offline=True)
    s = _session(user, ws, agent, runner=runner, key="off")
    RunnerBinding.objects.filter(session=s).update(emdash_project=agent.slug)
    with patch("apps.realtime.groups.publish"):
        assert client.delete("/api/agents/e2e-agent-9/").status_code == 204
    closes = client.get(f"/api/harness/runners/{runner.id}/closes").json()["closes"]
    assert [(c["session_key"], c["project"]) for c in closes] == [("off", "e2e-agent-9")]

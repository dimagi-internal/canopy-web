"""An agent's connection works only while the agent is in its app's workspace.

Connect already holds "an agent connects with its own workspace's app"; this pins
the use-time half (final review I1): a moved agent's old connection is
`not_connected` for its turns and absent from what the runner stages, a grant of
it stops wherever the grantee is, a grant across workspaces from an owner that
stayed keeps working, and the move's dry run names what stops.
"""
from __future__ import annotations

import uuid

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents import move
from apps.agents.models import Agent, AgentAdmin
from apps.connections import services
from apps.connections.models import Connection, ConnectionGrant
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey, Turn
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import _stored, sf_app

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


def _ws(user, slug):
    w = Workspace.objects.create(slug=slug, display_name=slug.upper(), created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.OWNER)
    return w


@pytest.fixture
def dimagi(user):
    return _ws(user, "dimagi")


@pytest.fixture
def x(user):
    return _ws(user, "x")


def _agent(ws, user, slug):
    a = Agent.objects.create(slug=slug, name=slug.title(), workspace=ws, owner=user)
    AgentAdmin.objects.get_or_create(agent=a, user=user)
    return a


def _moved(agent, dest):
    Agent.objects.filter(pk=agent.pk).update(workspace=dest)
    return Agent.objects.get(pk=agent.pk)


def test_a_moved_agents_old_connection_is_not_connected(user, dimagi, x):
    eva = _agent(dimagi, user, "eva")
    _stored({"agent": eva}, user, app=sf_app(dimagi))
    assert services.connection_for_agent(eva, "salesforce")
    assert services.providers_for_agent(eva) == ["salesforce"]
    eva = _moved(eva, x)
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(eva, "salesforce")
    assert services.providers_for_agent(eva) == []


def test_reconnecting_after_a_move_is_one_connection_not_ambiguous(user, dimagi, x):
    eva = _agent(dimagi, user, "eva")
    _stored({"agent": eva}, user, app=sf_app(dimagi))
    eva = _moved(eva, x)
    here = _stored({"agent": eva}, user, app=sf_app(x), access="00D!new")
    assert services.connection_for_agent(eva, "salesforce").pk == here.pk


def test_a_grant_across_workspaces_works_while_its_owner_stays(user, dimagi, x):
    eva, echo = _agent(dimagi, user, "eva"), _agent(x, user, "echo")
    conn = _stored({"agent": eva}, user, app=sf_app(dimagi))
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=user)
    assert services.connection_for_agent(echo, "salesforce").pk == conn.pk
    assert services.providers_for_agent(echo) == ["salesforce"]


def test_a_grant_stops_when_its_owner_moves(user, dimagi, x):
    eva, echo = _agent(dimagi, user, "eva"), _agent(dimagi, user, "echo")
    conn = _stored({"agent": eva}, user, app=sf_app(dimagi))
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=user)
    _moved(eva, x)
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(echo, "salesforce")
    assert services.providers_for_agent(echo) == []


def test_a_grantee_that_moves_keeps_the_grant(user, dimagi, x):
    eva, echo = _agent(dimagi, user, "eva"), _agent(dimagi, user, "echo")
    conn = _stored({"agent": eva}, user, app=sf_app(dimagi))
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=user)
    echo = _moved(echo, x)
    assert services.connection_for_agent(echo, "salesforce").pk == conn.pk


def test_a_moved_agents_turn_token_is_not_connected(user, dimagi, x):
    eva = _agent(dimagi, user, "eva")
    _stored({"agent": eva}, user, app=sf_app(dimagi))
    eva = _moved(eva, x)
    runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=user, workspace=x,
                                   last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=eva, runner=runner, rank=0)
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=uuid.uuid4().hex, status=RunnerDeviceKey.ACTIVE)
    raw, _ = RunnerAccessToken.mint(key)
    turn = Turn.objects.create(agent=eva, origin=Turn.ORIGIN_EMAIL, prompt="x",
                               idempotency_key=uuid.uuid4().hex, status=Turn.CLAIMED, claimed_by=runner)
    base = f"/api/harness/runners/{runner.pk}/turns/{turn.pk}"
    r = Client().post(f"{base}/connection-token", {"provider": "salesforce"}, content_type="application/json",
                      HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    listed = Client().get(f"{base}/connections", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert listed.json() == {"providers": []}


def test_the_move_dry_run_names_the_connections_that_stop(user, dimagi, x):
    eva, echo = _agent(dimagi, user, "eva"), _agent(dimagi, user, "echo")
    conn = _stored({"agent": eva}, user, app=sf_app(dimagi), account_label="eva@dimagi-ai.com")
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=user)
    p = move.plan(eva, x, by=user)
    stops = [s for s in p.stops_working if s["kind"] == "connections"]
    assert len(stops) == 1
    assert "Salesforce (eva@dimagi-ai.com), shared with echo" in stops[0]["detail"]
    assert "'dimagi'" in stops[0]["detail"] and "'x'" in stops[0]["detail"]
    assert {"table": "connections.Connection", "rows": 1,
            "why": "made with 'dimagi's connection apps, which stay there"} in p.left_behind


def test_moving_writes_nothing_to_connections_and_moving_back_resumes(user, dimagi, x):
    eva = _agent(dimagi, user, "eva")
    conn = _stored({"agent": eva}, user, app=sf_app(dimagi))
    p = move.move(eva, x, by=user)
    assert any(s["kind"] == "connections" for s in p.stops_working)
    assert Connection.objects.get(pk=conn.pk).app.workspace_id == "dimagi"
    back = move.plan(Agent.objects.get(pk=eva.pk), dimagi, by=user)
    assert [s["kind"] for s in back.stops_working if s["kind"].startswith("connections")] == ["connections_resume"]


def test_an_agent_without_connections_adds_nothing_to_a_move(user, dimagi, x):
    eva = _agent(dimagi, user, "eva")
    p = move.plan(eva, x, by=user)
    assert not [s for s in p.stops_working if s["kind"].startswith("connections")]
    assert not [t for t in p.left_behind if t["table"] == "connections.Connection"]

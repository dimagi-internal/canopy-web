"""GET/PUT /api/agents/{slug}/runners — the ordered runner-assignment API that
backs the routing-matrix UI. RunnerAssignment(agent, runner, rank) is the
single routing authority for agent turns (spec 2026-07-24)."""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def owner():
    return User.objects.create_user("owner", "owner@dimagi.com", "pw")


@pytest.fixture()
def workspace(owner):
    ws = Workspace.objects.create(slug="canopy", display_name="Canopy", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    return ws


@pytest.fixture()
def client(owner):
    c = Client()
    c.force_login(owner)
    return c


@pytest.fixture()
def agent(workspace):
    return Agent.objects.create(slug="echo", name="Echo", workspace=workspace)


@pytest.fixture()
def runner_a():
    return Runner.objects.create(name="runner-a", kind=Runner.EMDASH)


@pytest.fixture()
def runner_b():
    return Runner.objects.create(name="runner-b", kind=Runner.CLOUD)


def _put(client, slug, runner_ids):
    return client.put(
        f"/api/agents/{slug}/runners",
        data=json.dumps({"runner_ids": [str(rid) for rid in runner_ids]}),
        content_type="application/json",
    )


def test_list_agent_runners_returns_seeded_order(client, agent, runner_a, runner_b):
    RunnerAssignment.objects.create(agent=agent, runner=runner_a, rank=0)
    RunnerAssignment.objects.create(agent=agent, runner=runner_b, rank=1)

    resp = client.get(f"/api/agents/{agent.slug}/runners")
    assert resp.status_code == 200, resp.content
    got = resp.json()
    assert [x["runner_name"] for x in got] == [runner_a.name, runner_b.name]
    assert [x["rank"] for x in got] == [0, 1]
    assert got[0]["runner_id"] == str(runner_a.id)
    assert got[0]["kind"] == Runner.EMDASH
    # last_heartbeat_at is unset -> live_status is DISCONNECTED, not ONLINE
    assert got[0]["online"] is False
    assert got[0]["ready"] is True


def test_list_agent_runners_empty_for_unassigned_agent(client, agent):
    resp = client.get(f"/api/agents/{agent.slug}/runners")
    assert resp.status_code == 200, resp.content
    assert resp.json() == []


def test_put_agent_runners_replaces_ordered_list(client, agent, runner_a, runner_b):
    r = _put(client, agent.slug, [runner_b.id, runner_a.id])
    assert r.status_code == 200, r.content

    got = client.get(f"/api/agents/{agent.slug}/runners").json()
    assert [x["runner_name"] for x in got] == [runner_b.name, runner_a.name]
    assert [x["rank"] for x in got] == [0, 1]


def test_put_agent_runners_reorders_removes_and_adds_atomically(client, agent, runner_a, runner_b):
    RunnerAssignment.objects.create(agent=agent, runner=runner_a, rank=0)
    RunnerAssignment.objects.create(agent=agent, runner=runner_b, rank=1)
    runner_c = Runner.objects.create(name="runner-c", kind=Runner.REMOTE)

    r = _put(client, agent.slug, [runner_b.id, runner_c.id])
    assert r.status_code == 200, r.content

    got = client.get(f"/api/agents/{agent.slug}/runners").json()
    assert [x["runner_name"] for x in got] == [runner_b.name, runner_c.name]
    assert [x["rank"] for x in got] == [0, 1]
    # runner_a was dropped from the list entirely
    assert runner_a.name not in [x["runner_name"] for x in got]


def test_put_agent_runners_rejects_retired_runner(client, agent, runner_a):
    retired = Runner.objects.create(name="retired-1", kind=Runner.EMDASH, status=Runner.RETIRED)

    r = _put(client, agent.slug, [runner_a.id, retired.id])
    assert r.status_code == 422, r.content
    assert str(retired.id) in r.json()["detail"]
    # nothing was persisted — the bad id rolls back the whole batch
    assert RunnerAssignment.objects.filter(agent=agent).count() == 0


def test_put_agent_runners_rejects_unknown_runner_id(client, agent):
    import uuid

    bogus = uuid.uuid4()
    r = _put(client, agent.slug, [bogus])
    assert r.status_code == 422, r.content
    assert str(bogus) in r.json()["detail"]
    assert RunnerAssignment.objects.filter(agent=agent).count() == 0


def test_put_agent_runners_rejects_runner_paired_by_other_user(client, agent, runner_a):
    """A runner paired by a different human, in a workspace the caller isn't a
    member of, is invisible to _runner_visibility_q — it must 422 the same as
    a nonexistent id (no existence leak), and must not get attached."""
    other_owner = User.objects.create_user("other-runner-owner", "other-runner-owner@dimagi.com", "pw")
    other_ws = Workspace.objects.create(slug="other-runner-ws", display_name="Other RW", created_by=other_owner)
    WorkspaceMembership.objects.create(user=other_owner, workspace=other_ws, role=WorkspaceMembership.OWNER)
    foreign = Runner.objects.create(
        name="foreign-runner", kind=Runner.EMDASH, paired_by=other_owner, workspace=other_ws,
    )

    r = _put(client, agent.slug, [runner_a.id, foreign.id])
    assert r.status_code == 422, r.content
    assert str(foreign.id) in r.json()["detail"]
    # nothing was persisted — the bad id rolls back the whole batch
    assert RunnerAssignment.objects.filter(agent=agent).count() == 0


def test_put_agent_runners_empty_list_clears_assignments(client, agent, runner_a):
    RunnerAssignment.objects.create(agent=agent, runner=runner_a, rank=0)

    r = _put(client, agent.slug, [])
    assert r.status_code == 200, r.content
    assert r.json() == []
    assert RunnerAssignment.objects.filter(agent=agent).count() == 0


def test_put_agent_runners_rejects_duplicate_runner_id(client, agent, runner_a, runner_b):
    RunnerAssignment.objects.create(agent=agent, runner=runner_a, rank=0)

    r = _put(client, agent.slug, [runner_b.id, runner_b.id])
    assert r.status_code == 422, r.content
    assert "duplicate runner id" in r.json()["detail"]
    # assignments unchanged
    assert RunnerAssignment.objects.filter(agent=agent).count() == 1
    assert RunnerAssignment.objects.filter(agent=agent).first().runner_id == runner_a.id


def test_agent_runners_is_tenant_gated(client, workspace):
    other_owner = User.objects.create_user("other", "other@dimagi.com", "pw")
    other_ws = Workspace.objects.create(slug="other", display_name="Other", created_by=other_owner)
    WorkspaceMembership.objects.create(user=other_owner, workspace=other_ws, role=WorkspaceMembership.OWNER)
    Agent.objects.create(slug="secret-agent", name="Secret", workspace=other_ws)

    assert client.get("/api/agents/secret-agent/runners").status_code == 404
    assert _put(client, "secret-agent", []).status_code == 404

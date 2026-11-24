"""The fleet list reads each box's `github.<slug>` checks in a fixed number of queries.

Every row's health asks which agents the box runs (`routing.agents_routed_to`) and
each agent's GitHub readiness (`delegations.readiness`). Asked row by row that is a
membership walk and a delegation read per (runner, agent) pair; the list reads them
once for the lot (`api_runners_fleet.prime_github_readiness`), with the same answer.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import routing
from apps.harness import runners as harness_runners
from apps.harness.models import Runner, RunnerAssignment, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    jj = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    viewer = get_user_model().objects.create_user(username="vi", email="vi@dimagi.com")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    agents = [Agent.objects.create(slug=f"a{i}", name=f"A{i}", workspace=ws, owner=jj) for i in range(6)]
    return {"jj": jj, "viewer": viewer, "ws": ws, "agents": agents}


def _runner(fleet, i, *, owner=None, assign=True, **kw):
    r = Runner.objects.create(
        name=f"r{i}", kind=kw.pop("kind", Runner.CLOUD), host=f"r{i}", owner=owner or fleet["jj"],
        workspace=fleet["ws"], status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
        health={"checks": [{"name": "disk", "status": "ok", "detail": ""}],
                "received_at": timezone.now().isoformat()}, **kw)
    harness_runners.enroll_runner(r, "dimagi") if r.owner_id == fleet["jj"].pk else None
    if assign:
        for a in fleet["agents"]:
            RunnerAssignment.objects.create(agent=a, runner=r, rank=i, enabled=True)
    return r


def _list_queries(user) -> tuple[int, list]:
    c = Client()
    c.force_login(user)
    c.get("/api/harness/runners/")  # warm the session
    with CaptureQueriesContext(connection) as ctx:
        resp = c.get("/api/harness/runners/")
    assert resp.status_code == 200, resp.content
    return len(ctx.captured_queries), resp.json()


def test_the_list_costs_the_same_for_three_boxes_or_nine(fleet):
    for i in range(3):
        _runner(fleet, i)
    few, rows = _list_queries(fleet["jj"])
    assert {n for r in rows for n in r["health_checks"]} >= {f"github.a{i}" for i in range(6)}
    for i in range(3, 9):
        _runner(fleet, i)
    many, rows = _list_queries(fleet["jj"])
    assert len(rows) == 9
    assert many == few, (few, many)


def test_many_runners_route_the_same_agents_as_one_at_a_time(fleet):
    jj, agents = fleet["jj"], fleet["agents"]
    plain = _runner(fleet, 0)
    _runner(fleet, 1, assign=False)  # routes nothing
    one = _runner(fleet, 2, capabilities={"serves": {"kind": "agent", "slug": "a1"}})
    orphan = _runner(fleet, 3, owner=None)
    Runner.objects.filter(pk=orphan.pk).update(owner=None)
    viewers = _runner(fleet, 4, owner=fleet["viewer"])  # its owner may hold none of them
    follower = _runner(fleet, 5, assign=False)
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=follower, rank=0, enabled=True)
    RunnerAssignment.objects.filter(runner=plain, agent=agents[2]).update(enabled=False)
    # No order of its own: it follows the workspace's, which lists `follower`.
    Agent.objects.create(slug="lone", name="Lone", workspace=fleet["ws"], owner=jj)
    rows = list(Runner.objects.all())
    bulk = routing.agents_routed_to_of(rows)
    for r in rows:
        assert [a.slug for a in bulk[r.pk]] == [a.slug for a in routing.agents_routed_to(r)], r.name
    assert [a.slug for a in bulk[one.pk]] == ["a1"]
    assert "a2" not in [a.slug for a in bulk[plain.pk]]
    assert bulk[viewers.pk] == []
    assert "lone" in [a.slug for a in bulk[follower.pk]]

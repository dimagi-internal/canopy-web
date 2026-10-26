"""The runners a person may start a chat with an agent on (`GET /pinnable-runners`).

The New chat picker listed only the agent's runner order, so an owner whose agent
is ordered onto a cloud box could not choose their own laptop for it — though
choosing one pins the chat there past the order and any strict rule. The list is
every box the placement gates would accept for the caller, the order's first.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _runner(owner, name, *, kind=Runner.EMDASH, projects=("ace",), sessions=True, status=Runner.ONLINE):
    return enrolled(Runner.objects.create(
        name=name, kind=kind, host=name, owner=owner, status=status,
        last_heartbeat_at=timezone.now(),
        capabilities={"projects": list(projects), "sessions": sessions},
    ))


@pytest.fixture
def world():
    jj, mt = _user("jj"), _user("mt")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=mt, role=WorkspaceMembership.EDITOR)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj)
    cloud = _runner(jj, "cloud-2", kind=Runner.CLOUD)
    laptop = _runner(jj, "jj-mbp")
    RunnerAssignment.objects.create(agent=ace, runner=cloud, rank=0)
    return {"jj": jj, "mt": mt, "ace": ace, "cloud": cloud, "laptop": laptop}


def _names(user):
    c = Client()
    c.force_login(user)
    res = c.get("/api/w/connect/agents/ace/pinnable-runners")
    assert res.status_code == 200, res.content
    return [(r["runner_name"], r["rank"]) for r in res.json()]


def test_the_owner_may_choose_a_box_outside_the_order(world):
    assert _names(world["jj"]) == [("cloud-2", 0), ("jj-mbp", None)]


def test_a_box_without_the_agent_or_sessions_is_not_offered(world):
    _runner(world["jj"], "no-repo", projects=("hal",))
    _runner(world["jj"], "no-sessions", sessions=False)
    assert [n for n, _ in _names(world["jj"])] == ["cloud-2", "jj-mbp"]


def test_a_box_dedicated_to_other_agents_is_not_offered(world):
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=world["ace"].workspace)
    world["laptop"].only_agents.set([hal])
    assert [n for n, _ in _names(world["jj"])] == ["cloud-2"]


def test_someone_else_sees_only_the_boxes_they_administer(world):
    mine = _runner(world["mt"], "mt-mbp")
    assert _names(world["mt"]) == []  # mt is no admin of ace, and their box cannot hold it
    RunnerAdmin.objects.create(runner=world["laptop"], user=world["mt"], granted_by=world["jj"])
    assert _names(world["mt"]) == [("jj-mbp", None)]
    assert mine.name not in [n for n, _ in _names(world["jj"])]  # a box ace's admins never trusted


def test_a_paused_box_in_the_order_still_sorts_by_rank(world):
    Runner.objects.filter(pk=world["cloud"].pk).update(paused=True)
    assert _names(world["jj"]) == [("cloud-2", 0), ("jj-mbp", None)]

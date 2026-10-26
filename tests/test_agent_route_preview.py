"""Where an agent's work would go right now (`GET /route-preview`) — what "Auto"
means, read from the claim's own ladder (`routing.labelled_assignment_rows`).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.harness import routing
from apps.harness.models import Runner, RunnerAssignment, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _runner(owner, name, *, kind=Runner.EMDASH, status=Runner.ONLINE):
    return enrolled(Runner.objects.create(
        name=name, kind=kind, host=name, owner=owner, status=status,
        last_heartbeat_at=timezone.now(), capabilities={"projects": ["ace"], "sessions": True},
    ))


@pytest.fixture
def world():
    """ACE as it was found: ordered onto the cloud box alone, with a person route
    that puts jj's laptops first (the first of them offline), not strict."""
    jj = _user("jj")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws, owner=jj)
    cloud = _runner(jj, "cloud-2", kind=Runner.CLOUD)
    hal_box = _runner(jj, "hal-mbp", status=Runner.DISCONNECTED)
    laptop = _runner(jj, "jj-mbp")
    RunnerAssignment.objects.create(agent=ace, runner=cloud, rank=0)
    for rank, box in enumerate((hal_box, laptop)):
        RunnerAssignment.objects.create(agent=ace, runner=box, rank=rank, source="canopy_web_chat",
                                        actor="jj@dimagi.com")
    return {"jj": jj, "ws": ws, "ace": ace, "cloud": cloud, "hal": hal_box, "laptop": laptop}


def _preview(user, **params):
    c = Client()
    c.force_login(user)
    res = c.get("/api/w/connect/agents/ace/route-preview", params)
    assert res.status_code == 200, res.content
    return res.json()


def test_auto_for_you_follows_your_route_past_an_offline_box(world):
    body = _preview(world["jj"])
    assert [(r["runner_name"], r["rung"], r["status"]) for r in body["runners"]] == [
        ("hal-mbp", "person", "offline"), ("jj-mbp", "person", "online"), ("cloud-2", "default", "online")]
    assert body["picks"] == str(world["laptop"].pk)
    assert body["summary"] == "Goes to jj-mbp, from your route (hal-mbp is offline)"


def test_someone_without_a_route_gets_the_agents_order(world):
    body = _preview(world["jj"], actor="Someone <someone@else.org>")
    assert body["actor"] == "someone@else.org"
    assert [r["runner_name"] for r in body["runners"]] == ["cloud-2"]
    assert body["summary"] == "Goes to cloud-2, from ACE's runner order"


def test_a_strict_rule_that_has_nobody_available_waits(world):
    RunnerAssignment.objects.filter(agent=world["ace"], actor="jj@dimagi.com").update(strict=True)
    Runner.objects.filter(pk=world["laptop"].pk).update(paused=True)
    body = _preview(world["jj"])
    assert body["strict"] is True and body["picks"] is None
    assert [r["runner_name"] for r in body["runners"]] == ["hal-mbp", "jj-mbp"]  # no cloud below a strict rule
    assert body["summary"] == "Waits: no runner is available (hal-mbp is offline, jj-mbp is paused)"


def test_an_agent_following_its_workspace_order_says_so(world):
    RunnerAssignment.objects.filter(agent=world["ace"]).delete()
    WorkspaceRunnerOrder.objects.create(workspace=world["ws"], runner=world["cloud"], rank=0)
    body = _preview(world["jj"], source="email")
    assert body["follows_workspace"] == "connect"
    assert body["summary"] == "Goes to cloud-2, from the connect workspace order"


def test_the_preview_and_the_claim_share_one_ladder(world):
    defaults, priorities = routing.load_assignment_rows([world["ace"].pk])
    args = (world["ace"].pk, "canopy_web_chat", "jj@dimagi.com", defaults, priorities)
    labelled = routing.labelled_assignment_rows(*args, serves="ace")
    assert routing.assignment_rows_for(*args, serves="ace") == list(enumerate(r for r, _ in labelled))

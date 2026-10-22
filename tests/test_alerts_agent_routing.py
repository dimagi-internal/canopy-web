"""The `agent_cant_run` alert source: an agent that is CONFIGURED so nothing can
run it — no routes at all, or a workspace order whose runners lack its repo.

Configuration only: a routed but offline runner is liveness and raises nothing.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.utils import timezone

from apps.agents.models import Agent
from apps.attention import alerts
from apps.attention.models import Alert
from apps.harness import alerts_agent_routing as src
from apps.harness.models import Runner, RunnerAssignment, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

SOURCE = "apps.harness.alerts_agent_routing"


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


@pytest.fixture
def fleet(monkeypatch):
    jj, ed, viewer = _user("jj"), _user("ed"), _user("viewer")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=ed, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=ws, user=viewer, role=WorkspaceMembership.VIEWER)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=jj)
    monkeypatch.setattr(alerts, "SOURCES", [SOURCE])
    monkeypatch.setattr(alerts, "send_to_user", lambda user, title, body, url: False)
    return {"jj": jj, "ed": ed, "viewer": viewer, "ws": ws, "eva": eva}


def _runner(fleet, name, kind=Runner.CLOUD, projects=(), status=Runner.ONLINE):
    return Runner.objects.create(
        name=name, kind=kind, host=name, owner=fleet["jj"], workspace=fleet["ws"], status=status,
        last_heartbeat_at=timezone.now() if status == Runner.ONLINE else None,
        capabilities={"agents": [], "projects": list(projects)})


def _raises():
    return {r.subject_key: r for r in src.reconcile(timezone.now())}


def test_agent_with_no_route_raises_setup(fleet):
    out = _raises()
    assert set(out) == {"agent:eva:routing"}
    r = out["agent:eva:routing"]
    assert r.title == "Eva has no runner to run on"
    assert r.workspace == "dimagi"
    assert r.url == "/w/dimagi/agents/eva/settings"
    assert r.scopes == ("agent:eva", "workspace:dimagi")
    assert src.SEVERITY == "setup" and src.CONFIRM_WITHIN is None
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).severity == Alert.SETUP


def test_routed_agent_raises_nothing_and_fix_resolves(fleet):
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).status == Alert.OPEN
    RunnerAssignment.objects.create(agent=fleet["eva"], runner=_runner(fleet, "box"), rank=0)
    assert _raises() == {}
    alerts.tick(timezone.now())
    a = Alert.objects.get(kind=src.KIND)
    assert a.status == Alert.RESOLVED and a.resolved_reason == Alert.REASON_SOURCE


def test_offline_runner_does_not_raise(fleet):
    box = _runner(fleet, "box", status=Runner.DISCONNECTED)
    RunnerAssignment.objects.create(agent=fleet["eva"], runner=box, rank=0)
    assert _raises() == {}


def test_inherited_offline_runner_does_not_raise(fleet):
    box = _runner(fleet, "cloud-ec2-1", status=Runner.DISCONNECTED)
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=box, rank=0)
    assert _raises() == {}


def test_missing_repo_raises_and_fix_resolves(fleet):
    laptop = _runner(fleet, "jj-laptop", kind=Runner.EMDASH, projects=["other"])
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=laptop, rank=0)
    r = _raises()["agent:eva:routing"]
    assert r.title == "Eva: jj-laptop doesn't have its repo"
    alerts.tick(timezone.now())
    a = Alert.objects.get(kind=src.KIND)
    fp = a.fingerprint
    alerts.tick(timezone.now())
    assert Alert.objects.get(kind=src.KIND).fingerprint == fp  # no churn

    laptop.capabilities = {"agents": [], "projects": ["eva"]}
    laptop.save(update_fields=["capabilities"])
    assert _raises() == {}
    alerts.tick(timezone.now())
    a.refresh_from_db()
    assert a.status == Alert.RESOLVED


def test_missing_repo_names_are_joined_readably(fleet):
    for i, n in enumerate(["a-laptop", "b-laptop", "c-laptop"]):
        WorkspaceRunnerOrder.objects.create(
            workspace=fleet["ws"], runner=_runner(fleet, n, kind=Runner.EMDASH, projects=["x"]), rank=i)
    assert _raises()["agent:eva:routing"].title == "Eva: a-laptop, b-laptop and c-laptop don't have its repo"


def test_reason_changes_the_fingerprint(fleet):
    none = _raises()["agent:eva:routing"].digest()
    laptop = _runner(fleet, "jj-laptop", kind=Runner.EMDASH, projects=["x"])
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=laptop, rank=0)
    assert _raises()["agent:eva:routing"].digest() != none


def test_one_working_runner_is_enough(fleet):
    laptop = _runner(fleet, "jj-laptop", kind=Runner.EMDASH, projects=["x"])
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=laptop, rank=0)
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=_runner(fleet, "cloud"), rank=1)
    assert _raises() == {}


def test_actions_are_for_agent_editors(fleet):
    alerts.tick(timezone.now())
    a = Alert.objects.get(kind=src.KIND)
    acts = src.actions(a, fleet["ed"])
    assert [x.id for x in acts] == ["routing", "dismiss"]
    assert acts[0].closes and acts[0].href == "/w/dimagi/agents/eva/settings"
    assert src.actions(a, fleet["viewer"]) == []
    assert src.CLOSING_ACTION_IDS == ("routing", "dismiss")


def test_only_rule_routes_do_not_count(fleet):
    RunnerAssignment.objects.create(agent=fleet["eva"], runner=_runner(fleet, "box"), rank=0, source="email")
    assert _raises()["agent:eva:routing"].title == "Eva has no runner to run on"


def test_directed_only_runner_raises_its_own_reason(fleet):
    box = _runner(fleet, "pinned-box")
    box.capabilities = {"agents": [], "directed": True}
    box.save(update_fields=["capabilities"])
    RunnerAssignment.objects.create(agent=fleet["eva"], runner=box, rank=0)
    r = _raises()["agent:eva:routing"]
    assert r.title == "Eva's runners (pinned-box) only take directed turns"
    Agent.objects.create(slug="hal", name="Hal", workspace=fleet["ws"], owner=fleet["jj"])
    assert _raises()["agent:hal:routing"].digest() != r.digest()  # different reason, different fingerprint


def test_inherited_directed_only_runner_raises_its_own_reason(fleet):
    box = _runner(fleet, "pinned-box")
    box.capabilities = {"agents": [], "directed": True}
    box.save(update_fields=["capabilities"])
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=box, rank=0)
    assert _raises()["agent:eva:routing"].title == "Eva's runners (pinned-box) only take directed turns"


def test_cannot_hold_raises_its_own_reason(fleet):
    stranger = _user("stranger")
    box = Runner.objects.create(name="their-box", kind=Runner.CLOUD, host="their-box", owner=stranger,
                                workspace=fleet["ws"], status=Runner.ONLINE,
                                last_heartbeat_at=timezone.now(), capabilities={"agents": []})
    WorkspaceRunnerOrder.objects.create(workspace=fleet["ws"], runner=box, rank=0)
    r = _raises()["agent:eva:routing"]
    assert r.title == "Eva can't use their-box: their owner can't hold its turns"

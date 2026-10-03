"""An agent with no default order of its own follows its workspace's.

Cloud boxes get replaced; with every agent holding its own copy of "jj → hal →
cloud", each replacement meant editing every agent (2026-10-03). So an agent with
no default order follows its workspace's order — or the nearest ancestor's — live,
and its source rules still come first.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents import services as agent_services
from apps.agents.models import Agent
from apps.harness import initiator as _initiator
from apps.harness import services
from apps.harness.models import Runner, RunnerAssignment, Turn, WorkspaceRunnerOrder
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

_BY_CANOPY = _initiator.system(via="test")


def _user(name):
    return get_user_model().objects.create_user(username=name, email=f"{name}@dimagi.com")


def _runner(owner, name, *, kind=Runner.EMDASH, projects=("hal", "ada", "jarvis")):
    return Runner.objects.create(
        name=name, kind=kind, host=name, owner=owner, status=Runner.ONLINE,
        last_heartbeat_at=timezone.now(), capabilities={"projects": list(projects)},
    )


def _order(ws, *runners):
    for i, r in enumerate(runners):
        WorkspaceRunnerOrder.objects.create(workspace=ws, runner=r, rank=i)


def _enqueue(agent, key="t1", origin=Turn.ORIGIN_API, age_seconds=0):
    turn, _ = services.enqueue_turn(initiator=_BY_CANOPY, agent=agent, origin=origin,
                                    idempotency_key=key, prompt="go")
    if age_seconds:
        Turn.objects.filter(pk=turn.pk).update(
            created_at=timezone.now() - dt.timedelta(seconds=age_seconds))
    return turn


@pytest.fixture
def tree():
    """Dimagi (owned by jj) with Connect below it; jj's three boxes."""
    jj = _user("jj")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(workspace=dimagi, user=jj, role=WorkspaceMembership.OWNER)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj,
                                       parent=dimagi)
    boxes = (_runner(jj, "jj-mbp"), _runner(jj, "hal-mbp"),
             _runner(jj, "cloud-1", kind=Runner.CLOUD, projects=("canopy-web",)))
    return jj, dimagi, connect, boxes


def test_an_agent_in_a_division_follows_the_parents_order(tree):
    _jj, dimagi, connect, (jj_box, hal_box, _cloud) = tree
    _order(dimagi, jj_box, hal_box)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    turn = _enqueue(hal)
    assert services.claim_next_turn(hal_box) is None  # rank 1 waits for rank 0
    assert services.claim_next_turn(jj_box).pk == turn.pk


def test_it_falls_through_the_inherited_order_like_its_own(tree):
    _jj, dimagi, connect, (jj_box, _hal_box, cloud) = tree
    _order(dimagi, jj_box, cloud)
    Runner.objects.filter(pk=jj_box.pk).update(paused=True)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    turn = _enqueue(hal)
    assert services.claim_next_turn(cloud).pk == turn.pk


def test_the_nearest_workspace_with_an_order_wins(tree):
    _jj, dimagi, connect, (jj_box, hal_box, _cloud) = tree
    _order(dimagi, jj_box)
    _order(connect, hal_box)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=connect)
    turn = _enqueue(ada)
    assert services.claim_next_turn(jj_box) is None
    assert services.claim_next_turn(hal_box).pk == turn.pk


def test_an_agent_with_its_own_order_does_not_follow(tree):
    _jj, dimagi, connect, (jj_box, hal_box, _cloud) = tree
    _order(dimagi, jj_box)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=connect)
    RunnerAssignment.objects.create(agent=ada, runner=hal_box, rank=0)
    _enqueue(ada, age_seconds=3600)
    assert services.claim_next_turn(jj_box) is None


def test_an_order_switched_entirely_off_is_still_its_own(tree):
    """An owner who disabled every runner chose a list; falling back to the
    workspace's would route work they parked."""
    _jj, dimagi, connect, (jj_box, hal_box, _cloud) = tree
    _order(dimagi, jj_box)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=connect)
    RunnerAssignment.objects.create(agent=ada, runner=hal_box, rank=0, enabled=False)
    _enqueue(ada, age_seconds=3600)
    assert services.claim_next_turn(jj_box) is None


def test_source_rules_still_come_first(tree):
    _jj, dimagi, connect, (jj_box, hal_box, cloud) = tree
    _order(dimagi, jj_box, hal_box)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    RunnerAssignment.objects.create(agent=hal, runner=cloud, rank=0, source=Turn.ORIGIN_API,
                                    strict=True)
    turn = _enqueue(hal, age_seconds=3600)
    assert services.claim_next_turn(jj_box) is None
    assert services.claim_next_turn(cloud).pk == turn.pk


def test_a_laptop_without_the_agents_repo_is_skipped_not_waited_on(tree):
    """jj has no `muse` checkout. Listed first, it must not hold Muse's work
    for the cascade grace — and the cloud, which sets agents up itself, takes it."""
    _jj, dimagi, connect, (jj_box, _hal_box, cloud) = tree
    _order(dimagi, jj_box, cloud)
    muse = Agent.objects.create(slug="muse", name="Muse", workspace=connect)
    turn = _enqueue(muse)
    inh = services.inherited_orders([muse.pk])[muse.pk]
    assert inh.missing_repo == [jj_box] and inh.runners == [cloud]
    assert services.claim_next_turn(jj_box) is None
    assert services.claim_next_turn(cloud).pk == turn.pk


def test_a_box_whose_owner_cannot_hold_the_agent_is_dropped(tree):
    jj, dimagi, connect, (jj_box, _hal_box, _cloud) = tree
    editor = _user("ed")
    WorkspaceMembership.objects.create(workspace=dimagi, user=editor, role=WorkspaceMembership.EDITOR)
    WorkspaceMembership.objects.create(workspace=connect, user=editor, role=WorkspaceMembership.EDITOR)
    ed_box = _runner(editor, "ed-mbp")
    _order(dimagi, ed_box, jj_box)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=jj)
    turn = _enqueue(hal)
    inh = services.inherited_orders([hal.pk])[hal.pk]
    assert inh.cannot_hold == [ed_box] and inh.runners == [jj_box]
    assert services.claim_next_turn(ed_box) is None
    assert services.claim_next_turn(jj_box).pk == turn.pk


def test_a_box_in_the_inherited_order_may_fetch_the_agents_secrets(tree):
    jj, dimagi, connect, (jj_box, _hal_box, _cloud) = tree
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    assert not agent_services.caller_runs_agent(jj, hal)
    _order(dimagi, jj_box)
    assert agent_services.caller_runs_agent(jj, hal)


def test_the_default_order_endpoint_says_what_it_follows(tree):
    jj, dimagi, connect, (jj_box, _hal_box, cloud) = tree
    _order(dimagi, jj_box, cloud)
    Agent.objects.create(slug="muse", name="Muse", workspace=connect)
    c = Client()
    c.force_login(jj)
    body = c.get("/api/w/connect/agents/muse/default-order").json()
    assert body["own"] is False and body["workspace"] == "dimagi"
    assert [r["runner_name"] for r in body["runners"]] == ["cloud-1"]
    assert body["missing_repo"] == ["jj-mbp"]


def test_saving_an_empty_list_makes_an_agent_follow_again(tree):
    jj, dimagi, connect, (jj_box, hal_box, _cloud) = tree
    _order(dimagi, jj_box)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=jj)
    RunnerAssignment.objects.create(agent=hal, runner=hal_box, rank=0)
    rule = RunnerAssignment.objects.create(agent=hal, runner=hal_box, rank=0, source="email")
    c = Client()
    c.force_login(jj)
    r = c.put("/api/w/connect/agents/hal/runners", {"runners": []}, content_type="application/json")
    assert r.status_code == 200
    assert c.get("/api/w/connect/agents/hal/default-order").json()["workspace"] == "dimagi"
    assert RunnerAssignment.objects.filter(pk=rule.pk).exists()  # rules untouched


def test_only_a_workspace_owner_sets_the_order(tree):
    """It routes agents in every division below too, and only ownership flows
    down the tree."""
    jj, dimagi, _connect, (jj_box, _hal_box, _cloud) = tree
    editor = _user("ed")
    WorkspaceMembership.objects.create(workspace=dimagi, user=editor, role=WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(editor)
    body = {"runners": [{"runner_id": str(jj_box.pk)}]}
    assert c.put("/api/workspaces/dimagi/runner-order", body, content_type="application/json").status_code in (403, 404)
    assert not WorkspaceRunnerOrder.objects.exists()
    c.force_login(jj)
    assert c.put("/api/workspaces/dimagi/runner-order", body, content_type="application/json").status_code == 200


def test_the_topology_marks_inherited_routes(tree):
    jj, dimagi, connect, (jj_box, hal_box, _cloud) = tree
    _order(dimagi, jj_box, hal_box)
    Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    c = Client()
    c.force_login(jj)
    body = c.get("/api/workspaces/dimagi/runner-topology").json()
    ws = {w["slug"]: w for w in body["workspaces"]}
    assert [r["runner_id"] for r in ws["dimagi"]["order"]] == [str(jj_box.pk), str(hal_box.pk)]
    assert ws["connect"]["order_from"] == "dimagi"
    hal = ws["connect"]["agents"][0]
    assert hal["follows"]["workspace"] == "dimagi"
    assert [r["inherited"] for r in hal["routes"]] == [True, True]


def test_an_agent_with_its_own_order_is_told_what_it_would_follow(tree):
    jj, dimagi, connect, (jj_box, hal_box, _cloud) = tree
    _order(dimagi, jj_box)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect)
    RunnerAssignment.objects.create(agent=hal, runner=hal_box, rank=0)
    c = Client()
    c.force_login(jj)
    body = c.get("/api/w/connect/agents/hal/default-order").json()
    assert body["own"] is True and body["workspace"] == "dimagi" and body["runners"] == []


def test_a_divisions_agent_takes_its_owners_box_from_the_parent_workspace(tree):
    """The fleet's boxes live in `dimagi`, its agents in `connect`. Saving an
    agent's own list through the tenant URL refused the caller's OWN box because
    it lived one workspace up (found on labs, 2026-10-03)."""
    jj, _dimagi, connect, (_jj_box, _hal_box, cloud) = tree
    Runner.objects.filter(pk=cloud.pk).update(workspace_id="dimagi")
    Agent.objects.create(slug="echo", name="Echo", workspace=connect, owner=jj)
    c = Client()
    c.force_login(jj)
    r = c.put("/api/w/connect/agents/echo/runners",
              {"runners": [{"runner_id": str(cloud.pk), "enabled": True}]}, content_type="application/json")
    assert r.status_code == 200, r.content
    assert [x["runner_name"] for x in r.json()] == ["cloud-1"]


def test_someone_elses_box_in_the_parent_is_still_refused(tree):
    jj, dimagi, connect, _boxes = tree
    other = _user("other")
    WorkspaceMembership.objects.create(workspace=dimagi, user=other, role=WorkspaceMembership.EDITOR)
    box = _runner(other, "other-mbp")
    Runner.objects.filter(pk=box.pk).update(workspace_id="dimagi")
    Agent.objects.create(slug="echo", name="Echo", workspace=connect, owner=jj)
    c = Client()
    c.force_login(jj)
    r = c.put("/api/w/connect/agents/echo/runners",
              {"runners": [{"runner_id": str(box.pk), "enabled": True}]}, content_type="application/json")
    assert r.status_code == 422

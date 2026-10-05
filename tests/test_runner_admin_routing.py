"""A runner ADMIN may route agents' work onto the box (owner decision 2026-10-05).

`PUT /api/agents/{slug}/runners` and `PUT /api/agents/{slug}/runner-rules` used
to accept only runners the caller OWNED, so the fleet's conductor (an agent login
holding RunnerAdmin grants on every box, but pairing none of them) could not
rebuild a routing it was asked to fix. The runner side asks "owns it, or
administers it" of every runner a write ADDS (`_runners_for_routing`, #1144);
the agent side is unchanged — editor tier to route, agent admin for
`turn_mode: auto` — and the box must still be able to hold the agent
(`runner_may_hold_agent`). This file pins the owner's decision end to end, for
the default list as well as the rules, and that administering a box is still
not BEING it.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent
from apps.harness.models import Runner, RunnerAdmin, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def world():
    jj = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ada = User.objects.create_user("ada", "ada@dimagi-ai.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    WorkspaceMembership.objects.create(user=jj, workspace=ws, role=WorkspaceMembership.OWNER)
    # An editor: may route the agent, may not set `auto` on it.
    WorkspaceMembership.objects.create(user=ada, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="echo", name="Echo", workspace=ws)
    # jj owns both boxes (and, as workspace owner, may hold the agent).
    cloud = Runner.objects.create(name="cloud-ec2-1", kind=Runner.CLOUD, owner=jj, workspace=ws)
    laptop = Runner.objects.create(name="jj-laptop", kind=Runner.EMDASH, owner=jj, workspace=ws)
    RunnerAdmin.objects.create(runner=cloud, user=ada, granted_by=jj)
    c = Client()
    return {"jj": jj, "ada": ada, "ws": ws, "agent": agent, "cloud": cloud,
            "laptop": laptop, "client": c}


def _put_runners(c, slug, *runners):
    return c.put(f"/api/agents/{slug}/runners",
                 data=json.dumps({"runners": [{"runner_id": str(r.id), "enabled": True} for r in runners]}),
                 content_type="application/json")


def _put_rules(c, slug, *runners, turn_mode=""):
    body = {"rules": [{"source": "slack", "runners": [{"runner_id": str(r.id)} for r in runners],
                       "turn_mode": turn_mode}]}
    return c.put(f"/api/agents/{slug}/runner-rules", data=json.dumps(body),
                 content_type="application/json")


def test_runner_admin_can_set_the_default_order_naming_a_box_they_administer(world):
    world["client"].force_login(world["ada"])
    r = _put_runners(world["client"], "echo", world["cloud"])
    assert r.status_code == 200, r.content
    assert list(RunnerAssignment.objects.filter(agent=world["agent"], source="")
                .values_list("runner_id", flat=True)) == [world["cloud"].id]


def test_runner_admin_cannot_name_a_box_they_neither_own_nor_administer(world):
    """Same tenant, visible in the fleet list — but no grant: 403 naming the box
    (it is visible, so naming it leaks nothing), and nothing is written."""
    world["client"].force_login(world["ada"])
    r = _put_runners(world["client"], "echo", world["cloud"], world["laptop"])
    assert r.status_code == 403, r.content
    assert "jj-laptop" in r.json()["detail"]
    assert not RunnerAssignment.objects.filter(agent=world["agent"]).exists()


def test_runner_admin_can_set_a_source_rule_naming_a_box_they_administer(world):
    world["client"].force_login(world["ada"])
    r = _put_rules(world["client"], "echo", world["cloud"], turn_mode="manual")
    assert r.status_code == 200, r.content
    assert RunnerAssignment.objects.filter(agent=world["agent"], source="slack",
                                           runner=world["cloud"]).exists()


def test_runner_admin_rule_naming_an_unadministered_box_is_403(world):
    world["client"].force_login(world["ada"])
    r = _put_rules(world["client"], "echo", world["laptop"])
    assert r.status_code == 403, r.content
    assert not RunnerAssignment.objects.filter(agent=world["agent"]).exists()


def test_an_auto_rule_still_needs_the_agents_admin(world):
    """Administering the BOX says nothing about the AGENT: `auto` is the agent
    admins' to set (#1106), runner admin or not."""
    world["client"].force_login(world["ada"])
    r = _put_rules(world["client"], "echo", world["cloud"], turn_mode="auto")
    assert r.status_code == 403, r.content
    assert "auto" in r.json()["detail"]
    assert not RunnerAssignment.objects.filter(agent=world["agent"]).exists()

    from apps.agents.models import AgentAdmin

    AgentAdmin.objects.get_or_create(agent=world["agent"], user=world["ada"])
    r = _put_rules(world["client"], "echo", world["cloud"], turn_mode="auto")
    assert r.status_code == 200, r.content


def test_a_box_that_cannot_hold_the_agent_is_still_refused(world):
    """Being the box's admin does not make the box fit for the agent: a runner
    owned by someone who is no admin of the agent stays a 403."""
    stranger = User.objects.create_user("st", "st@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=stranger, workspace=world["ws"],
                                       role=WorkspaceMembership.EDITOR)
    theirs = Runner.objects.create(name="st-laptop", kind=Runner.EMDASH, owner=stranger,
                                   workspace=world["ws"])
    RunnerAdmin.objects.create(runner=theirs, user=world["ada"], granted_by=stranger)
    world["client"].force_login(world["ada"])
    r = _put_runners(world["client"], "echo", theirs)
    assert r.status_code == 403, r.content
    assert "cannot run echo" in r.json()["detail"]


def test_a_grant_on_an_unhomed_runner_does_not_route(world):
    """A runner with no workspace has no tenant to share: only its owner reaches it."""
    legacy = Runner.objects.create(name="legacy", kind=Runner.EMDASH, owner=world["jj"],
                                   workspace=None)
    RunnerAdmin.objects.create(runner=legacy, user=world["ada"], granted_by=world["jj"])
    world["client"].force_login(world["ada"])
    r = _put_runners(world["client"], "echo", legacy)
    assert r.status_code == 422, r.content


def test_a_retired_box_is_still_unknown_to_its_admin(world):
    world["cloud"].status = Runner.RETIRED
    world["cloud"].save(update_fields=["status"])
    world["client"].force_login(world["ada"])
    r = _put_runners(world["client"], "echo", world["cloud"])
    assert r.status_code == 422, r.content


def test_the_owner_is_unchanged(world):
    world["client"].force_login(world["jj"])
    r = _put_runners(world["client"], "echo", world["cloud"], world["laptop"])
    assert r.status_code == 200, r.content
    r = _put_rules(world["client"], "echo", world["laptop"], world["cloud"], turn_mode="auto")
    assert r.status_code == 200, r.content


def test_a_runner_admin_is_still_not_the_runner(world):
    """Routing widened; speaking FOR the box did not. Heartbeat and the
    credential fetch still 404 for an admin."""
    world["client"].force_login(world["ada"])
    rid = world["cloud"].id
    r = world["client"].post(f"/api/harness/runners/{rid}/heartbeat",
                             data=json.dumps({}), content_type="application/json")
    assert r.status_code == 404, r.content
    r = world["client"].get(f"/api/harness/runners/{rid}/credential")
    assert r.status_code == 404, r.content

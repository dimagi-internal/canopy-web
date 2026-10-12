"""Every routing write leaves a record of who changed what, with the rows before
and after (`apps/harness/routing_audit.py` → the settings audit, `apps/events/audit.py`).

Origin, 2026-10-12: ACE carried a strict rule sending every API turn ada@
requested to one cloud box, and nobody could say who had set it. The rows carry
no author, each save deletes and re-creates them, and only MCP-originated writes
left even a tool-call line. These tests pin that each routing write path records
one SettingChange naming the caller with the rows before and after, that a no-op
save records nothing, and that a refused write records nothing.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.events.models import SettingChange
from apps.harness.models import Runner, RunnerAssignment
from apps.workspaces.models import Workspace, WorkspaceMembership
from apps.workspaces.testing import enrolled

pytestmark = pytest.mark.django_db


@pytest.fixture
def fleet():
    User = get_user_model()
    jj = User.objects.create_user(username="jj", email="jj@dimagi.com")
    other = User.objects.create_user(username="st", email="stewari@dimagi.com")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj)
    WorkspaceMembership.objects.create(workspace=ws, user=jj, role=WorkspaceMembership.OWNER)
    WorkspaceMembership.objects.create(workspace=ws, user=other, role=WorkspaceMembership.EDITOR)
    ace = Agent.objects.create(slug="ace", name="ACE", workspace=ws)
    now = timezone.now()
    laptop = enrolled(Runner.objects.create(
        name="jj-mbp", kind=Runner.EMDASH, owner=jj, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=now, capabilities={},
    ))
    cloud = enrolled(Runner.objects.create(
        name="cloud-ec2-2", kind=Runner.CLOUD, owner=jj, workspace=ws,
        status=Runner.ONLINE, last_heartbeat_at=now, capabilities={},
    ))
    RunnerAssignment.objects.create(agent=ace, runner=laptop, rank=0)
    c = Client()
    c.force_login(jj)
    return {"c": c, "jj": jj, "other": other, "ws": ws, "ace": ace, "laptop": laptop, "cloud": cloud}


def _events(op=""):
    """The audit rows for routes whose name starts with `op`, oldest first."""
    return list(SettingChange.objects.filter(operation__startswith=op).order_by("id"))


def _change(ev):
    [change] = ev.changes
    return change


def _put(c, path, body):
    return c.put(path, data=json.dumps(body), content_type="application/json")


def test_setting_and_removing_a_persons_route_is_recorded(fleet):
    c, cloud = fleet["c"], fleet["cloud"]
    res = _put(c, "/api/agents/ace/actor-routes/ada@dimagi-ai.com",
               {"runners": [{"runner_id": str(cloud.id)}], "sources": ["api"]})
    assert res.status_code == 200, res.content
    [ev] = _events("set_agent_actor_route")
    assert ev.workspace == fleet["ws"]
    assert "jj@dimagi.com set ace route for ada@dimagi-ai.com → cloud-ec2-2" == ev.summary
    assert ev.actor_email == "jj@dimagi.com"
    assert ev.via == "ui"
    assert _change(ev)["before"] == []
    assert [(r["source"], r["actor"], r["runner"], r["strict"]) for r in _change(ev)["after"]] == [
        ("api", "ada@dimagi-ai.com", "cloud-ec2-2", True)]

    assert c.delete("/api/agents/ace/actor-routes/ada@dimagi-ai.com").status_code == 204
    [gone] = _events("delete_agent_actor_route")
    assert gone.summary == "jj@dimagi.com removed ace route for ada@dimagi-ai.com"
    assert _change(gone)["after"] == []
    assert _change(gone)["before"][0]["runner"] == "cloud-ec2-2"


def test_one_rule_set_and_delete_are_recorded(fleet):
    c, cloud = fleet["c"], fleet["cloud"]
    path = "/api/agents/ace/runner-rules/email?actor=alice@dimagi.com"
    assert _put(c, path, {"runners": [{"runner_id": str(cloud.id)}], "strict": True}).status_code == 200
    [ev] = _events("set_agent_runner_rule")
    assert _change(ev)["scope"] == "ace email rule for alice@dimagi.com"
    assert c.delete(path).status_code == 204
    [gone] = _events("delete_agent_runner_rule")
    assert _change(gone)["before"] and _change(gone)["after"] == []


def test_resending_a_rule_unchanged_records_nothing(fleet):
    c, cloud = fleet["c"], fleet["cloud"]
    path = "/api/agents/ace/runner-rules/slack"
    body = {"runners": [{"runner_id": str(cloud.id)}], "strict": False}
    assert _put(c, path, body).status_code == 200
    assert _put(c, path, body).status_code == 200
    assert len(_events("set_agent_runner_rule")) == 1
    # Deleting something that isn't there is idempotent — and not a change.
    assert c.delete("/api/agents/ace/runner-rules/email").status_code == 204
    assert _events("delete_agent_runner_rule") == []


def test_default_order_change_is_recorded_with_before_and_after(fleet):
    c, laptop, cloud = fleet["c"], fleet["laptop"], fleet["cloud"]
    res = _put(c, "/api/agents/ace/runners", {"runners": [
        {"runner_id": str(cloud.id)}, {"runner_id": str(laptop.id), "enabled": False}]})
    assert res.status_code == 200, res.content
    [ev] = _events("replace_agent_runners")
    assert [r["runner"] for r in _change(ev)["before"]] == ["jj-mbp"]
    assert [(r["runner"], r["rank"], r["enabled"]) for r in _change(ev)["after"]] == [
        ("cloud-ec2-2", 0, True), ("jj-mbp", 1, False)]
    assert ev.summary == "jj@dimagi.com changed ace default runner order → cloud-ec2-2, jj-mbp"


def test_wholesale_rules_replace_is_recorded(fleet):
    c, cloud = fleet["c"], fleet["cloud"]
    res = _put(c, "/api/agents/ace/runner-rules", {"rules": [
        {"source": "email", "actor": "", "strict": True, "runners": [{"runner_id": str(cloud.id)}]}]})
    assert res.status_code == 200, res.content
    [ev] = _events("replace_agent_runner_rules")
    assert [r["source"] for r in _change(ev)["after"]] == ["email"]


def test_workspace_order_change_is_recorded(fleet):
    c, laptop = fleet["c"], fleet["laptop"]
    res = _put(c, "/api/workspaces/connect/runner-order", {"runners": [{"runner_id": str(laptop.id)}]})
    assert res.status_code == 200, res.content
    [ev] = _events("set_runner_order")
    assert ev.workspace == fleet["ws"]
    assert [r["runner"] for r in _change(ev)["after"]] == ["jj-mbp"]


def test_a_refused_write_records_nothing(fleet):
    """An editor who doesn't administer the box is refused — and no record says
    the rule changed."""
    c = Client()
    c.force_login(fleet["other"])
    res = _put(c, "/api/agents/ace/actor-routes/alice@dimagi.com",
               {"runners": [{"runner_id": str(fleet["cloud"].id)}]})
    assert res.status_code in (403, 404), res.content
    assert _events() == []


def test_the_record_is_readable_from_the_events_api(fleet):
    c, cloud = fleet["c"], fleet["cloud"]
    _put(c, "/api/agents/ace/actor-routes/ada@dimagi-ai.com",
         {"runners": [{"runner_id": str(cloud.id)}], "sources": ["api"]})
    res = c.get("/api/events/setting-changes?operation=set_agent")
    assert res.status_code == 200, res.content
    [item] = res.json()["items"]
    assert item["operation"] == "set_agent_actor_route"
    assert item["workspace"] == "connect"
    assert item["changes"][0]["after"][0]["runner"] == "cloud-ec2-2"

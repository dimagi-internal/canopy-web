"""`/api/attention/` — what is waiting on you, in one list, and hiding things from it.

Each source keeps its owner's rule (the board's waiting-on-you filter, the
session feed's `feed.status`, `members.manage`), so these tests pin that the
list agrees with those routes — and that hiding something hides it only until
something new happens on it.
"""
from __future__ import annotations

import datetime as dt

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentTask
from apps.attention import services
from apps.canopy_sessions.models import RunnerBinding, Session
from apps.harness.models import Runner
from apps.workspaces.models import Workspace, WorkspaceAccessRequest, WorkspaceMembership

pytestmark = pytest.mark.django_db

MENU = {"question": "Proceed?", "options": [{"number": 1, "label": "Yes"}], "source": "transcript"}


@pytest.fixture()
def jj():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    c = Client()
    c.force_login(u)
    return {"user": u, "ws": ws, "agent": agent, "client": c}


def _keys(resp, group="now"):
    assert resp.status_code == 200, resp.content
    return [i["key"] for i in resp.json()[group]]


def test_tasks_waiting_on_you_with_their_actions(jj):
    AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="Send EOI?", ask_kind="review", ask_body="ok?")
    AgentTask.objects.create(agent=jj["agent"], ext_id="T2", title="numbers", waiting_on_user=jj["user"])
    AgentTask.objects.create(agent=jj["agent"], ext_id="T3", title="plain work")
    items = {i["key"]: i for i in jj["client"].get("/api/attention/").json()["now"]}
    assert set(items) == {"task:eva:T1", "task:eva:T2"}
    review = items["task:eva:T1"]
    assert review["actions"] == ["approve", "decline", "reply"]
    assert review["target"] == {"agent": "eva", "task": "T1"}
    assert review["url"] == "/w/connect/agents/eva/tasks?waiting=me"
    assert review["reason"] == "Approve or decline" and review["body"] == "ok?"
    assert items["task:eva:T2"]["reason"] == "Parked on you"


def test_a_chat_blocked_on_you_is_listed(jj):
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=jj["user"],
                                   workspace=jj["ws"], status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    s = Session.objects.create(workspace=jj["ws"], agent=jj["agent"], title="spark", origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=s, runner=runner, session_key="spark",
                                 live_seen_at=timezone.now(), pending_question=MENU)
    item = next(i for i in jj["client"].get("/api/attention/").json()["now"] if i["kind"] == "session")
    assert item["key"] == f"session:{s.id}" and item["url"] == f"/w/connect/chat/{s.id}"
    assert item["reason"] == "Needs an answer"


def test_access_requests_reach_only_those_who_manage_members(jj):
    asker = User.objects.create_user("ann", "ann@dimagi.com", "pw")
    req = WorkspaceAccessRequest.objects.create(workspace=jj["ws"], user=asker, note="hi")
    assert f"access:{req.pk}" in _keys(jj["client"].get("/api/attention/"))
    viewer = User.objects.create_user("vi", "vi@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=jj["ws"], role=WorkspaceMembership.VIEWER)
    vc = Client()
    vc.force_login(viewer)
    assert f"access:{req.pk}" not in _keys(vc.get("/api/attention/"))


def test_never_another_workspaces_items(jj):
    other = User.objects.create_user("x", "x@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="Other", created_by=other)
    zed = Agent.objects.create(slug="zed", name="Zed", workspace=ws2, owner=other)
    AgentTask.objects.create(agent=zed, ext_id="T1", title="secret?", ask_kind="review")
    assert jj["client"].get("/api/attention/").json() == {"now": [], "quiet": [], "hidden": []}
    assert jj["client"].get("/api/w/other/attention/").status_code == 404


def test_untouched_items_go_quiet_newest_first(jj):
    old = AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="old", ask_kind="question")
    AgentTask.objects.create(agent=jj["agent"], ext_id="T2", title="new", ask_kind="question")
    AgentTask.objects.filter(pk=old.pk).update(updated_at=timezone.now() - dt.timedelta(days=10))
    body = jj["client"].get("/api/attention/").json()
    assert [i["key"] for i in body["now"]] == ["task:eva:T2"]
    assert [i["key"] for i in body["quiet"]] == ["task:eva:T1"]


def test_hiding_lasts_until_something_new_happens(jj):
    t = AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="q", ask_kind="question")
    c = jj["client"]
    r = c.post("/api/attention/hide", {"keys": ["task:eva:T1"]}, content_type="application/json")
    assert r.status_code == 200 and r.json() == {"count": 1}
    assert _keys(c.get("/api/attention/")) == []
    assert _keys(c.get("/api/attention/"), "hidden") == ["task:eva:T1"]
    # Nothing was decided: the task is exactly as it was.
    t.refresh_from_db()
    assert t.ask_is_open and t.status == AgentTask._meta.get_field("status").default
    # Something new on it brings it back.
    AgentTask.objects.filter(pk=t.pk).update(updated_at=timezone.now() + dt.timedelta(seconds=5))
    assert _keys(c.get("/api/attention/")) == ["task:eva:T1"]


def test_hide_for_days_and_unhide(jj):
    AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="q", ask_kind="question")
    c = jj["client"]
    c.post("/api/attention/hide", {"keys": ["task:eva:T1"], "days": 3}, content_type="application/json")
    assert _keys(c.get("/api/attention/")) == []
    assert c.delete("/api/attention/hide/task:eva:T1").json() == {"count": 1}
    assert _keys(c.get("/api/attention/")) == ["task:eva:T1"]


def test_hiding_is_only_ever_your_own_list(jj):
    AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="q", ask_kind="question")
    services.hide(jj["user"], ["task:eva:T1"])
    peer = User.objects.create_user("pe", "pe@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=peer, workspace=jj["ws"], role=WorkspaceMembership.EDITOR)
    pc = Client()
    pc.force_login(peer)
    # An unrouted open ask is in every member's list; jj hiding it leaves peer's alone.
    assert _keys(pc.get("/api/attention/")) == ["task:eva:T1"]

"""`/api/me/attention/` — what is waiting on you, in one list, and hiding things from it.

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
    items = {i["key"]: i for i in jj["client"].get("/api/me/attention/").json()["now"]}
    assert set(items) == {"task:eva:T1", "task:eva:T2"}
    review = items["task:eva:T1"]
    assert review["actions"] == ["approve", "decline", "reply"]
    assert review["target"] == {"agent": "eva", "task": "T1", "key": "CON-1"}
    assert review["url"] == "/w/connect/tasks/CON-1"
    assert review["reason"] == "Approve or decline" and review["body"] == "ok?"
    assert review["state"] == "asking"
    assert items["task:eva:T2"]["state"] == "your_move"


def test_a_chat_blocked_on_you_is_listed(jj):
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=jj["user"],
                                   workspace=jj["ws"], status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    s = Session.objects.create(workspace=jj["ws"], agent=jj["agent"], title="spark", origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=s, runner=runner, session_key="spark",
                                 live_seen_at=timezone.now(), pending_question=MENU)
    item = next(i for i in jj["client"].get("/api/me/attention/").json()["now"] if i["kind"] == "session")
    assert item["key"] == f"session:{s.id}" and item["url"] == f"/w/connect/chat/{s.id}"
    assert item["reason"] == "Needs an answer" and item["state"] == "asking"
    assert item["organisation"] == "connect"


def test_a_chat_held_behind_a_paused_runner_is_listed_as_parked(jj):
    runner = Runner.objects.create(name="jj-mbp", kind=Runner.EMDASH, host="jj-mac", owner=jj["user"],
                                   workspace=jj["ws"], status=Runner.ONLINE, last_heartbeat_at=timezone.now(),
                                   paused=True)
    s = Session.objects.create(workspace=jj["ws"], agent=jj["agent"], title="spark", origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=s, runner=runner, session_key="spark",
                                 live_seen_at=timezone.now(), pending_question=MENU)
    item = next(i for i in jj["client"].get("/api/me/attention/").json()["now"] if i["kind"] == "session")
    assert item["state"] == "parked"
    assert item["reason"] == "Waiting — jj-mbp is paused"


def _working_session(jj, *, owner, title="busy"):
    runner = Runner.objects.create(name=f"{owner.username}-mbp", kind=Runner.EMDASH, host="mac", owner=owner,
                                   workspace=jj["ws"], status=Runner.ONLINE, last_heartbeat_at=timezone.now())
    s = Session.objects.create(workspace=jj["ws"], agent=jj["agent"], title=title, origin=Session.ORIGIN_RUNNER)
    RunnerBinding.objects.create(session=s, runner=runner, session_key=title, live_seen_at=timezone.now(),
                                 agent_status="working")
    return s


def test_your_running_session_is_listed_apart_not_as_an_ask(jj):
    s = _working_session(jj, owner=jj["user"])
    body = jj["client"].get("/api/me/attention/").json()
    assert [i["key"] for i in body["running"]] == [f"session:{s.id}"]
    assert body["running"][0]["reason"] == "Running" and body["running"][0]["state"] == "running"
    assert all(i["kind"] != "session" for i in body["now"] + body["quiet"])


def test_a_colleagues_running_session_is_not_yours(jj):
    peer = User.objects.create_user("pe", "pe@dimagi.com", "pw")
    _working_session(jj, owner=peer)
    assert jj["client"].get("/api/me/attention/").json()["running"] == []


def test_access_requests_reach_only_those_who_manage_members(jj):
    asker = User.objects.create_user("ann", "ann@dimagi.com", "pw")
    req = WorkspaceAccessRequest.objects.create(workspace=jj["ws"], user=asker, note="hi")
    assert f"access:{req.pk}" in _keys(jj["client"].get("/api/me/attention/"))
    viewer = User.objects.create_user("vi", "vi@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=jj["ws"], role=WorkspaceMembership.VIEWER)
    vc = Client()
    vc.force_login(viewer)
    assert f"access:{req.pk}" not in _keys(vc.get("/api/me/attention/"))


def test_never_another_workspaces_items(jj):
    other = User.objects.create_user("x", "x@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="Other", created_by=other)
    zed = Agent.objects.create(slug="zed", name="Zed", workspace=ws2, owner=other)
    AgentTask.objects.create(agent=zed, ext_id="T1", title="secret?", ask_kind="review")
    assert jj["client"].get("/api/me/attention/").json() == {"now": [], "quiet": [], "hidden": [], "running": []}
    assert jj["client"].get("/api/w/other/me/attention/").status_code == 404


def test_untouched_items_go_quiet_newest_first(jj):
    old = AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="old", ask_kind="question")
    AgentTask.objects.create(agent=jj["agent"], ext_id="T2", title="new", ask_kind="question")
    AgentTask.objects.filter(pk=old.pk).update(updated_at=timezone.now() - dt.timedelta(days=10))
    body = jj["client"].get("/api/me/attention/").json()
    assert [i["key"] for i in body["now"]] == ["task:eva:T2"]
    assert [i["key"] for i in body["quiet"]] == ["task:eva:T1"]


def test_hiding_lasts_until_something_new_happens(jj):
    t = AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="q", ask_kind="question")
    c = jj["client"]
    r = c.post("/api/me/attention/hide", {"keys": ["task:eva:T1"]}, content_type="application/json")
    assert r.status_code == 200 and r.json() == {"count": 1}
    assert _keys(c.get("/api/me/attention/")) == []
    assert _keys(c.get("/api/me/attention/"), "hidden") == ["task:eva:T1"]
    # Nothing was decided: the task is exactly as it was.
    t.refresh_from_db()
    assert t.ask_is_open and t.status == AgentTask._meta.get_field("status").default
    # Something new on it brings it back.
    AgentTask.objects.filter(pk=t.pk).update(updated_at=timezone.now() + dt.timedelta(seconds=5))
    assert _keys(c.get("/api/me/attention/")) == ["task:eva:T1"]


def test_hide_for_days_and_unhide(jj):
    AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="q", ask_kind="question")
    c = jj["client"]
    c.post("/api/me/attention/hide", {"keys": ["task:eva:T1"], "days": 3}, content_type="application/json")
    assert _keys(c.get("/api/me/attention/")) == []
    assert c.delete("/api/me/attention/hide/task:eva:T1").json() == {"count": 1}
    assert _keys(c.get("/api/me/attention/")) == ["task:eva:T1"]


def test_hiding_is_only_ever_your_own_list(jj):
    AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="q", ask_kind="question")
    services.hide(jj["user"], ["task:eva:T1"])
    peer = User.objects.create_user("pe", "pe@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=peer, workspace=jj["ws"], role=WorkspaceMembership.EDITOR)
    pc = Client()
    pc.force_login(peer)
    # An unrouted open ask is in every member's list; jj hiding it leaves peer's alone.
    assert _keys(pc.get("/api/me/attention/")) == ["task:eva:T1"]


def test_one_read_spans_every_organisation_labelled_by_each(jj):
    """A user-level view: one call covers both trees, each read on its own and
    each row labelled with its organisation — no client fans out or merges."""
    fam = Workspace.objects.create(slug="family", display_name="Family", created_by=jj["user"])
    WorkspaceMembership.objects.create(user=jj["user"], workspace=fam, role=WorkspaceMembership.OWNER)
    kid = Workspace.objects.create(slug="kids", display_name="Kids", created_by=jj["user"], parent=fam)
    AgentTask.objects.create(agent=jj["agent"], ext_id="T1", title="work", ask_kind="review")
    home = Agent.objects.create(slug="nan", name="Nan", workspace=kid, owner=jj["user"])
    AgentTask.objects.create(agent=home, ext_id="T1", title="home", ask_kind="review")
    rows = {i["key"]: i for i in jj["client"].get("/api/me/attention/").json()["now"]}
    assert rows["task:eva:T1"]["organisation"] == "connect"
    assert rows["task:nan:T1"]["organisation"] == "family"
    assert rows["task:nan:T1"]["workspace"] == "kids"
    # Pinned, it is exactly that organisation.
    assert _keys(jj["client"].get("/api/w/connect/me/attention/")) == ["task:eva:T1"]


def test_states_match_the_wire_enum():
    from typing import get_args

    from apps.attention import schemas, types

    assert set(get_args(schemas.State)) == set(types.STATES)


def test_my_projects_span_every_organisation(jj):
    from apps.agents.models import AgentProject

    fam = Workspace.objects.create(slug="family", display_name="Family", created_by=jj["user"])
    WorkspaceMembership.objects.create(user=jj["user"], workspace=fam, role=WorkspaceMembership.OWNER)
    nan = Agent.objects.create(slug="nan", name="Nan", workspace=fam, owner=jj["user"])
    AgentProject.objects.create(agent=jj["agent"], ext_id="P1", name="Work", owner_user=jj["user"])
    AgentProject.objects.create(agent=nan, ext_id="P1", name="Home", owner_user=jj["user"])
    AgentProject.objects.create(agent=nan, ext_id="P2", name="Not mine")
    # The flat fleet list refuses to join the two organisations; the user-level view reads each.
    assert jj["client"].get("/api/projects/").status_code == 422
    r = jj["client"].get("/api/me/projects/")
    assert r.status_code == 200, r.content
    assert sorted(p["name"] for p in r.json()) == ["Home", "Work"]


@pytest.fixture()
def two_orgs(jj):
    """jj in two organisations: the flat lists refuse (422), the user-level views read each."""
    fam = Workspace.objects.create(slug="family", display_name="Family", created_by=jj["user"])
    WorkspaceMembership.objects.create(user=jj["user"], workspace=fam, role=WorkspaceMembership.OWNER)
    nan = Agent.objects.create(slug="nan", name="Nan", workspace=fam, owner=jj["user"])
    return {**jj, "fam": fam, "nan": nan}


def test_user_level_views_read_every_organisation_where_flat_lists_refuse(two_orgs):
    c = two_orgs["client"]
    AgentTask.objects.create(agent=two_orgs["agent"], ext_id="T1", title="work", ask_kind="review")
    AgentTask.objects.create(agent=two_orgs["nan"], ext_id="T1", title="home", ask_kind="question")
    Session.objects.create(workspace=two_orgs["ws"], agent=two_orgs["agent"], title="w", created_by=two_orgs["user"])
    Session.objects.create(workspace=two_orgs["fam"], agent=two_orgs["nan"], title="h", created_by=two_orgs["user"])
    for flat in ("/api/canopy-sessions/", "/api/tasks/?waiting=me", "/api/agents/"):
        assert c.get(flat).status_code == 422, flat
    sessions = c.get("/api/me/sessions/").json()
    assert sorted(s["workspace"] for s in sessions) == ["connect", "family"]
    tasks = c.get("/api/me/tasks/?waiting=me").json()
    assert [t["title"] for t in tasks] == ["work", "home"]  # review before question
    agents = c.get("/api/me/agents/").json()
    assert [a["slug"] for a in agents["items"]] == ["eva", "nan"]
    # Pinned, a user-level view is that one organisation.
    assert [s["title"] for s in c.get("/api/w/family/me/sessions/").json()] == ["h"]

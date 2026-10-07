"""The task and project routes — which are also the MCP tools."""
import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, AgentTask
from apps.harness.models import Turn
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db


@pytest.fixture()
def c():
    u = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u)
    WorkspaceMembership.objects.create(user=u, workspace=ws, role=WorkspaceMembership.OWNER)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=u)
    client = Client()
    client.force_login(u)
    return client, agent, u


def _viewer(agent, username="vi"):
    v = User.objects.create_user(username, f"{username}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=v, workspace=agent.workspace,
                                       role=WorkspaceMembership.VIEWER)
    client = Client()
    client.force_login(v)
    return client, v


def test_create_list_filter_act(c):
    client, agent, _u = c
    AgentProject.objects.create(agent=agent, ext_id="P1", name="IDM talk")
    r = client.post("/api/agents/eva/tasks/", [
        {"title": "Draft deck", "project": "P1"},
        {"title": "Send EOI?", "ask_kind": "review", "idempotency_key": "k"},
    ], content_type="application/json")
    assert r.status_code == 201, r.content
    assert [t["ext_id"] for t in r.json()] == ["T1", "T2"]
    assert len(client.get("/api/agents/eva/tasks/?project=P1").json()) == 1
    assert len(client.get("/api/agents/eva/tasks/?project=none").json()) == 1
    assert [t["ext_id"] for t in client.get("/api/agents/eva/tasks/?ask=open").json()] == ["T2"]
    r = client.post("/api/agents/eva/tasks/T2/actions", {"action": "approve"},
                    content_type="application/json")
    assert r.status_code == 200 and r.json()["task"]["status"] == "in_progress"
    assert client.post("/api/agents/eva/tasks/T2/actions", {"action": "decline"},
                       content_type="application/json").status_code == 409
    queue = client.get("/api/agents/eva/actions/?status=pending").json()
    assert [a["task_ext_id"] for a in queue] == ["T2"]
    r = client.post(f"/api/agents/eva/actions/{queue[0]['id']}/applied", {},
                    content_type="application/json")
    assert r.json()["status"] == "applied"


def test_empty_reply_is_422(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": ""},
                    content_type="application/json")
    assert r.status_code == 422


def test_unknown_action_is_422(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="review")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "defer"},
                    content_type="application/json")
    assert r.status_code == 422


def test_get_project_has_four_sections(c):
    client, agent, _u = c
    AgentProject.objects.create(agent=agent, ext_id="P1", name="IDM talk",
                                links=[{"label": "deck", "url": "https://x"}])
    body = client.get("/api/agents/eva/projects/P1/").json()
    assert {"name", "outcome", "status", "tasks", "recent_turns", "links"} <= body.keys()


def test_get_project_tasks_live_first_and_turns_touching_it(c):
    client, agent, _u = c
    p = AgentProject.objects.create(agent=agent, ext_id="P1", name="IDM talk")
    AgentTask.objects.create(agent=agent, ext_id="T1", title="old", project=p, status="done")
    AgentTask.objects.create(agent=agent, ext_id="T2", title="live", project=p)
    AgentTask.objects.create(agent=agent, ext_id="T3", title="elsewhere")
    Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, prompt="work T2",
                        task_ext_ids=["T2"], idempotency_key="t-a")
    Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, prompt="other",
                        task_ext_ids=["T3", "T22"], idempotency_key="t-b")
    body = client.get("/api/agents/eva/projects/P1/").json()
    assert [t["ext_id"] for t in body["tasks"]] == ["T2", "T1"]
    assert [t["task_ext_ids"] for t in body["recent_turns"]] == [["T2"]]
    assert body["recent_turns"][0]["prompt_preview"] == "work T2"


def test_get_task_carries_its_actions_and_patch_by_ref(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    client.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": "yes"},
                content_type="application/json")
    body = client.get("/api/agents/eva/tasks/t1/").json()
    assert body["ext_id"] == "T1" and body["ask_open"] is False and body["ask_closed_at"]
    assert [(a["action"], a["comment"]) for a in body["actions"]] == [("reply", "yes")]
    assert "id" not in body and "uuid" not in body
    r = client.patch("/api/agents/eva/tasks/T1/", {"title": "renamed"},
                     content_type="application/json")
    assert r.status_code == 200 and r.json()["title"] == "renamed"
    assert client.get("/api/agents/eva/tasks/T9/").status_code == 404


def test_create_explicit_ext_id_collision_is_409(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="a")
    r = client.post("/api/agents/eva/tasks/", [{"ext_id": "T1", "title": "b"}],
                    content_type="application/json")
    assert r.status_code == 409


def test_create_unknown_waiting_on_is_422(c):
    client, _agent, _u = c
    r = client.post("/api/agents/eva/tasks/",
                    [{"title": "b", "waiting_on_email": "nobody@nowhere.org"}],
                    content_type="application/json")
    assert r.status_code == 422
    assert AgentTask.objects.count() == 0


def test_viewer_may_approve_but_not_dispatch_done_create_or_patch(c):
    _client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="review")
    AgentTask.objects.create(agent=agent, ext_id="T2", title="w")
    viewer, _v = _viewer(agent)
    for action in ("dispatch", "done"):
        r = viewer.post("/api/agents/eva/tasks/T2/actions", {"action": action},
                        content_type="application/json")
        assert r.status_code == 403, action
    assert viewer.post("/api/agents/eva/tasks/", [{"title": "x"}],
                       content_type="application/json").status_code == 403
    assert viewer.patch("/api/agents/eva/tasks/T2/", {"title": "x"},
                        content_type="application/json").status_code == 403
    r = viewer.post("/api/agents/eva/tasks/T1/actions", {"action": "approve"},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    # Marking the agent's queue applied is the agent's (or an editor's) job.
    row_id = r.json()["action"]["id"]
    assert viewer.post(f"/api/agents/eva/actions/{row_id}/applied", {},
                       content_type="application/json").status_code == 403


def test_non_member_gets_404(c):
    _client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="review")
    stranger = User.objects.create_user("st", "st@dimagi.com", "pw")
    sc = Client()
    sc.force_login(stranger)
    assert sc.get("/api/agents/eva/tasks/").status_code == 404
    assert sc.post("/api/agents/eva/tasks/T1/actions", {"action": "approve"},
                   content_type="application/json").status_code == 404


def test_fleet_routes(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    assert [t["agent_slug"] for t in client.get("/api/tasks/?waiting=me").json()] == ["eva"]
    assert client.get("/api/projects/").status_code == 200


def test_fleet_waiting_me_includes_parked_task_with_no_ask(c):
    client, agent, u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="numbers", waiting_on_user=u)
    other = User.objects.create_user("o", "o@dimagi.com", "pw")
    AgentTask.objects.create(agent=agent, ext_id="T2", title="theirs", waiting_on_user=other)
    got = client.get("/api/tasks/?waiting=me").json()
    assert [t["ext_id"] for t in got] == ["T1"]
    assert [t["ext_id"] for t in client.get("/api/agents/eva/tasks/?waiting=me").json()] == ["T1"]


def test_fleet_never_shows_another_workspaces_unrouted_ask(c):
    client, _agent, _u = c
    owner = User.objects.create_user("x", "x@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    a2 = Agent.objects.create(slug="zed", name="Zed", workspace=ws2, owner=owner)
    AgentTask.objects.create(agent=a2, ext_id="T1", title="secret?", ask_kind="review")
    assert client.get("/api/tasks/?waiting=me").json() == []
    assert client.get("/api/tasks/").json() == []
    AgentProject.objects.create(agent=a2, ext_id="P1", name="hidden")
    assert client.get("/api/projects/").json() == []


def test_fleet_tasks_review_before_question_and_agent_filter(c):
    client, agent, u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    AgentTask.objects.create(agent=agent, ext_id="T2", title="r", ask_kind="review")
    AgentTask.objects.create(agent=agent, ext_id="T3", title="w")
    a2 = Agent.objects.create(slug="ace", name="Ace", workspace=agent.workspace, owner=u)
    AgentTask.objects.create(agent=a2, ext_id="T1", title="r2", ask_kind="review")
    got = client.get("/api/tasks/?agent=eva").json()
    assert [t["ext_id"] for t in got] == ["T2", "T1", "T3"]
    assert {t["agent_slug"] for t in client.get("/api/tasks/").json()} == {"eva", "ace"}


def test_fleet_projects_filters(c):
    client, agent, _u = c
    AgentProject.objects.create(agent=agent, ext_id="P1", name="a", repo_slug="canopy")
    AgentProject.objects.create(agent=agent, ext_id="P2", name="b", status="done")
    assert [p["ext_id"] for p in client.get("/api/projects/").json()] == ["P1"]
    assert [p["ext_id"] for p in client.get("/api/projects/?status=done").json()] == ["P2"]
    assert client.get("/api/projects/?repo_slug=nope").json() == []


@pytest.mark.parametrize("path", [
    "/api/items/", "/api/agents/eva/items/", "/api/agents/eva/work-products/",
    "/api/agents/eva/tasks/waiting/", "/api/agents/eva/commands", "/api/agent-runs/projects/",
])
def test_removed_routes_are_gone(c, path):
    client, _agent, _u = c
    assert client.get(path).status_code in (404, 405)


def test_operation_ids_are_the_mcp_tool_names():
    from apps.api.api import api

    schema = api.get_openapi_schema()
    ops = {op["operationId"] for p in schema["paths"].values() for op in p.values()
           if isinstance(op, dict) and "operationId" in op}
    assert {"list_tasks", "create_tasks", "get_task", "patch_task", "act_on_task",
            "list_task_actions", "mark_task_action_applied", "list_projects",
            "create_project", "get_project", "patch_project", "list_fleet_tasks",
            "list_fleet_projects"} <= ops

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
    # An agent matches its own published tasks on the key it sent.
    assert [t["idempotency_key"] for t in r.json()] == ["", "k"]
    assert len(client.get("/api/agents/eva/tasks/?project=P1").json()) == 1
    assert len(client.get("/api/agents/eva/tasks/?project=none").json()) == 1
    assert [t["ext_id"] for t in client.get("/api/agents/eva/tasks/?ask=open").json()] == ["T2"]
    r = client.post("/api/agents/eva/tasks/T2/actions", {"action": "approve"},
                    content_type="application/json")
    assert r.status_code == 200 and r.json()["task"]["status"] == "in_progress"
    # Approve started the work: one turn, and nothing left in the agent's queue.
    assert len(r.json()["turn_ids"]) == 1 and r.json()["action"]["status"] == "applied"
    assert client.post("/api/agents/eva/tasks/T2/actions", {"action": "decline"},
                       content_type="application/json").status_code == 409
    assert client.get("/api/agents/eva/actions/?status=pending").json() == []
    # A viewer's note is what still waits in the queue for the agent to drain.
    viewer, _v = _viewer(agent)
    r = viewer.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": "fyi"},
                    content_type="application/json")
    assert r.status_code == 200 and r.json()["turn_ids"] == []
    queue = client.get("/api/agents/eva/actions/?status=pending").json()
    assert [a["task_ext_id"] for a in queue] == ["T1"]
    r = client.post(f"/api/agents/eva/actions/{queue[0]['id']}/applied", {},
                    content_type="application/json")
    assert r.json()["status"] == "applied"


def test_editor_reply_and_nudge_start_turns(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="w", status="in_progress")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": "also X"},
                    content_type="application/json")
    assert r.status_code == 200 and len(r.json()["turn_ids"]) == 1
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "nudge"},
                    content_type="application/json")
    assert r.status_code == 200 and len(r.json()["turn_ids"]) == 1
    assert r.json()["task"]["status"] == "in_progress"


def test_nudge_on_a_suggested_task_is_409(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="w")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "nudge"},
                    content_type="application/json")
    assert r.status_code == 409, r.content


def test_dispatch_action_is_gone(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="w", status="in_progress")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "dispatch"},
                    content_type="application/json")
    assert r.status_code == 422


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


def test_viewer_may_approve_but_not_nudge_done_create_or_patch(c):
    _client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="review")
    AgentTask.objects.create(agent=agent, ext_id="T2", title="w", status="in_progress")
    viewer, _v = _viewer(agent)
    for action in ("nudge", "done"):
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


def test_fleet_routes_answer_under_a_tenant_prefix(c):
    """A tenant page's client rewrites /api/tasks/ and /api/projects/ to
    /api/w/<ws>/… (WS_SCOPED_API_PREFIXES); the pinned path must serve that
    workspace, and a workspace the caller is not in must 404."""
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    got = client.get("/api/w/connect/tasks/?waiting=me")
    assert got.status_code == 200, got.content
    assert [t["agent_slug"] for t in got.json()] == ["eva"]
    assert client.get("/api/w/connect/projects/").status_code == 200
    assert client.get("/api/w/elsewhere/tasks/").status_code == 404


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


# --- ext_id allocation ----------------------------------------------------------


def test_an_explicit_ext_id_does_not_break_auto_numbering(c):
    client, agent, _u = c
    post = lambda body: client.post("/api/agents/eva/tasks/", body,  # noqa: E731
                                    content_type="application/json")
    assert post([{"ext_id": "T1", "title": "a"}]).status_code == 201
    r = post([{"title": "b"}])
    assert r.status_code == 201, r.content
    assert r.json()[0]["ext_id"] == "T2"
    # An explicit id ahead of the counter moves it, so the next auto id is fresh.
    assert post([{"ext_id": "T7", "title": "c"}]).status_code == 201
    assert post([{"title": "d"}]).json()[0]["ext_id"] == "T8"


def test_a_case_variant_ext_id_is_a_duplicate(c):
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="a")
    r = client.post("/api/agents/eva/tasks/", [{"ext_id": "t1", "title": "b"}],
                    content_type="application/json")
    assert r.status_code == 409
    assert agent.tasks.count() == 1


def test_raised_by_must_be_a_turn_of_this_agent(c):
    client, agent, u = c
    other = Agent.objects.create(slug="ace", name="Ace", workspace=agent.workspace, owner=u)
    foreign = Turn.objects.create(agent=other, origin=Turn.ORIGIN_API, idempotency_key="f")
    mine = Turn.objects.create(agent=agent, origin=Turn.ORIGIN_API, idempotency_key="m")
    post = lambda raised_by: client.post(  # noqa: E731
        "/api/agents/eva/tasks/", [{"title": "x", "raised_by": str(raised_by)}],
        content_type="application/json")
    assert post(foreign.id).status_code == 422
    assert post(mine.id).status_code == 201


# --- approve runs on_approve -----------------------------------------------------


def test_approve_dispatches_on_approve(c):
    client, agent, _u = c
    client.post("/api/agents/eva/tasks/", [{
        "title": "Send it?", "ask_kind": "review", "idempotency_key": "d1",
        "on_approve": [{"prompt": "/eva:turn send it"}]}], content_type="application/json")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "approve"},
                    content_type="application/json")
    assert r.status_code == 200, r.content
    body = r.json()
    assert len(body["turn_ids"]) == 1
    assert body["action"]["status"] == "applied", "the dispatched turn IS the follow-up"
    assert body["task"]["status"] == "in_progress" and body["task"]["dispatched_at"]
    turn = Turn.objects.get(pk=body["turn_ids"][0])
    assert turn.agent_id == agent.pk and "send it" in turn.prompt


def test_cross_workspace_target_is_422_and_the_ask_stays_open(c):
    client, _agent, _u = c
    owner = User.objects.create_user("x", "x@dimagi.com", "pw")
    ws2 = Workspace.objects.create(slug="other", display_name="Other", created_by=owner)
    Agent.objects.create(slug="zed", name="Zed", workspace=ws2, owner=owner)
    client.post("/api/agents/eva/tasks/", [{
        "title": "Fan out?", "ask_kind": "review", "idempotency_key": "d2",
        "on_approve": [{"prompt": "go", "target_agent": "zed"}]}], content_type="application/json")
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "approve"},
                    content_type="application/json")
    assert r.status_code == 422
    task = AgentTask.objects.get(ext_id="T1")
    assert task.ask_is_open and task.status == "suggested" and not task.actions.exists()
    assert not Turn.objects.filter(agent__slug="zed").exists()


def test_a_malformed_on_approve_is_422(c):
    client, agent, _u = c
    # Refused at the door...
    r = client.post("/api/agents/eva/tasks/", [{
        "title": "x", "on_approve": [{"prompt": "go", "routing": "anywhere"}]}],
        content_type="application/json")
    assert r.status_code == 422
    # ...and a stored spec that cannot run rolls the approve back.
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="review",
                             on_approve=[{"prompt": "go", "target_agent": "nobody"}])
    r = client.post("/api/agents/eva/tasks/T1/actions", {"action": "approve"},
                    content_type="application/json")
    assert r.status_code == 422
    assert AgentTask.objects.get(ext_id="T1").ask_is_open


def test_fleet_tasks_limit_caps_rows(c):
    client, agent, _u = c
    for i in range(3):
        AgentTask.objects.create(agent=agent, ext_id=f"T{i + 1}", title="t")
    assert len(client.get("/api/tasks/?limit=2").json()) == 2
    assert len(client.get("/api/tasks/?limit=0").json()) == 1  # clamped, not a 500
    assert len(client.get("/api/tasks/").json()) == 3


# --- ext_id safety, origin, source -------------------------------------------------


def test_case_variant_ext_id_is_refused_by_the_database_too():
    # The service checks first (iexact); this is the race that slips past it — two
    # creates both seeing the id free. The Lower(ext_id) constraint stops it.
    from django.db import IntegrityError, transaction

    u = User.objects.create_user("db", "db@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="w2", display_name="W2", created_by=u)
    agent = Agent.objects.create(slug="eve", name="Eve", workspace=ws, owner=u)
    AgentTask.objects.create(agent=agent, ext_id="T1", title="a")
    with pytest.raises(IntegrityError), transaction.atomic():
        AgentTask.objects.create(agent=agent, ext_id="t1", title="b")


def test_a_raced_case_variant_maps_to_409(c, monkeypatch):
    from apps.agents import services

    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="a")
    # Simulate the race: the pre-check sees "t1" as free, the insert collides.
    monkeypatch.setattr(services, "_claim_ext_id", lambda _agent, explicit: explicit)
    r = client.post("/api/agents/eva/tasks/", [{"ext_id": "t1", "title": "b"}],
                    content_type="application/json")
    assert r.status_code == 409, r.content
    assert agent.tasks.count() == 1


def test_an_ext_id_with_a_slash_is_422(c):
    client, agent, _u = c
    r = client.post("/api/agents/eva/tasks/", [{"ext_id": "a/b", "title": "x"}],
                    content_type="application/json")
    assert r.status_code == 422, r.content
    assert "/" in r.json()["detail"]
    assert agent.tasks.count() == 0


def test_patch_cannot_set_an_ext_id(c):
    # ext_id is the address; a PATCH has no such field (unknown fields are 422).
    client, agent, _u = c
    AgentTask.objects.create(agent=agent, ext_id="T1", title="a")
    r = client.patch("/api/agents/eva/tasks/T1/", {"ext_id": "a/b"},
                     content_type="application/json")
    assert r.status_code == 422
    assert agent.tasks.get().ext_id == "T1"


@pytest.mark.parametrize("origin,stored", [
    ("", ""), ("api", "api"), ("email", "email"), ("huddle", "huddle"),
    ("task-tracker", "task-tracker"), ("dispatch", "dispatch"),
    ("manual", "api"), ("cron", "canopy_scheduler"),  # retired spellings normalize
])
def test_known_origins_are_accepted(c, origin, stored):
    client, agent, _u = c
    r = client.post("/api/agents/eva/tasks/", [{"title": "x", "origin": origin}],
                    content_type="application/json")
    assert r.status_code == 201, r.content
    assert agent.tasks.get().origin == stored


@pytest.mark.parametrize("origin", ["nonsense", "canopy_scheduler", "canopy_web_chat"])
def test_an_unknown_or_server_only_origin_is_422(c, origin):
    client, agent, _u = c
    r = client.post("/api/agents/eva/tasks/", [{"title": "x", "origin": origin}],
                    content_type="application/json")
    assert r.status_code == 422
    assert agent.tasks.count() == 0


def test_create_keeps_source(c):
    client, agent, _u = c
    r = client.post("/api/agents/eva/tasks/", [{"title": "x", "source": "sheet:board"}],
                    content_type="application/json")
    assert r.status_code == 201, r.content
    assert agent.tasks.get().source == "sheet:board"


def test_actions_list_is_capped_and_puts_pending_first(c):
    from apps.agents.models import AgentTaskAction

    client, agent, _u = c
    task = AgentTask.objects.create(agent=agent, ext_id="T1", title="t")
    old_pending = AgentTaskAction.objects.create(agent=agent, task=task, action="reply",
                                                 status=AgentTaskAction.PENDING)
    applied = [AgentTaskAction.objects.create(agent=agent, task=task, action="reply")
               for _ in range(3)]
    ids = lambda q: [a["id"] for a in client.get(f"/api/agents/eva/actions/{q}").json()]  # noqa: E731
    # The pending row is the OLDEST, yet a short page still carries it.
    assert ids("?limit=2") == [old_pending.id, applied[-1].id]
    assert ids("") == [old_pending.id] + [a.id for a in reversed(applied)]
    assert ids("?status=applied&limit=1") == [applied[-1].id]
    assert len(ids("?limit=0")) == 1  # clamped, not a 500


def test_the_agent_replying_on_its_own_card_does_not_wake_itself(c):
    """An agent's own login is an editor of its workspace, but its note on its own
    task must not enqueue a turn of itself — a turn that replies again would loop."""
    _client, agent, _u = c
    bot = User.objects.create_user("eva-bot", "eva@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=bot, workspace=agent.workspace,
                                       role=WorkspaceMembership.EDITOR)
    agent.user = bot
    agent.save(update_fields=["user"])
    AgentTask.objects.create(agent=agent, ext_id="T1", title="w", status="in_progress")
    as_agent = Client()
    as_agent.force_login(bot)
    r = as_agent.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": "noted"},
                      content_type="application/json")
    assert r.status_code == 200 and r.json()["turn_ids"] == []
    assert r.json()["action"]["status"] == "pending"


def test_the_agent_answering_its_own_question_does_not_wake_itself(c):
    """Same rule for an answer: anyone allowed to answer wakes the agent, except
    the agent itself — its answer to its own question would only wake itself."""
    _client, agent, _u = c
    bot = User.objects.create_user("eva-bot", "eva@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=bot, workspace=agent.workspace,
                                       role=WorkspaceMembership.EDITOR)
    agent.user = bot
    agent.save(update_fields=["user"])
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    as_agent = Client()
    as_agent.force_login(bot)
    r = as_agent.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": "found it"},
                      content_type="application/json")
    assert r.status_code == 200 and r.json()["turn_ids"] == []
    assert r.json()["action"]["status"] == "pending"


def test_a_viewer_answering_a_question_wakes_the_agent(c):
    """An answer is the response the agent explicitly asked for, so the viewer
    tier that may answer also starts the turn carrying it."""
    _client, agent, _u = c
    viewer = User.objects.create_user("vi", "vi@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=agent.workspace,
                                       role=WorkspaceMembership.VIEWER)
    AgentTask.objects.create(agent=agent, ext_id="T1", title="q", ask_kind="question")
    as_viewer = Client()
    as_viewer.force_login(viewer)
    r = as_viewer.post("/api/agents/eva/tasks/T1/actions", {"action": "reply", "comment": "Tuesday"},
                       content_type="application/json")
    assert r.status_code == 200, r.content
    assert len(r.json()["turn_ids"]) == 1 and r.json()["action"]["status"] == "applied"

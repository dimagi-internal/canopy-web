"""A personal project reaches nobody but its lead and its agents: every GET, walked.

There is no single read helper a route can be made to call, and a route written next
year will not know to ask, so this walks the real OpenAPI schema. It seeds a personal
project and everything that hangs off it, with a sentinel in every text field — written
straight to the database, including rows the services refuse (a share, a check-in, a
vault, an artifact), so a read that trusts the write refusal is caught too — then calls
EVERY GET as a workspace editor and a workspace admin who are not on the project, and
asserts no sentinel and no task key comes back.

A path parameter with no entry in `PATH_VALUES` fails the test: a new route opts in by
saying what to call it with. Add the value that reaches project data if there is one; a
parameter that names nothing about a project gets a value that finds nothing.

The two tests beside the walk keep it honest: the same routes show a twin WORKSPACE
project to the same editor, and the personal project's own lead reads every family of
sentinel. A walk that 404s everywhere would otherwise pass.
"""
from __future__ import annotations

import ast
import itertools
import re

import pytest
from django.contrib import admin
from fastmcp.exceptions import ToolError

from apps.agents.models import (
    AgentProject,
    AgentTask,
    AgentTaskAction,
)
from apps.harness.models import Turn
from apps.mcp import project_gateway
from apps.mcp import projects as mcp_projects
from tests._source import iter_app_sources

pytestmark = [pytest.mark.guard, pytest.mark.django_db]

from tests.fixtures.personal_project_world import (
    _REQUIRED_QUERY,
    MARK,
    PUBLIC_MARK,
    S,
    _body,
    _client,
    _get_routes,
    _make_world,
    _query_variants,
    path_values,
)


@pytest.fixture()
def world() -> dict:
    return _make_world()


def _urls(path: str, op: dict, values: dict, queries: dict):
    """Every (url, the values that went into it) to try for one route."""
    names = re.findall(r"\{([^}]+)\}", path)
    missing = [n for n in names if n not in values]
    assert not missing, (
        f"{path}: no fixture mapping for path parameter(s) {missing}. Add each to `_project_values` if it can reach a "
        f"project's data, else to NOT_ABOUT_A_PROJECT, in tests/test_personal_project_never_leaks.py.")
    params = op.get("parameters", [])
    required = {q["name"]: _REQUIRED_QUERY.get(q["name"], "guard") for q in params if q["in"] == "query" and q.get("required")}
    for combo in itertools.product(*(values[n] for n in names)):
        url = path
        for name, value in zip(names, combo):
            url = url.replace("{%s}" % name, value)
        yield url, {}, list(combo)
        for q in params:
            if q["in"] == "query" and q["name"] in queries and q["name"] not in required:
                for value in queries[q["name"]]:
                    yield url, {**required, q["name"]: value}, [*combo, value]
        if required:
            yield url, required, list(combo)


def _walk(client, w: dict, visit) -> int:
    values, queries, count = path_values(w), _query_variants(w), 0
    for path, op in _get_routes():
        for url, query, echoed in _urls(path, op, values, queries):
            visit(url, query, echoed, client.get(url, query))
            count += 1
    return count


# ---- the walk ----------------------------------------------------------------------


@pytest.mark.parametrize("who", ["editor", "wsadmin"])
def test_every_get_keeps_a_personal_project_from_a_non_reader(world, who):
    keys = [re.escape(t.key) for t in world["tasks"]]
    leaked_key = re.compile(r"(?<![\w-])(?:" + "|".join(keys) + r")(?![\w-])", re.I)
    leaks, statuses, broken = [], {}, []

    def visit(url, query, echoed, resp):
        statuses[resp.status_code] = statuses.get(resp.status_code, 0) + 1
        if resp.status_code >= 500 and resp.status_code != 503:  # 503: a service this box has no config for
            broken.append(url)
        text = _body(resp)
        # A 404 repeats what was asked for; what was asked for is not a leak.
        for value in sorted(echoed + list(query.values()), key=len, reverse=True):
            if value:
                text = text.replace(value, "")
        found = re.findall(MARK + r"[\w.-]*", text, re.I) + leaked_key.findall(text)
        if found:
            leaks.append(f"{url} {query} [{resp.status_code}]: {sorted(set(found))[:4]}")

    total = _walk(_client(world[who]), world, visit)
    assert not leaks, f"{who} read a personal project through:\n" + "\n".join(leaks)
    assert not broken, f"a route that errors cannot be shown not to leak: {sorted(set(broken))}"
    assert total > 500, f"only {total} requests: the walk is not walking"
    assert statuses.get(200, 0) > 100, f"too few answers to mean anything: {statuses}"


def test_the_walk_reaches_a_workspace_project_for_the_same_editor(world):
    """The control: the same person, the same routes, a twin anyone in the workspace reads."""
    client = _client(world["editor"])
    twin, public = world["twin"], world["public_task"]
    for url in ("/api/projects/", "/api/workspace-projects/", f"/api/workspace-projects/guardws/{twin.handle}/",
                "/api/tasks/", f"/api/tasks/{public.key}/", f"/api/agents/{world['lead_agent'].slug}/projects/"):
        resp = client.get(url, {"status": ""} if url == "/api/projects/" else {})
        assert resp.status_code == 200, url
        assert PUBLIC_MARK in _body(resp), url


def test_its_lead_and_its_agents_read_what_the_walk_kept_out(world):
    """Positive control over the routes that serve each family of sentinel."""
    project, tasks = world["project"], world["tasks"]
    addr = f"/api/workspace-projects/guardws/{project.handle}/"
    expected = {
        addr: ["project.name", "project.outcome", "project.notes", "task.lead", "update.body", "draft.body",
               "milestone.name", "skill.description", "project.owner_note"],
        f"{addr}milestones/": ["milestone.description"],
        f"{addr}updates/": ["update.body", "draft.body"],
        f"{addr}services/": ["skill.instructions", "mcp.description"],
        f"{addr}kb/sources": ["kbsource.name"],
        f"{addr}kb/versions": ["kbversion.summary"],
        f"/api/tasks/{tasks[2].key}/": ["task.lead.notes", "action.comment", "task.sub"],
        f"/api/tasks/{tasks[0].key}/": ["why.quote"],
        "/api/tasks/": ["task.lead.next"],
    }
    for who in ("lead", "lead_login"):
        client = _client(world[who])
        for url, fields in expected.items():
            resp = client.get(url)
            assert resp.status_code == 200, (who, url, resp.status_code)
            text = _body(resp)
            for field in fields:
                assert S(field) in text, (who, url, field)
    # The turn's log: only the project's readers, not its agent's admin, who started it.
    hidden = _client(world["wsadmin"]).get(f"/api/harness/turns/{world['turn'].pk}").json()
    assert hidden["prompt"] == ""
    assert _client(world["lead"]).get(f"/api/harness/turns/{world['turn'].pk}").json()["prompt"] == S("turn.prompt")


# ---- the MCP tools, which are not REST routes ---------------------------------------


@pytest.mark.parametrize("who", ["editor", "wsadmin"])
def test_gateway_and_kb_tools_refuse_a_non_reader(world, who, monkeypatch):
    user, project = world[who], world["project"]
    monkeypatch.setattr(project_gateway, "_current_user", lambda: user)
    monkeypatch.setattr(mcp_projects, "_current_user", lambda: user)
    assert mcp_projects.readable_project(user, "guardws", project.handle) is None
    assert project not in project_gateway._openable(user)
    assert MARK not in project_gateway._list_projects("").lower()
    assert project.address not in project_gateway._list_projects(MARK)
    for call in (lambda: project_gateway._open(project.address), lambda: project_gateway._open(project.handle),
                 lambda: project_gateway._skill(project.address, f"{MARK}-skill"),
                 lambda: project_gateway._kb_call(project.address, "kb_info", {}),
                 lambda: mcp_projects._call("guardws", project.handle, "info", {})):
        with pytest.raises(ToolError) as exc:
            call()
        # The refusal says what was asked for, exactly as for a project that is not there.
        assert S("project.name") not in str(exc.value)
    assert mcp_projects._specs("guardws", project.handle) == []


@pytest.mark.parametrize("who", ["lead", "lead_login"])
def test_gateway_opens_it_for_a_reader(world, who, monkeypatch):
    user, project = world[who], world["project"]
    monkeypatch.setattr(project_gateway, "_current_user", lambda: user)
    monkeypatch.setattr(project_gateway, "_call", lambda *a: "a stored knowledge base's info")
    assert mcp_projects.readable_project(user, "guardws", project.handle) == project
    assert S("project.name") in project_gateway._list_projects("")
    assert f"{MARK}-skill" in project_gateway._open(project.address)
    assert S("skill.instructions") in project_gateway._skill(project.address, f"{MARK}-skill")


def test_django_admin_shows_staff_no_more_than_the_api(world):
    """AgentProject has no admin; the board rows and turns that do leave out a personal
    project's, for staff who may not read it."""
    from django.test import RequestFactory

    assert AgentProject not in admin.site._registry
    request = RequestFactory().get("/admin/")
    request.user = world["wsadmin"]
    tasks = admin.site._registry[AgentTask].get_queryset(request)
    assert world["public_task"] in tasks and not (set(world["tasks"]) & set(tasks))
    assert world["action"] not in admin.site._registry[AgentTaskAction].get_queryset(request)
    assert world["turn"] not in admin.site._registry[Turn].get_queryset(request)


# ---- no new query slips past the rule ---------------------------------------------

#: Every `AgentProject.objects` / board `AgentTask.objects` outside a function that asks
#: `project_read_q`/`task_read_q` in the same call chain, and why it may. (file, function).
#: A new entry is a claim that what it reads goes no further than the people who may
#: read the row; if that is not so, filter it instead.
QUERY_ALLOWLIST = {
    # Gates and writers: the row found is checked by the caller, or the caller already holds it.
    ("apps/agents/services/projects.py", "get_project"): "resolves a name; project_or_404 and _project_ref gate the result",
    ("apps/agents/services/projects.py", "get_workspace_project"): "resolves an address; project_ws_or_404 and _project_ref gate the result",
    ("apps/agents/services/projects.py", "free_handle"): "handles only, to avoid a clash; the -2 suffix is the documented handle oracle",
    ("apps/agents/services/projects.py", "new_project"): "the one constructor of a project row",
    ("apps/agents/services/tasks.py", "_height_below"): "walks ids of a task already gated",
    ("apps/agents/services/tasks.py", "_descendants"): "the subtree of a task already gated, to move it",
    ("apps/agents/services/tasks.py", "create_tasks"): "the writer: an idempotency replay is checked with may_use_project before it is returned",
    ("apps/agents/services/tasks.py", "_ext_id_taken"): "an existence check on the agent's own alias namespace",
    ("apps/agents/services/tasks.py", "get_task"): "resolves a name; check_task_visible and resolve_parent gate the result",
    ("apps/agents/services/tasks.py", "list_tasks"): "machinery: an agent's own rows for its status report, no HTTP reader",
    ("apps/agents/services/task_actions.py", "act"): "re-reads the task the route already gated, for update",
    ("apps/agents/services/recurrence.py", "spawn"): "the writer: locks a template the route gated (or the tick holds); "
                                                     "its instance stays in the template's project",
    ("apps/agents/services/recurrence.py", "spawn_due"): "off-request: the recurrence tick, writes instances and returns a count",
    ("apps/agents/work.py", "task_by_key"): "resolves a key; task_or_404 gates the result",
    ("apps/agents/work.py", "set_settings"): "exists(): whether a prefix is frozen",
    ("apps/agents/work.py", "annotate_projects"): "figures for projects the caller's list already filtered",
    ("apps/agents/work.py", "top_rank"): "a rank, to place a new task",
    ("apps/agents/kb_versions.py", "publish_full"): "locks the project the route already gated",
    ("apps/agents/kb_versions.py", "rollback"): "locks the project the route already gated",
    ("apps/agents/kb_versions.py", "_replicate"): "off-request: the replica worker",
    ("apps/agents/kb_vectors.py", "_background_sync"): "off-request: vectors for one project",
    ("apps/agents/participants.py", "backfill"): "management command: writes participants, returns counts",
    ("apps/agents/proposals.py", "partner_tasks"): "a project's partner rows; callers hold a gated project",
    ("apps/agents/proposals.py", "propose"): "the writer: restricted to workspace projects",
    ("apps/agents/proposals.py", "_after_task_act"): "updates a task the route already gated",
    ("apps/agents/proposals.py", "expire_stale"): "off-request: the sweep, by status",
    ("apps/agents/proposals.py", "expire_stale_tasks"): "off-request: the sweep, by status",
    ("apps/agents/proposals.py", "task_partner_tasks"): "a task's partner rows; callers hold a gated task",
    ("apps/agents/proposals.py", "propose_task"): "the writer, inside a project_or_404'd project",
    ("apps/agents/move.py", "check"): "the move's own refusal; names only what the mover's agent leads",
    ("apps/agents/move.py", "_work"): "the move itself: a workspace-level act, nothing is shown but counts",
    ("apps/agents/move.py", "_project_memberships"): "the move itself, over the agent's own memberships",
    ("apps/agents/management/commands/route_waiting_tasks.py", "handle"): "a management command, run by an operator",
    ("apps/huddles/checkins.py", "proposed_tasks"): "the tasks of a run already gated",
    ("apps/harness/artifact_origin.py", "explicit_project"): "filtered to workspace projects: an artifact never links a personal one",
    ("apps/harness/turn_access.py", "redact"): "reads personal projects to hide them",
    ("apps/harness/schedule_turns.py", "_raise_job_nag"): "off-request: whether a schedule's ask is open",
    ("apps/harness/schedule_turns.py", "resolve_schedule_nags"): "off-request: closes a schedule's own asks",
    ("apps/agent_runs/documents.py", "create_run_doc"): "filtered to workspace projects: a run document never files under a personal one",
    ("apps/contacts/people_api.py", "add_person_fact"): "the writer: refuses a personal project",
    ("apps/runs/aggregate.py", "narrative_signals"): "project_out renders a personal project as None",
    ("apps/dynamic_views/api.py", "connection_share_target"): "filtered to workspace projects",
}

_READ_RULE = {"project_read_q", "task_read_q", "shared_with_q"}


def _board_model_names(tree: ast.Module, rel: str) -> set[str]:
    """Which of AgentProject/AgentTask this module means by the board's. `apps.agent_tasks`
    has an `AgentTask` of its own, and a module importing it means that one."""
    names = {"AgentProject", "AgentTask"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            is_other = (module.endswith("agent_tasks.models") or (rel.startswith("apps/agent_tasks/") and module == "models"))
            if is_other and any(a.name == "AgentTask" for a in node.names):
                names.discard("AgentTask")
    return names


def _query_sites() -> list[tuple[str, str, int]]:
    """(file, enclosing function, line) of every query that does not ask the read rule."""
    out = []
    for src in iter_app_sources(("apps",)):
        if src.rel.startswith("apps/agent_tasks/") or "/migrations/" in src.rel:
            continue
        names = _board_model_names(src.tree, src.rel)
        for func in ast.walk(src.tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for node in ast.walk(func):
                if not (isinstance(node, ast.Attribute) and node.attr == "objects"
                        and isinstance(node.value, ast.Name) and node.value.id in names):
                    continue
                asked = {n.attr if isinstance(n, ast.Attribute) else n.id for n in ast.walk(func)
                         if isinstance(n, ast.Attribute | ast.Name)}
                if not asked & _READ_RULE:
                    out.append((src.rel, func.name, node.lineno))
    return out


def test_every_board_query_asks_the_read_rule_or_says_why_not():
    sites = _query_sites()
    unexplained = sorted({(f, fn) for f, fn, _ in sites} - set(QUERY_ALLOWLIST))
    assert not unexplained, (
        "An AgentProject/AgentTask query that does not ask `project_read_q`/`task_read_q`:\n"
        + "\n".join(f"  {f}::{fn}" for f, fn in unexplained)
        + "\nFilter it, or (only if what it reads goes no further than the row's readers) add it to QUERY_ALLOWLIST "
          "with the reason.")


def test_the_query_allowlist_stays_honest():
    """A removed query takes its entry with it, and every entry says why."""
    stale = sorted(set(QUERY_ALLOWLIST) - {(f, fn) for f, fn, _ in _query_sites()})
    assert not stale, f"QUERY_ALLOWLIST entries with no such query any more: {stale}"
    assert all(len(reason) > 15 for reason in QUERY_ALLOWLIST.values())


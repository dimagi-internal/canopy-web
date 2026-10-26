"""The projects connection: one MCP server at `/api/mcp/projects/` that serves every
project's toolbox through the same small set of tools.

Driven the way Claude Code drives it — streamable HTTP with a real personal token,
through the dispatcher `config/asgi.py` mounts. The tool list is the same whatever
projects exist; a project's contents arrive only when a call asks for them, and
every knowledge-base call names its project, since MCP 2026-07-28 keeps no session.
"""
from __future__ import annotations

import json

import httpx2
import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from starlette.applications import Starlette
from starlette.routing import Mount

from apps.agents.models import AgentProject, AgentTask, ProjectService
from apps.harness.dispatch import task_turn_prompt
from apps.mcp.project_gateway import gateway_mcp, kb_tool_specs
from apps.mcp.projects import ProjectMcpDispatch, project_mcp
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures import kb as kb_fixtures

# Committed, not rolled back: a knowledge-base call runs on a worker thread with its own
# DB connection (projects.off_the_shared_thread), which must see the test's rows.
pytestmark = pytest.mark.django_db(transaction=True)

s3, world = kb_fixtures.s3, kb_fixtures.world

BASE = "https://canopy.test"
URL = "/api/mcp/projects/"
GATEWAY_TOOLS = {"projects", "open_project", "project_skill"}
KB_TOOLS = {s["name"] for s in kb_tool_specs()}
ALL_TOOLS = GATEWAY_TOOLS | KB_TOOLS


def _publish(world, s3, tmp_path):
    kb_fixtures.full(world, s3, tmp_path, kb_fixtures.bundle(tmp_path, "bundle", kb_fixtures.ITEMS))


def _token(user):
    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return raw


def _reader():
    reader = User.objects.create_user("ace-human", "c@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=reader, workspace=Workspace.objects.get(slug="connect"),
                                       role=WorkspaceMembership.VIEWER)
    return reader


def _messages(r) -> list[dict]:
    """The JSON-RPC messages of one response, an SSE stream or a JSON body."""
    if r.headers.get("content-type", "").startswith("application/json"):
        return [r.json()]
    return [json.loads(line[5:]) for line in r.text.splitlines() if line.startswith("data:") and line[5:].strip()]


def run_sessions(token: str, script):
    """Run `script(new_session)` against the projects connection; `new_session()`
    opens an MCP session and returns `call(method, params) -> (result, messages)`."""
    inner = gateway_mcp.http_app(path="/", transport="streamable-http")
    projects_app = project_mcp.http_app(path="/", transport="streamable-http")
    app = Starlette(routes=[Mount("/api/mcp/projects", app=ProjectMcpDispatch(projects_app, gateway=inner))])
    headers = {"Accept": "application/json, text/event-stream", "Authorization": f"Bearer {token}"}

    async def main():
        async with inner.router.lifespan_context(inner):
            async with httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url=BASE) as c:
                ids = iter(range(1, 10_000))

                async def new_session():
                    r = await c.post(URL, headers=headers, json={
                        "jsonrpc": "2.0", "id": next(ids), "method": "initialize",
                        "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                   "clientInfo": {"name": "test", "version": "1"}}})
                    assert r.status_code == 200, r.text
                    sid = r.headers["mcp-session-id"]
                    h = {**headers, "mcp-session-id": sid}
                    await c.post(URL, headers=h, json={"jsonrpc": "2.0", "method": "notifications/initialized"})

                    async def call(method, params=None):
                        r = await c.post(URL, headers=h, json={"jsonrpc": "2.0", "id": next(ids),
                                                              "method": method, "params": params or {}})
                        msgs = _messages(r)
                        reply = next(m for m in msgs if "id" in m)
                        return reply.get("result") or reply.get("error"), msgs
                    return call

                return await script(new_session)
    return async_to_sync(main)()


def _names(result) -> set[str]:
    return {t["name"] for t in result["tools"]}


def _text(result) -> str:
    return result["content"][0]["text"]


def test_one_fixed_set_of_tools_reads_any_projects_knowledge_base(world, s3, tmp_path):
    tok = _token(_reader())

    async def script(new_session):
        call = await new_session()
        return (await call("tools/list"))[0]

    before = run_sessions(tok, script)
    _publish(world, s3, tmp_path)

    async def script2(new_session):
        call = await new_session()
        listed, _ = await call("tools/list")
        found, _ = await call("tools/call", {"name": "projects", "arguments": {}})
        card, _ = await call("tools/call", {"name": "open_project", "arguments": {"project": "connect/givewell-kb"}})
        other = await new_session()  # a call stands alone: nothing to have opened first
        rows, _ = await other("tools/call", {"name": "kb_table", "arguments": {
            "project": "connect/givewell-kb", "name": "grants", "match": {"Recipient": "Dimagi"}}})
        hits, _ = await other("tools/call", {"name": "kb_search", "arguments": {
            "project": "givewell-kb", "query": "cholera"}})
        return listed, found, card, rows, hits

    listed, found, card, rows, hits = run_sessions(tok, script2)

    assert _names(before) == _names(listed) == ALL_TOOLS  # the same tools, however many projects
    assert {"kb_search", "kb_table", "kb_info", "kb_original"} <= KB_TOOLS
    assert "connect/givewell-kb — GiveWell Knowledge Base — knowledge base" in _text(found)
    assert 'read it with the kb_ tools, project="connect/givewell-kb"' in _text(card)
    assert rows["structuredContent"]["result"]["rows"][0]["Grant"] == "Water pilot"
    assert "WASHmobile" in _text(hits)


def test_a_projects_card_lists_its_skills_servers_and_plugins_and_a_skill_reads_on_use(world):
    project = world["project"]
    ProjectService.objects.create(project=project, kind="skill", name="cite-givewell",
                                  description="How to cite a GiveWell figure",
                                  instructions="Quote the figure, then link the page it came from.")
    ProjectService.objects.create(project=project, kind="skill", name="from-the-repo",
                                  url="https://github.com/dimagi-internal/eva/tree/main/skills/x")
    ProjectService.objects.create(project=project, kind="mcp", name="kindora", url="https://mcp.kindora.co/mcp")
    ProjectService.objects.create(project=project, kind="plugin", name="chrome-sales",
                                  url="https://github.com/dimagi-internal/chrome-sales")
    tok = _token(world["owner"])

    async def script(new_session):
        call = await new_session()
        listed, _ = await call("tools/call", {"name": "projects", "arguments": {"query": "givewell"}})
        card, _ = await call("tools/call", {"name": "open_project", "arguments": {"project": "givewell-kb"}})
        no_kb, _ = await call("tools/call", {"name": "kb_search",
                                             "arguments": {"project": "givewell-kb", "query": "x"}})
        skill, _ = await call("tools/call", {"name": "project_skill",
                                             "arguments": {"project": "connect/givewell-kb", "skill": "cite-givewell"}})
        elsewhere, _ = await call("tools/call", {"name": "project_skill",
                                                 "arguments": {"project": "givewell-kb", "skill": "from-the-repo"}})
        missing, _ = await call("tools/call", {"name": "project_skill",
                                               "arguments": {"project": "givewell-kb", "skill": "nope"}})
        return listed, card, no_kb, skill, elsewhere, missing

    listed, card, no_kb, skill, elsewhere, missing = run_sessions(tok, script)

    assert "2 skills, 1 shared MCP server, 1 plugin" in _text(listed)
    text = _text(card)
    assert "- cite-givewell — How to cite a GiveWell figure" in text
    assert "claude mcp add --transport http kindora https://mcp.kindora.co/mcp" in text
    assert "/plugin marketplace add https://github.com/dimagi-internal/chrome-sales" in text
    assert "kb_ tools" not in text  # it has published none
    assert no_kb.get("isError") and "has no knowledge base you may read" in _text(no_kb)
    assert _text(skill).endswith("Quote the figure, then link the page it came from.")
    assert "they live at https://github.com/dimagi-internal/eva" in _text(elsewhere)
    assert missing.get("isError") and "its skills: cite-givewell, from-the-repo" in _text(missing)


def test_a_stranger_finds_nothing_and_cannot_open(world, s3, tmp_path):
    _publish(world, s3, tmp_path)
    tok = _token(User.objects.create_user("s", "s@else.com", "pw"))

    async def script(new_session):
        call = await new_session()
        listed, _ = await call("tools/call", {"name": "projects", "arguments": {}})
        opened, _ = await call("tools/call", {"name": "open_project", "arguments": {"project": "connect/givewell-kb"}})
        read, _ = await call("tools/call", {"name": "kb_search",
                                            "arguments": {"project": "connect/givewell-kb", "query": "cholera"}})
        return listed, opened, read

    listed, opened, read = run_sessions(tok, script)

    assert "No project you may open carries anything" in _text(listed)
    assert opened.get("isError") and read.get("isError")
    assert "WASHmobile" not in _text(read)


def test_a_project_carrying_nothing_is_not_listed(world):
    tok = _token(world["owner"])

    async def script(new_session):
        call = await new_session()
        return (await call("tools/call", {"name": "projects", "arguments": {}}))[0]

    assert "No project you may open carries anything" in _text(run_sessions(tok, script))


def test_a_turn_on_a_projects_task_is_told_to_open_it(world):
    project = world["project"]
    task = AgentTask.objects.create(agent=world["eva"], title="Check the cholera figure", project=project)
    assert "open_project" not in task_turn_prompt(task, action="approve")

    ProjectService.objects.create(project=project, kind="skill", name="cite-givewell", instructions="x")
    brief = task_turn_prompt(AgentTask.objects.get(pk=task.pk), action="approve")

    assert 'Its toolbox (1 skill): open it first with open_project("connect/givewell-kb").' in brief


def test_a_skill_is_its_instructions_and_a_server_needs_its_url(world):
    c = world["client"]
    url = "/api/workspace-projects/connect/givewell-kb/services/"

    skill = c.post(url, {"kind": "skill", "name": "cite", "instructions": "Quote, then link."},
                   content_type="application/json")
    server = c.post(url, {"kind": "mcp", "name": "kindora"}, content_type="application/json")
    stray = c.post(url, {"kind": "mcp", "name": "x", "url": "https://x.test/mcp", "instructions": "no"},
                   content_type="application/json")

    assert skill.status_code == 201, skill.content
    assert skill.json()["instructions"] == "Quote, then link." and skill.json()["url"] == ""
    assert server.status_code == 422 and "needs its url" in server.json()["detail"]
    assert stray.status_code == 422
    assert AgentProject.objects.get(handle="givewell-kb").served.filter(kind="skill").count() == 1

"""What a project serves to its workspace (`ProjectService`): its knowledge base,
and the MCP servers, skills and plugins it shares — listed on the project and, for
an MCP server, in the workspace's directory."""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentProject, ProjectService
from apps.workspaces.models import Workspace, WorkspaceMembership

J = "application/json"
BASE = "/api/workspace-projects/dimagi/funders"


@pytest.fixture()
def w(db):
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    viewer = User.objects.create_user("v", "v@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=viewer, workspace=ws, role=WorkspaceMembership.VIEWER)
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner)
    project = AgentProject.objects.create(agent=eva, ext_id="P1", name="Funders", handle="funders")
    c, vc = Client(), Client()
    c.force_login(owner)
    vc.force_login(viewer)
    return {"c": c, "vc": vc, "project": project}


def _add(c, **body):
    return c.post(f"{BASE}/services/", body, content_type=J)


def test_a_project_shares_an_mcp_server_and_the_directory_lists_it(w):
    r = _add(w["c"], kind="mcp", name="Salesforce", url="https://mcp.example.org/sf", description="Our CRM")

    assert r.status_code == 201, r.content
    assert (r.json()["name"], r.json()["add_command"]) == (
        "salesforce", "claude mcp add --transport http salesforce https://mcp.example.org/sf")
    [row] = w["vc"].get("/api/mcp-servers/").json()
    assert (row["kind"], row["name"], row["address"], row["url"]) == (
        "mcp", "salesforce", "dimagi/funders", "https://mcp.example.org/sf")
    detail = w["vc"].get(f"{BASE}/").json()
    assert [s["kind"] for s in detail["services"]] == ["mcp"]


def test_skills_and_plugins_are_listed_on_the_project_not_the_mcp_directory(w):
    assert _add(w["c"], kind="skill", name="grant-writing", url="https://github.com/dimagi/skills/grant").status_code == 201
    assert _add(w["c"], kind="plugin", name="funders", url="https://github.com/dimagi/funders-plugin").status_code == 201

    kinds = [(s["kind"], s["name"]) for s in w["vc"].get(f"{BASE}/services/").json()]
    assert kinds == [("plugin", "funders"), ("skill", "grant-writing")]
    assert w["vc"].get("/api/mcp-servers/").json() == []


def test_only_those_who_may_change_the_project_share_from_it(w):
    assert _add(w["vc"], kind="mcp", name="x", url="https://x.example.org").status_code == 403


def test_a_name_is_unique_per_kind_and_a_url_must_be_web(w):
    assert _add(w["c"], kind="mcp", name="sf", url="https://a.example.org").status_code == 201
    assert _add(w["c"], kind="mcp", name="sf", url="https://b.example.org").status_code == 422
    assert _add(w["c"], kind="mcp", name="other", url="file:///etc/passwd").status_code == 422
    assert _add(w["c"], kind="mcp", name="Not A Name!", url="https://c.example.org").status_code == 422


def test_change_and_remove_a_shared_server_but_not_the_knowledge_base(w):
    sid = _add(w["c"], kind="mcp", name="sf", url="https://a.example.org").json()["id"]
    kb = ProjectService.objects.create(project=w["project"], kind="kb", name="kb")

    changed = w["c"].patch(f"{BASE}/services/{sid}", {"url": "https://b.example.org"}, content_type=J)
    kept = w["c"].delete(f"{BASE}/services/{kb.pk}")
    gone = w["c"].delete(f"{BASE}/services/{sid}")

    assert changed.json()["url"] == "https://b.example.org"
    assert kept.status_code == 422 and gone.status_code == 204
    assert list(ProjectService.objects.values_list("kind", flat=True)) == ["kb"]


def test_a_published_knowledge_base_reads_through_the_project(w):
    ProjectService.objects.create(project=w["project"], kind="kb", name="kb", kb_key="k", kb_sha256="a" * 64)
    row = w["vc"].get(f"{BASE}/").json()
    assert (row["mcp_kind"], row["mcp_path"]) == ("kb", "/api/mcp/projects/dimagi/funders/")
    [svc] = row["services"]
    assert svc["kind"] == "kb" and svc["add_command"].startswith("claude mcp add --transport http funders ")

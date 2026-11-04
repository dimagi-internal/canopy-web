"""The routes that change THE AGENT ITSELF gate on `agent_edit` on that agent, not on the workspace.

Publishing skills, re-reading skill history and editing what an agent says about itself need
`access.may_edit_agent`: the owner, an admin, the agent's own login, or an access-list line whose
role holds `agent_edit`. A workspace editor with no such line, a read_write or read_only line, a
viewer and a contact are refused, with a reason naming who can give them the role; a non-member
still gets 404. REST and MCP are one route.
"""
from __future__ import annotations

import contextlib
from unittest import mock

import pytest
from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import AuthenticatedUser, auth_context_var

from apps.agents import roles, skill_history
from apps.agents.models import Agent, AgentGrant, AgentAdmin, AgentSkill
from apps.mcp.server import mcp
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M, G = WorkspaceMembership, AgentGrant

SKILLS = {"skills": [{"name": "setup", "description": "once"}]}


@contextlib.contextmanager
def as_user(user):
    claims = {"sub": str(user.pk), "user_id": user.pk, "auth_method": "pat"}
    access = AccessToken(token="t", client_id="c", scopes=["canopy:user"], claims=claims)
    tok = auth_context_var.set(AuthenticatedUser(access))
    try:
        yield
    finally:
        auth_context_var.reset(tok)


@pytest.fixture()
def w():
    mk = get_user_model().objects.create_user
    names = ("owner", "boss", "adm", "line_ed", "line_rw", "line_ro", "ws_ed", "viewer", "out", "bot")
    u = {n: mk(n, f"{n}@dimagi.com", "pw") for n in names}
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u["boss"])
    M.objects.create(user=u["boss"], workspace=ws, role=M.OWNER)
    for n in ("owner", "adm", "line_ed", "line_rw", "line_ro", "ws_ed", "bot"):
        M.objects.create(user=u[n], workspace=ws, role=M.EDITOR)
    M.objects.create(user=u["viewer"], workspace=ws, role=M.VIEWER)
    other = Workspace.objects.create(slug="other", display_name="Other", created_by=u["out"])
    M.objects.create(user=u["out"], workspace=other, role=M.OWNER)
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=u["owner"], user=u["bot"])
    AgentAdmin.objects.create(agent=agent, user=u["adm"], granted_by=u["owner"])
    for n, role in (("line_ed", roles.EDITOR), ("line_rw", roles.READ_WRITE), ("line_ro", roles.READ_ONLY)):
        G.objects.create(agent=agent, kind=G.PERSON, user=u[n], role=role)
    return {"ws": ws, "agent": agent, **u}


def _client(user):
    c = Client()
    c.force_login(user)
    return c


def _put_skills(user):
    return _client(user).put("/api/agents/hal/skills/", data=SKILLS, content_type="application/json")


def _upsert(user, **fields):
    body = {"slug": "hal", "name": "Hal", **fields}
    return _client(user).post("/api/agents/", data=body, content_type="application/json")


ALLOWED = ("owner", "adm", "boss", "bot", "line_ed")
REFUSED = ("line_rw", "line_ro", "ws_ed", "viewer")


@pytest.mark.parametrize("who", ALLOWED)
def test_who_may_publish_skills(w, who):
    r = _put_skills(w[who])
    assert r.status_code == 200, r.content
    assert AgentSkill.objects.filter(agent=w["agent"], name="setup").exists()


@pytest.mark.parametrize("who", REFUSED)
def test_who_may_not_publish_skills(w, who):
    r = _put_skills(w[who])
    assert r.status_code == 403
    assert "owner@dimagi.com" in r.json()["detail"] and "editor role on hal" in r.json()["detail"]
    assert not AgentSkill.objects.filter(agent=w["agent"]).exists()


def test_a_non_member_still_gets_404_not_403(w):
    assert _put_skills(w["out"]).status_code == 404


def test_a_workspace_default_editor_line_keeps_workspace_editors_able(w):
    assert _put_skills(w["ws_ed"]).status_code == 403
    G.objects.create(workspace=w["ws"], kind=G.WORKSPACE, subject_workspace=w["ws"], at_least=M.EDITOR,
                     role=roles.EDITOR)
    assert _put_skills(w["ws_ed"]).status_code == 200
    assert _put_skills(w["viewer"]).status_code == 403


@pytest.mark.parametrize("who,status", [("line_ed", 200), ("owner", 200), ("ws_ed", 403), ("line_rw", 403)])
def test_forcing_a_skill_history_sync(w, who, status):
    with mock.patch.object(skill_history, "sync"):
        r = _client(w[who]).post("/api/agents/hal/skill-history/sync")
    assert r.status_code == status, r.content


def test_the_history_page_offers_sync_only_to_those_who_may(w):
    for who, expected in (("line_ed", True), ("ws_ed", False), ("viewer", False)):
        assert skill_history.history_payload(w["agent"], w[who])["viewer_can_sync"] is expected


# --- editing what the agent says about itself ---------------------------------------------

def test_a_workspace_editor_cannot_rename_the_agent_but_an_editor_line_can(w):
    assert _upsert(w["ws_ed"], name="Hal 2").status_code == 403
    assert Agent.objects.get(slug="hal").name == "Hal"
    assert _upsert(w["line_ed"], name="Hal 2").status_code == 201
    assert Agent.objects.get(slug="hal").name == "Hal 2"


def test_persona_and_description_are_the_agents_own_too(w):
    for field in ("persona", "description"):
        assert _upsert(w["line_rw"], **{field: "new"}).status_code == 403
        assert _upsert(w["adm"], **{field: "new"}).status_code == 201


def test_a_reupsert_that_changes_nothing_stays_a_workspace_editors(w):
    """The plugin re-registers an agent on every publish with the stored values."""
    assert _upsert(w["ws_ed"]).status_code == 201


def test_creating_an_agent_stays_a_workspace_editors(w):
    r = _client(w["ws_ed"]).post("/api/agents/", data={"slug": "newbie", "name": "Newbie", "workspace": "connect"},
                                 content_type="application/json")
    assert r.status_code == 201, r.content


def test_the_repo_pointer_stays_an_admins_even_with_an_editor_line(w):
    r = _upsert(w["line_ed"], repo_url="https://github.com/o/hal")
    assert r.status_code == 403 and "owner or one of its admins" in r.json()["detail"]
    assert _upsert(w["adm"], repo_url="https://github.com/o/hal").status_code == 201


# --- the detail tells the UI -----------------------------------------------------------------

@pytest.mark.parametrize("who,expected", [("owner", True), ("adm", True), ("line_ed", True),
                                          ("line_rw", False), ("ws_ed", False), ("viewer", False)])
def test_the_detail_says_whether_the_caller_may_edit(w, who, expected):
    body = _client(w[who]).get("/api/agents/hal/").json()
    assert body["can_edit_agent"] is expected


# --- MCP is the same route -------------------------------------------------------------------

def _mcp_replace_skills(user):
    with as_user(user):
        return async_to_sync(mcp.call_tool)("replace_skills", {"slug": "hal", "skills": SKILLS["skills"],
                                                                 "workspace": "connect"})


def test_the_mcp_tool_is_gated_the_same_way(w):
    assert _mcp_replace_skills(w["line_ed"]).structured_content["count"] == 1
    with pytest.raises(ToolError) as e:
        _mcp_replace_skills(w["ws_ed"])
    assert "editor role on hal" in str(e.value)
    with pytest.raises(ToolError):
        _mcp_replace_skills(w["line_rw"])


def test_agent_edit_is_enough_to_change_the_agent_whatever_the_workspace_role(w):
    """A workspace viewer with an editor line may rename it; without one, or for a new agent, they may not."""
    c = Client()
    c.force_login(w["viewer"])
    body = {"slug": "hal", "name": "Hal the second"}
    assert c.post("/api/agents/", body, content_type="application/json").status_code == 403
    G.objects.create(agent=w["agent"], kind=G.PERSON, user=w["viewer"], role=roles.EDITOR)
    assert c.post("/api/agents/", body, content_type="application/json").status_code in (200, 201)
    w["agent"].refresh_from_db()
    assert w["agent"].name == "Hal the second"
    new = {"slug": "newbie", "name": "New", "workspace": "connect"}
    assert c.post("/api/agents/", new, content_type="application/json").status_code == 403

"""The access list over HTTP: who may read it, who may change it, and what a line may say.

`docs/superpowers/specs/2026-10-30-agent-roles-and-access-list-design.md`.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents import access, roles
from apps.agents.models import Agent, AgentAdmin, AgentGrant
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M, G = WorkspaceMembership, AgentGrant


@pytest.fixture()
def w():
    mk = get_user_model().objects.create_user
    names = ("boss", "owner", "admin", "ed", "vw", "out", "rootadmin")
    u = {n: mk(n, f"{n}@dimagi.com", "pw") for n in names}
    root = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=u["boss"])
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=u["boss"], parent=root)
    M.objects.create(user=u["boss"], workspace=root, role=M.OWNER)
    M.objects.create(user=u["rootadmin"], workspace=root, role=M.ADMIN)
    for n, role in (("owner", M.EDITOR), ("admin", M.EDITOR), ("ed", M.EDITOR), ("vw", M.VIEWER)):
        M.objects.create(user=u[n], workspace=connect, role=role)
    M.objects.create(user=u["out"], workspace=root, role=M.EDITOR)         # in dimagi, not connect
    agent = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=u["owner"])
    AgentAdmin.objects.create(agent=agent, user=u["admin"], granted_by=u["owner"])
    return {"root": root, "connect": connect, "agent": agent, **u}


def _c(user):
    c = Client()
    c.force_login(user)
    return c


def _post(w, who, body, path="/api/agents/hal/access-list"):
    return _c(w[who]).post(path, body, content_type="application/json")


# --- reading ----------------------------------------------------------------------------

def test_any_member_reads_the_list_and_sees_the_roles_that_come_with_the_agent(w):
    G.objects.create(agent=w["agent"], kind=G.PERSON, user=w["vw"], role=roles.USER)
    G.objects.create(workspace=w["root"], kind=G.DOMAIN, domain="dimagi.com", role=roles.VIEWER)
    r = _c(w["vw"]).get("/api/agents/hal/access-list")
    assert r.status_code == 200
    body = r.json()
    assert body["can_edit"] is False and body["uses_access_list"] is False
    assert {(ln["scope"], ln["who"], ln["role"]) for ln in body["lines"]} == {
        ("agent", "vw@dimagi.com", "user"), ("workspace", "Anyone at @dimagi.com (verified)", "viewer")}
    assert all(ln["can_edit"] is False for ln in body["lines"])
    assert {(i["who"], i["role"]) for i in body["implicit"]} >= {("owner@dimagi.com", "owner"),
                                                                ("admin@dimagi.com", "admin")}
    assert [r["role"] for r in body["roles"]] == list(roles.LADDER)
    assert "run the whole agent" in next(r for r in body["roles"] if r["role"] == "editor")["can"]


def test_a_stranger_to_the_workspace_gets_a_404_not_a_403(w):
    stranger = get_user_model().objects.create_user("s", "s@elsewhere.org", "pw")
    assert _c(stranger).get("/api/agents/hal/access-list").status_code == 404


# --- who may change it ------------------------------------------------------------------

@pytest.mark.parametrize("who,status", [("owner", 201), ("admin", 201), ("boss", 201), ("ed", 403), ("vw", 403)])
def test_only_the_agents_admins_add_lines(w, who, status):
    r = _post(w, who, {"kind": "person", "role": "user", "email": "vw@dimagi.com"})
    assert r.status_code == status, r.content
    assert AgentGrant.objects.filter(agent=w["agent"]).count() == (1 if status == 201 else 0)


def test_a_line_takes_effect_at_once(w):
    assert access.decide(w["agent"], w["vw"], verified=True).access == access.NONE
    r = _post(w, "owner", {"kind": "person", "role": "editor", "email": "VW@dimagi.com"})
    assert r.status_code == 201 and r.json()["who"] == "vw@dimagi.com"
    d = access.decide(w["agent"], w["vw"], verified=True)
    assert (d.access, d.agent_role) == (access.FULL, roles.EDITOR)


def test_a_group_line_for_another_workspace_the_adder_is_in(w):
    r = _post(w, "owner", {"kind": "workspace", "role": "editor", "workspace": "connect", "at_least": "editor"})
    assert r.status_code == 201 and r.json()["who"] == "Everyone in connect, editor or above"


# --- what a line may say ----------------------------------------------------------------

@pytest.mark.parametrize("body,fragment", [
    ({"kind": "domain", "role": "editor", "domain": "dimagi.com"}, "viewer or user, not editor"),
    ({"kind": "contact", "role": "editor"}, "viewer or user, not editor"),
    ({"kind": "domain", "role": "viewer"}, "needs the domain"),
    ({"kind": "domain", "role": "viewer", "domain": "not a domain"}, "is not a domain"),
    ({"kind": "domain", "role": "viewer", "domain": "dimagi.com", "verified": False}, None),
    ({"kind": "person", "role": "user", "email": "nobody@nowhere.org"}, "no such person"),
    ({"kind": "workspace", "role": "user", "workspace": "secret"}, "not in a workspace"),
    ({"kind": "workspace", "role": "user", "workspace": "connect", "at_least": "god"}, "workspace role"),
])
def test_a_line_that_would_admit_too_much_or_name_nothing_is_refused(w, body, fragment):
    r = _post(w, "owner", body)
    if fragment is None:
        assert r.status_code == 201                     # an unproven line for one domain is allowed (viewer)
    else:
        assert r.status_code == 422 and fragment in r.json()["detail"], r.content


def test_a_person_is_only_found_among_workspaces_the_adder_can_see(w):
    lone = get_user_model().objects.create_user("lone", "lone@elsewhere.org", "pw")
    other = Workspace.objects.create(slug="private", display_name="Private", created_by=lone)
    M.objects.create(user=lone, workspace=other, role=M.OWNER)
    r = _post(w, "owner", {"kind": "person", "role": "user", "email": "lone@elsewhere.org"})
    assert r.status_code == 422 and "no such person" in r.json()["detail"]


def test_the_same_subject_twice_is_a_conflict_not_a_second_line(w):
    body = {"kind": "domain", "role": "viewer", "domain": "dimagi.com"}
    assert _post(w, "owner", body).status_code == 201
    assert _post(w, "owner", {**body, "role": "user"}).status_code == 409
    assert AgentGrant.objects.filter(agent=w["agent"]).count() == 1


# --- changing and removing --------------------------------------------------------------

def test_change_and_remove_a_line(w):
    line_id = _post(w, "owner", {"kind": "person", "role": "user", "email": "vw@dimagi.com"}).json()["id"]
    r = _c(w["admin"]).patch(f"/api/agents/hal/access-list/{line_id}",
                             {"role": "editor", "manual_only": True}, content_type="application/json")
    assert r.status_code == 200 and (r.json()["role"], r.json()["manual_only"]) == ("editor", True)
    assert _c(w["ed"]).delete(f"/api/agents/hal/access-list/{line_id}").status_code == 403
    assert _c(w["admin"]).delete(f"/api/agents/hal/access-list/{line_id}").status_code == 204
    assert not AgentGrant.objects.filter(agent=w["agent"]).exists()


def test_an_address_line_cannot_be_promoted_to_editor(w):
    line_id = _post(w, "owner", {"kind": "domain", "role": "user", "domain": "dimagi.com"}).json()["id"]
    r = _c(w["owner"]).patch(f"/api/agents/hal/access-list/{line_id}", {"role": "editor"},
                             content_type="application/json")
    assert r.status_code == 422


def test_an_inherited_line_is_not_changed_from_the_agent(w):
    d = AgentGrant.objects.create(workspace=w["root"], kind=G.DOMAIN, domain="dimagi.com", role=roles.VIEWER)
    r = _c(w["owner"]).delete(f"/api/agents/hal/access-list/{d.pk}")
    assert r.status_code == 404 and "inherited" in r.json()["detail"]


def test_opting_out_of_the_workspace_defaults(w):
    AgentGrant.objects.create(workspace=w["root"], kind=G.DOMAIN, domain="dimagi.com", role=roles.VIEWER)
    r = _c(w["owner"]).put("/api/agents/hal/inherit-workspace-defaults", {"inherit": False}, content_type="application/json")
    assert r.status_code == 200 and r.json()["inherit_workspace_defaults"] is False
    assert [ln for ln in r.json()["lines"] if ln["scope"] == "workspace"] == []
    assert _c(w["ed"]).put("/api/agents/hal/inherit-workspace-defaults", {"inherit": True},
                           content_type="application/json").status_code == 403


# --- a workspace's defaults -------------------------------------------------------------

def test_workspace_defaults_belong_to_workspace_admins_and_editor_defaults_to_its_owner(w):
    url = "/api/workspaces/dimagi/agent-defaults"
    ok = {"kind": "domain", "role": "viewer", "domain": "dimagi.com"}
    assert _post(w, "ed", ok, url).status_code in (403, 404)
    r = _post(w, "rootadmin", ok, url)
    assert r.status_code == 201 and r.json()["scope"] == "workspace"
    everyone = {"kind": "workspace", "role": "editor", "workspace": "dimagi", "at_least": "editor"}
    assert _post(w, "rootadmin", everyone, url).status_code == 403          # an admin cannot hand out editor
    assert _post(w, "boss", everyone, url).status_code == 201               # the owner can
    assert len(_c(w["boss"]).get(url).json()) == 2


def test_a_default_reaches_the_agent_and_removing_it_takes_it_back(w):
    from apps.agents.interface import parse

    Agent.objects.filter(pk=w["agent"].pk).update(
        interface=parse({"capabilities": {"ask": {"callers": [], "role": "viewer", "tools": ["Read"]}}}))
    w["agent"].refresh_from_db()
    r = _post(w, "boss", {"kind": "workspace", "role": "user", "workspace": "dimagi"},
              "/api/workspaces/dimagi/agent-defaults")
    assert r.status_code == 201
    d = access.decide(w["agent"], w["out"], verified=True)          # `out` is in dimagi, not connect
    assert (d.access, d.capability, d.agent_role) == (access.CONFINED, "ask", roles.USER)
    listed = _c(w["owner"]).get("/api/agents/hal/access-list").json()["lines"]
    assert [ln["scope"] for ln in listed] == ["workspace"]
    assert _c(w["boss"]).delete(f"/api/workspaces/dimagi/agent-defaults/{r.json()['id']}").status_code == 204
    assert _c(w["owner"]).get("/api/agents/hal/access-list").json()["lines"] == []

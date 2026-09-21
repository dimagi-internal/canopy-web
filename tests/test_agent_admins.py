"""Agent admins — an explicit, per-agent grant of the whole agent (phase 3 of who-is-asking).

"May hold this agent's keys" used to be a WORKSPACE role. The role that mattered,
`editor`, is handed to anyone from an allowed domain who clicks "join", so the
credentials gate fell back to workspace owners only. An admin is the deliberate
grant that was missing: the agent's owner, the workspace's owners, and whoever
they name — never an editor by default.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.models import Agent, AgentAdmin
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
M = WorkspaceMembership


@pytest.fixture()
def world():
    boss = User.objects.create_user("boss", "boss@dimagi.com", "pw")    # workspace owner
    op = User.objects.create_user("op", "op@dimagi.com", "pw")          # the agent's owner
    ed = User.objects.create_user("ed", "ed@dimagi.com", "pw")          # a self-joined editor
    out = User.objects.create_user("out", "out@else.org", "pw")         # not a member
    ws = Workspace.objects.create(slug="connect", display_name="Connect", created_by=boss)
    for u, role in ((boss, M.OWNER), (op, M.EDITOR), (ed, M.EDITOR)):
        M.objects.create(user=u, workspace=ws, role=role)
    agent = Agent.objects.create(slug="ace", name="Ace", workspace=ws, owner=op)
    return {"boss": boss, "op": op, "ed": ed, "out": out, "ws": ws, "agent": agent}


def _as(user) -> Client:
    c = Client()
    c.force_login(user)
    return c


def _creds(user):
    return _as(user).put("/api/agents/ace/credentials",
                         data={"values": {"TOKEN": "x"}}, content_type="application/json")


# --- who is an admin ------------------------------------------------------------

def test_owner_and_workspace_owner_are_admins_an_editor_is_not(world):
    a = world["agent"]
    assert a.is_admin(world["op"]) and a.is_admin(world["boss"])
    assert not a.is_admin(world["ed"]) and not a.is_admin(world["out"])


def test_a_grant_makes_an_editor_an_admin(world):
    AgentAdmin.objects.create(agent=world["agent"], user=world["ed"])
    assert world["agent"].is_admin(world["ed"])


def test_a_grant_whose_holder_left_the_workspace_is_inert(world):
    AgentAdmin.objects.create(agent=world["agent"], user=world["ed"])
    M.objects.filter(user=world["ed"]).delete()
    assert not world["agent"].is_admin(world["ed"])
    assert [r["email"] for r in _as(world["boss"]).get("/api/agents/ace/admins").json()] \
        == ["op@dimagi.com"]


# --- the credentials gate (D7) --------------------------------------------------

def test_the_agents_own_owner_may_now_set_its_credentials(world):
    """The point of D7: before this, an agent's own operator could not set its
    keys unless they also happened to own the whole workspace."""
    assert _creds(world["op"]).status_code == 200


def test_an_editor_may_not_until_granted(world):
    assert _creds(world["ed"]).status_code == 403
    AgentAdmin.objects.create(agent=world["agent"], user=world["ed"])
    assert _creds(world["ed"]).status_code == 200


def test_a_workspace_owner_keeps_the_gate(world):
    assert _creds(world["boss"]).status_code == 200


def test_a_non_member_still_gets_404_not_403(world):
    assert _creds(world["out"]).status_code == 404


# --- granting and revoking -------------------------------------------------------

def test_the_agent_owner_grants_and_revokes(world):
    c = _as(world["op"])
    r = c.put(f"/api/agents/ace/admins/{world['ed'].pk}")
    assert r.status_code == 200, r.content
    row = next(x for x in r.json() if x["email"] == "ed@dimagi.com")
    assert row["granted_by_email"] == "op@dimagi.com"
    assert c.put(f"/api/agents/ace/admins/{world['ed'].pk}").status_code == 200  # idempotent
    assert AgentAdmin.objects.count() == 1
    r = c.delete(f"/api/agents/ace/admins/{world['ed'].pk}")
    assert r.status_code == 200
    assert not world["agent"].is_admin(world["ed"])


def test_an_admin_cannot_mint_more_admins(world):
    """Granting admin hands over the keys, so it is held to the transfer bar:
    the agent's owner or a workspace owner — not a peer admin."""
    AgentAdmin.objects.create(agent=world["agent"], user=world["ed"])
    other = User.objects.create_user("o2", "o2@dimagi.com", "pw")
    M.objects.create(user=other, workspace=world["ws"], role=M.VIEWER)
    assert _as(world["ed"]).put(f"/api/agents/ace/admins/{other.pk}").status_code == 403


def test_only_a_member_can_be_granted(world):
    r = _as(world["op"]).put(f"/api/agents/ace/admins/{world['out'].pk}")
    assert r.status_code == 422


def test_the_owner_cannot_be_revoked(world):
    r = _as(world["boss"]).delete(f"/api/agents/ace/admins/{world['op'].pk}")
    assert r.status_code == 422


def test_a_token_cannot_change_admins(world):
    """A PAT, a widget's delegated token, a contact token: any Authorization
    header means a machine is in the loop, and admin is a person's decision."""
    raw, _ = PersonalToken.create_for_user(user=world["op"], label="t")
    c = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert c.put(f"/api/agents/ace/admins/{world['ed'].pk}").status_code == 403


def test_the_detail_says_what_the_caller_may_do(world):
    d = _as(world["ed"]).get("/api/agents/ace/").json()
    assert (d["is_admin"], d["can_manage_admins"]) == (False, False)
    d = _as(world["op"]).get("/api/agents/ace/").json()
    assert (d["is_admin"], d["can_manage_admins"]) == (True, True)


def test_any_member_can_see_who_holds_the_keys(world):
    rows = _as(world["ed"]).get("/api/agents/ace/admins").json()
    assert rows == [{"user_id": world["op"].pk, "email": "op@dimagi.com", "name": "op@dimagi.com",
                     "is_owner": True, "granted_by_email": None, "granted_at": None}]
    assert _as(world["out"]).get("/api/agents/ace/admins").status_code == 404

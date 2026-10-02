"""GET /api/workspaces/{slug}/agent-topology — what each agent's login gets from every other agent."""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents import interface
from apps.agents.models import Agent, AgentAdmin
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db
User = get_user_model()
M = WorkspaceMembership


def _user(email):
    return User.objects.create(username=email, email=email)


def _get(user, slug="dimagi"):
    c = Client()
    c.force_login(user)
    return c.get(f"/api/workspaces/{slug}/agent-topology")


@pytest.fixture
def fleet():
    """Ada (dimagi) and Hal (connect, a division). Hal's interface gives members
    only `ask` — exactly the shape that made every Ada→Hal dispatch fail."""
    jj = _user("jj@dimagi.com")
    ada_login, eva_login = _user("ada@dimagi-ai.com"), _user("eva@dimagi-ai.com")
    dimagi = Workspace.objects.create(slug="dimagi", display_name="Dimagi", created_by=jj)
    connect = Workspace.objects.create(slug="connect", display_name="Connect", created_by=jj, parent=dimagi)
    M.objects.create(workspace=dimagi, user=jj, role=M.OWNER)
    M.objects.create(workspace=dimagi, user=ada_login, role=M.EDITOR)
    M.objects.create(workspace=connect, user=ada_login, role=M.EDITOR)
    M.objects.create(workspace=dimagi, user=eva_login, role=M.EDITOR)
    ada = Agent.objects.create(slug="ada", name="Ada", workspace=dimagi, owner=jj, user=ada_login)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=connect, owner=jj,
                               interface=interface.parse({"capabilities": {"ask": {"callers": ["member"]}}}))
    eva = Agent.objects.create(slug="eva", name="Eva", workspace=dimagi, owner=jj, user=eva_login)
    return {"jj": jj, "ada": ada, "hal": hal, "eva": eva, "ada_login": ada_login}


def _edges(body):
    return {(e["source"], e["target"]): e for e in body["edges"]}


def test_member_confined_by_interface_is_what_broke_ada_to_hal(fleet):
    body = _get(fleet["jj"]).json()
    e = _edges(body)[("ada", "hal")]
    assert (e["access"], e["basis"], e["capabilities"]) == ("confined", "capabilities", ["ask"])
    assert e["can_grant"] and not e["can_revoke"]


def test_no_interface_means_full_for_a_member_and_nothing_outside(fleet):
    e = _edges(_get(fleet["jj"]).json())
    assert (e[("ada", "eva")]["access"], e[("ada", "eva")]["basis"]) == ("full", "no-interface")
    # Eva's login is not in connect: nothing, and not grantable until it joins.
    assert (e[("eva", "hal")]["access"], e[("eva", "hal")]["basis"]) == ("none", "not-member")
    assert not e[("eva", "hal")]["can_grant"]
    # Hal has no login at all, so it can send nobody anything directly.
    assert e[("hal", "ada")]["basis"] == "no-login"


def test_admin_grant_shows_as_full_and_revocable(fleet):
    AgentAdmin.objects.create(agent=fleet["hal"], user=fleet["ada_login"], granted_by=fleet["jj"])
    e = _edges(_get(fleet["jj"]).json())[("ada", "hal")]
    assert (e["access"], e["basis"], e["explicit_admin"], e["can_revoke"]) == ("full", "admin", True, True)


def test_agents_in_tree_order_with_who_reaches_them(fleet):
    body = _get(fleet["jj"]).json()
    assert [a["slug"] for a in body["agents"]] == ["ada", "eva", "hal"]
    hal = next(a for a in body["agents"] if a["slug"] == "hal")
    assert hal["full_people"] == ["jj@dimagi.com"] and hal["interface_published"]
    ada = next(a for a in body["agents"] if a["slug"] == "ada")
    assert ada["login_email"] == "ada@dimagi-ai.com"


def test_admin_but_not_owner_cannot_grant(fleet):
    ops = _user("ops@dimagi.com")
    M.objects.create(workspace=Workspace.objects.get(slug="dimagi"), user=ops, role=M.ADMIN)
    M.objects.create(workspace=Workspace.objects.get(slug="connect"), user=ops, role=M.ADMIN)
    e = _edges(_get(ops).json())[("ada", "hal")]
    assert e["access"] == "confined" and not e["can_grant"]


def test_editor_refused(fleet):
    assert _get(fleet["ada_login"]).status_code == 403

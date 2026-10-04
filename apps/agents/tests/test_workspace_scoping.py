"""End-to-end scoping of the live /api/agents surface — the Echo-safety net.

register() with no workspace → default workspace + creator membership (so Echo's
unchanged client keeps working); a domain teammate who has NOT explicitly
joined (2026-09-12: auto-join is gone — self-join replaces it, see
docs/superpowers/specs/2026-09-12-agent-instances-and-the-acl-design.md) gets
the same 404 and empty list an outsider does.
"""
from __future__ import annotations

import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.workspaces.services import DEFAULT_WORKSPACE_SLUG

pytestmark = pytest.mark.django_db
User = get_user_model()


@pytest.fixture(autouse=True)
def _domain(settings):
    settings.AUTH_ALLOWED_EMAIL_DOMAIN = "dimagi.com"


def _user(email, **kw):
    return User.objects.create(username=email, email=email, **kw)


def _client(u):
    c = Client()
    c.force_login(u)
    return c


def _post(c, url, data):
    return c.post(url, data=json.dumps(data), content_type="application/json")


def _register_echo(client):
    return _post(client, "/api/agents/", {"slug": "echo", "name": "Echo", "email": "echo@dimagi-ai.com"})


def test_register_without_workspace_assigns_default_and_keeps_creator_in():
    jj = _user("jj@dimagi.com", is_superuser=True)  # the human who minted Echo's PAT
    assert _register_echo(_client(jj)).status_code == 201
    echo = Agent.objects.get(slug="echo")
    assert echo.workspace_id == DEFAULT_WORKSPACE_SLUG
    # Echo's live calls (detail + the task-board drain) still work for the PAT human
    assert _client(jj).get("/api/agents/echo/").status_code == 200
    assert _client(jj).get("/api/agents/echo/tasks/").status_code == 200


def test_domain_teammate_no_longer_auto_joins_gets_404_and_empty_list():
    """Rewrite of `test_domain_teammate_auto_joins_and_sees_agent`: that test
    asserted auto-join behaviour that no longer exists. A same-domain
    teammate who has never explicitly joined (`POST /api/workspaces/{slug}
    /join`) is a non-member like any other, and gets the same 404 / empty
    list an outsider does — see `test_outsider_gets_404_and_empty_list`."""
    jj = _user("jj@dimagi.com", is_superuser=True)
    _register_echo(_client(jj))
    teammate = _user("t@dimagi.com")  # never explicitly added
    assert _client(teammate).get("/api/agents/echo/").status_code == 404
    items = _client(teammate).get("/api/agents/").json()["items"]
    assert all(a["slug"] != "echo" for a in items)
    # And merely hitting these endpoints must not have created a membership
    # row as a side effect (that side effect is exactly what was removed).
    from apps.workspaces.models import WorkspaceMembership

    assert not WorkspaceMembership.objects.filter(user=teammate).exists()


def test_outsider_gets_404_and_empty_list():
    jj = _user("jj@dimagi.com", is_superuser=True)
    _register_echo(_client(jj))
    outsider = _user("x@other.com")
    assert _client(outsider).get("/api/agents/echo/").status_code == 404
    items = _client(outsider).get("/api/agents/").json()["items"]
    assert all(a["slug"] != "echo" for a in items)


# ---- explicit homing (payload.workspace) — the "move echo to connect" path ----

def _make_ws(slug, owner, self_join=()):
    from apps.workspaces.models import Workspace
    return Workspace.objects.create(
        slug=slug, display_name=slug.title(), created_by=owner,
        access_request_domains=list(self_join),
    )


def test_register_with_workspace_moves_an_already_homed_agent():
    from apps.workspaces import services as wsvc

    jj = _user("jj@dimagi.com", is_superuser=True)
    c = _client(jj)
    _register_echo(c)  # homed in the default workspace
    connect = _make_ws("connect", jj)
    wsvc.ensure_member(connect, jj)
    r = _post(c, "/api/agents/", {"slug": "echo", "name": "Echo", "workspace": "connect"})
    assert r.status_code == 201
    assert Agent.objects.get(slug="echo").workspace_id == "connect"
    # scoped reads follow the agent: new tenant 200, old tenant 404
    assert c.get("/api/w/connect/agents/echo/").status_code == 200
    assert c.get(f"/api/w/{DEFAULT_WORKSPACE_SLUG}/agents/echo/").status_code == 404


def test_register_with_unknown_workspace_404s_and_does_not_move():
    jj = _user("jj@dimagi.com", is_superuser=True)
    c = _client(jj)
    _register_echo(c)
    r = _post(c, "/api/agents/", {"slug": "echo", "name": "Echo", "workspace": "nope"})
    assert r.status_code == 404
    assert Agent.objects.get(slug="echo").workspace_id == DEFAULT_WORKSPACE_SLUG


def test_register_with_nonmember_workspace_404s_and_does_not_move():
    jj = _user("jj@dimagi.com", is_superuser=True)
    other = _user("owner@other.com")
    _make_ws("private", other)  # exists, but jj is not a member (no self-join domain)
    c = _client(jj)
    _register_echo(c)
    r = _post(c, "/api/agents/", {"slug": "echo", "name": "Echo", "workspace": "private"})
    assert r.status_code == 404
    assert Agent.objects.get(slug="echo").workspace_id == DEFAULT_WORKSPACE_SLUG


# ---- flat (unpinned) upsert homes only on CREATE, membership-bound ----

def test_flat_reregister_of_an_existing_agent_leaves_its_home_alone():
    """Echo re-registers flat on every sync. A member of the agent's workspace
    who is NOT in the org default (and has several memberships, so no default
    is resolvable for them) must still update it, without moving it."""
    from apps.workspaces import services as wsvc

    jj = _user("jj@dimagi.com", is_superuser=True)
    wsvc.ensure_default_workspace()  # `dimagi` exists; the member below is not in it
    member = _user("m@partner.org")
    connect = _make_ws("connect", jj)
    other = _make_ws("other", jj)
    wsvc.ensure_member(connect, member)
    wsvc.ensure_member(other, member)
    c = _client(member)
    r = _post(c, "/api/w/connect/agents/", {"slug": "echo", "name": "Echo"})
    assert r.status_code == 201, r.content
    r = _post(c, "/api/agents/", {"slug": "echo", "name": "Echo v2"})
    assert r.status_code == 201, r.content
    echo = Agent.objects.get(slug="echo")
    assert echo.workspace_id == "connect"
    assert echo.name == "Echo v2"


def test_flat_create_lands_in_a_workspace_the_caller_is_in_not_the_default():
    from apps.workspaces import services as wsvc

    jj = _user("jj@dimagi.com", is_superuser=True)
    wsvc.ensure_default_workspace()
    member = _user("m@partner.org")
    wsvc.ensure_member(_make_ws("connect", jj), member)  # sole membership
    r = _post(_client(member), "/api/agents/", {"slug": "newbie", "name": "Newbie"})
    assert r.status_code == 201, r.content
    assert Agent.objects.get(slug="newbie").workspace_id == "connect"


def test_flat_create_with_an_ambiguous_home_422s_and_writes_nothing():
    from apps.workspaces import services as wsvc

    jj = _user("jj@dimagi.com", is_superuser=True)
    wsvc.ensure_default_workspace()
    member = _user("m@partner.org")
    wsvc.ensure_member(_make_ws("connect", jj), member)
    wsvc.ensure_member(_make_ws("other", jj), member)
    r = _post(_client(member), "/api/agents/", {"slug": "newbie", "name": "Newbie"})
    assert r.status_code == 422
    assert not Agent.objects.filter(slug="newbie").exists()

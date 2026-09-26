"""An agent is linked to the canopy user it IS (`Agent.user`); one user, one instance.

It was only ever set by migration `agents/0022`, matching on `Agent.email`, so
echo, ace and eva — created with a blank email — had no link and no way to get
one. Every "is this caller the agent itself" check then refused the agent's own
token: its readiness report (404, cloud-ec2-1 2026-09-26), secrets shared in its
chats, and the page its chats are on.
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.tokens.models import PersonalToken
from apps.workspaces import services as wsvc
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_workspace

pytestmark = pytest.mark.django_db


def _user(name, email=None):
    return get_user_model().objects.create_user(name, email or f"{name}@x.org", "pw")


@pytest.fixture
def ws():
    return a_workspace("tenant")


@pytest.fixture
def owner(ws):
    u = _user("olive", "olive@dimagi.com")
    wsvc.ensure_member(ws, u, WorkspaceMembership.OWNER)
    return u


@pytest.fixture
def ace_user(ws):
    u = _user("ace", "ace@dimagi-ai.com")
    wsvc.ensure_member(ws, u, WorkspaceMembership.EDITOR)
    return u


def _agent(ws, owner, slug):
    return Agent.objects.create(slug=slug, name=slug, workspace=ws, owner=owner)


def _put(client, slug, user):
    body = {"user_id": user.pk if user is not None else None}
    return client.put(f"/api/agents/{slug}/canopy-user", body, content_type="application/json")


def test_the_owner_links_the_agents_canopy_user_and_its_blank_email_is_filled(ws, owner, ace_user):
    agent = _agent(ws, owner, "ace")
    c = Client()
    c.force_login(owner)
    r = _put(c, "ace", ace_user)
    assert r.status_code == 200, r.content
    assert r.json()["canopy_user"]["email"] == "ace@dimagi-ai.com"
    agent.refresh_from_db()
    assert agent.user == ace_user and agent.email == "ace@dimagi-ai.com"

    assert _put(c, "ace", None).json()["canopy_user"] is None
    agent.refresh_from_db()
    assert agent.user is None


def test_one_canopy_user_is_one_instance(ws, owner, ace_user):
    """Of several ACE instances, exactly one can BE ace@dimagi-ai.com — and the
    refusal says which one already is."""
    _agent(ws, owner, "ace")
    _agent(ws, owner, "ace-staging")
    c = Client()
    c.force_login(owner)
    assert _put(c, "ace", ace_user).status_code == 200
    r = _put(c, "ace-staging", ace_user)
    assert r.status_code == 422
    assert "already the canopy user of 'ace'" in r.content.decode()


def test_a_login_from_another_workspace_is_refused(ws, owner):
    _agent(ws, owner, "ace")
    outsider = _user("stranger", "stranger@elsewhere.org")
    wsvc.ensure_member(a_workspace("elsewhere"), outsider, WorkspaceMembership.OWNER)
    c = Client()
    c.force_login(owner)
    r = _put(c, "ace", outsider)
    assert r.status_code == 422 and "not a member" in r.content.decode()


def test_only_the_owner_or_an_admin_and_only_from_the_browser(ws, owner, ace_user):
    _agent(ws, owner, "ace")
    member = _user("mo")
    wsvc.ensure_member(ws, member, WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(member)
    assert _put(c, "ace", ace_user).status_code == 403

    # The owner's own TOKEN is refused: the linked user counts as the agent
    # itself, so linking one is a person's decision in the UI.
    raw, _ = PersonalToken.create_for_user(user=owner, label="t")
    r = Client().put("/api/agents/ace/canopy-user", {"user_id": ace_user.pk},
                     content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 403
    assert Agent.objects.get(slug="ace").user is None

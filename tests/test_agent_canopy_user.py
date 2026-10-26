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


def test_only_the_owner_or_an_admin(ws, owner, ace_user):
    _agent(ws, owner, "ace")
    member = _user("mo")
    wsvc.ensure_member(ws, member, WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(member)
    assert _put(c, "ace", ace_user).status_code == 403

    # The owner's own TOKEN may do it too (MCP does everything; gated by role).
    raw, _ = PersonalToken.create_for_user(user=owner, label="t")
    r = Client().put("/api/agents/ace/canopy-user", {"user_id": ace_user.pk},
                     content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200, r.content
    assert Agent.objects.get(slug="ace").user == ace_user


def test_a_token_asks_for_a_persons_account_but_never_links_it(ws, owner):
    _agent(ws, owner, "ace")
    pat = _person(ws, "pat")
    raw, _ = PersonalToken.create_for_user(user=owner, label="t")
    r = Client().put("/api/agents/ace/canopy-user", {"user_id": pat.pk},
                     content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200, r.content
    assert r.json()["canopy_user"] is None
    assert r.json()["canopy_user_request"]["user_email"] == "pat@dimagi.com"
    # Linking the admin's OWN signed-in account needs a passkey, which a token never has.
    from django.utils import timezone

    owner.last_login = timezone.now()
    owner.save(update_fields=["last_login"])
    r = Client().put("/api/agents/ace/canopy-user", {"user_id": owner.pk},
                     content_type="application/json", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 403
    assert Agent.objects.get(slug="ace").user is None


def _person(ws, name):
    """An account someone has signed in to — it may be a person's."""
    from django.utils import timezone

    u = _user(name, f"{name}@dimagi.com")
    u.last_login = timezone.now()
    u.save(update_fields=["last_login"])
    wsvc.ensure_member(ws, u, WorkspaceMembership.EDITOR)
    return u


def test_a_persons_account_is_linked_only_when_they_accept(ws, owner):
    from apps.passkeys.testing import grant_step_up

    agent = _agent(ws, owner, "ace")
    pat = _person(ws, "pat")
    c = Client()
    c.force_login(owner)
    r = _put(c, "ace", pat)
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["canopy_user"] is None
    assert body["canopy_user_request"]["user_email"] == "pat@dimagi.com"
    agent.refresh_from_db()
    assert agent.user is None

    # Pat sees it; a token cannot accept for them, and a browser needs a step-up.
    me = Client()
    me.force_login(pat)
    [req] = me.get("/api/me/agent-login-requests/").json()
    raw, _ = PersonalToken.create_for_user(user=pat, label="t")
    assert Client().post(f"/api/me/agent-login-requests/{req['id']}/accept",
                         HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 403
    assert me.post(f"/api/me/agent-login-requests/{req['id']}/accept").status_code == 403
    # Nobody else can accept it.
    other = Client()
    other.force_login(owner)
    grant_step_up(other)
    assert other.post(f"/api/me/agent-login-requests/{req['id']}/accept").status_code == 404
    grant_step_up(me)
    r = me.post(f"/api/me/agent-login-requests/{req['id']}/accept")
    assert r.status_code == 200, r.content
    agent.refresh_from_db()
    assert agent.user == pat
    assert me.get("/api/me/agent-login-requests/").json() == []


def test_a_declined_request_links_nothing(ws, owner):
    agent = _agent(ws, owner, "ace")
    pat = _person(ws, "pat")
    c = Client()
    c.force_login(owner)
    _put(c, "ace", pat)
    me = Client()
    me.force_login(pat)
    [req] = me.get("/api/me/agent-login-requests/").json()
    assert me.post(f"/api/me/agent-login-requests/{req['id']}/decline").status_code == 200
    agent.refresh_from_db()
    assert agent.user is None
    assert me.post(f"/api/me/agent-login-requests/{req['id']}/accept").status_code in (403, 422)


def test_an_admin_links_their_own_signed_in_account_after_a_step_up(ws, owner):
    from apps.passkeys.testing import grant_step_up

    agent = _agent(ws, owner, "ace")
    c = Client()
    c.force_login(owner)
    assert _put(c, "ace", owner).status_code == 403  # step_up_required
    grant_step_up(c)
    assert _put(c, "ace", owner).status_code == 200
    agent.refresh_from_db()
    assert agent.user == owner


def test_the_agents_token_expires():
    """An agent's own canopy token is minted with the PAT default lifetime, never forever."""
    from django.conf import settings
    from django.utils import timezone

    from apps.agents import services

    ws = a_workspace("t2")
    o = _user("own2", "own2@dimagi.com")
    wsvc.ensure_member(ws, o, WorkspaceMembership.OWNER)
    login = _user("bot2", "bot2@dimagi-ai.com")
    wsvc.ensure_member(ws, login, WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="bot2", name="b", workspace=ws, owner=o, user=login)
    _, token, _, _ = services.mint_agent_canopy_token(agent, o)
    assert token.expires_at is not None
    days = (token.expires_at - timezone.now()).days
    assert abs(days - settings.PAT_DEFAULT_TTL_DAYS) <= 1

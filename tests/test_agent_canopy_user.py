"""An agent's canopy login (`Agent.user`) is ALWAYS the account at its own email; one
user, one instance. An existing account there is linked only when its person accepts.

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


def _agent(ws, owner, slug, email=""):
    return Agent.objects.create(slug=slug, name=slug, workspace=ws, owner=owner, email=email)


def _put(client, slug, link=True, **headers):
    return client.put(f"/api/agents/{slug}/canopy-user", {"link": link},
                      content_type="application/json", **headers)


def _signed_in(user):
    from django.utils import timezone

    user.last_login = timezone.now()
    user.save(update_fields=["last_login"])
    return user


def test_with_no_account_at_its_email_canopy_creates_its_login_and_links_it(ws, owner):
    agent = _agent(ws, owner, "muse", "muse@dimagi-ai.com")
    c = Client()
    c.force_login(owner)
    r = _put(c, "muse")
    assert r.status_code == 200, r.content
    assert r.json()["canopy_user"]["email"] == "muse@dimagi-ai.com"
    agent.refresh_from_db()
    assert agent.user.email == "muse@dimagi-ai.com" and agent.user.last_login is None
    assert _put(c, "muse", link=False).json()["canopy_user"] is None


def test_an_agent_with_no_email_has_no_login(ws, owner):
    _agent(ws, owner, "labsdata")
    c = Client()
    c.force_login(owner)
    r = _put(c, "labsdata")
    assert r.status_code == 422 and "no email" in r.content.decode()


def test_a_token_links_by_role_too(ws, owner):
    _agent(ws, owner, "muse", "muse@dimagi-ai.com")
    raw, _ = PersonalToken.create_for_user(user=owner, label="t")
    r = _put(Client(), "muse", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert r.status_code == 200, r.content
    assert Agent.objects.get(slug="muse").user.email == "muse@dimagi-ai.com"


def test_only_the_owner_or_an_admin(ws, owner):
    _agent(ws, owner, "muse", "muse@dimagi-ai.com")
    member = _user("mo")
    wsvc.ensure_member(ws, member, WorkspaceMembership.EDITOR)
    c = Client()
    c.force_login(member)
    assert _put(c, "muse").status_code == 403


def test_an_existing_account_at_its_email_is_linked_only_when_its_person_accepts(ws, owner, ace_user):
    from apps.passkeys.testing import grant_step_up

    agent = _agent(ws, owner, "ace", "ace@dimagi-ai.com")
    c = Client()
    c.force_login(owner)
    r = _put(c, "ace")
    assert r.status_code == 200, r.content
    assert r.json()["canopy_user"] is None
    assert r.json()["canopy_user_request"]["user_email"] == "ace@dimagi-ai.com"

    me = Client()
    me.force_login(ace_user)
    [req] = me.get("/api/me/agent-login-requests/").json()
    raw, _ = PersonalToken.create_for_user(user=ace_user, label="t")
    assert Client().post(f"/api/me/agent-login-requests/{req['id']}/accept",
                         HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 403
    assert me.post(f"/api/me/agent-login-requests/{req['id']}/accept").status_code == 403
    other = Client()
    other.force_login(owner)
    grant_step_up(other)
    assert other.post(f"/api/me/agent-login-requests/{req['id']}/accept").status_code == 404
    grant_step_up(me)
    assert me.post(f"/api/me/agent-login-requests/{req['id']}/accept").status_code == 200
    agent.refresh_from_db()
    assert agent.user == ace_user


def test_a_declined_request_links_nothing(ws, owner, ace_user):
    agent = _agent(ws, owner, "ace", "ace@dimagi-ai.com")
    c = Client()
    c.force_login(owner)
    _put(c, "ace")
    me = Client()
    me.force_login(ace_user)
    [req] = me.get("/api/me/agent-login-requests/").json()
    assert me.post(f"/api/me/agent-login-requests/{req['id']}/decline").status_code == 200
    agent.refresh_from_db()
    assert agent.user is None


def test_one_canopy_user_is_one_instance(ws, owner, ace_user):
    _agent(ws, owner, "ace", "ace@dimagi-ai.com")
    Agent.objects.filter(slug="ace").update(user=ace_user)
    _agent(ws, owner, "ace-staging", "ace@dimagi-ai.com")
    c = Client()
    c.force_login(owner)
    r = _put(c, "ace-staging")
    assert r.status_code == 422
    assert "already the canopy user of 'ace'" in r.content.decode()


def test_an_admin_whose_own_account_is_at_the_email_steps_up(ws, owner):
    from apps.passkeys.testing import grant_step_up

    agent = _agent(ws, owner, "olive-bot", owner.email)
    _signed_in(owner)
    raw, _ = PersonalToken.create_for_user(user=owner, label="t")
    assert _put(Client(), "olive-bot", HTTP_AUTHORIZATION=f"Bearer {raw}").status_code == 403
    c = Client()
    c.force_login(owner)
    assert _put(c, "olive-bot").status_code == 403  # step_up_required
    grant_step_up(c)
    assert _put(c, "olive-bot").status_code == 200
    agent.refresh_from_db()
    assert agent.user == owner


def test_a_new_email_unlinks_the_old_login_and_derives_the_new_one(ws, owner, ace_user):
    agent = _agent(ws, owner, "ace", "ace@dimagi-ai.com")
    Agent.objects.filter(pk=agent.pk).update(user=ace_user)
    c = Client()
    c.force_login(owner)
    r = c.post("/api/agents/", {"slug": "ace", "name": "ace", "email": "ace2@dimagi-ai.com"},
               content_type="application/json")
    assert r.status_code == 201, r.content
    agent.refresh_from_db()
    assert agent.user is not None and agent.user.email == "ace2@dimagi-ai.com"
    # A re-upsert with no email leaves the login alone.
    r = c.post("/api/agents/", {"slug": "ace", "name": "ace", "email": ""},
               content_type="application/json")
    agent.refresh_from_db()
    assert agent.user is not None


def test_another_accounts_link_is_refused_by_the_service(ws, owner, ace_user):
    from apps.agents import services

    _agent(ws, owner, "muse", "muse@dimagi-ai.com")
    with pytest.raises(services.AgentUserLinkError, match="account at its own email"):
        services.link_canopy_user(Agent.objects.get(slug="muse"), ace_user.pk)


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


def test_the_migration_links_only_unambiguous_never_signed_in_accounts(ws, owner, ace_user):
    import importlib

    from django.apps import apps as global_apps

    mig = importlib.import_module("apps.agents.migrations.0096_link_logins_at_agent_email")
    muse = _agent(ws, owner, "muse", "Muse@dimagi-ai.com")
    _user("muse-acct", "muse@dimagi-ai.com")
    person = _agent(ws, owner, "pal", "pal@dimagi.com")
    _signed_in(_user("pal", "pal@dimagi.com"))
    taken = _agent(ws, owner, "ace2", "ace@dimagi-ai.com")
    Agent.objects.create(slug="ace", name="ace", workspace=ws, owner=owner, email="ace@dimagi-ai.com",
                         user=ace_user)
    mig.link(global_apps, None)
    for a in (muse, person, taken):
        a.refresh_from_db()
    assert muse.user.email == "muse@dimagi-ai.com"
    assert person.user is None      # signed in interactively: may be a person's
    assert taken.user is None       # already another agent's login

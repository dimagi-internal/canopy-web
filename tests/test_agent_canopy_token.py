"""`POST /api/agents/<slug>/canopy-token` — the agent's own canopy-web token, minted for
`canopy agent vault` to write straight into the agent's vault.

Before it, `canopy-pat` came only from `manage.py create_token` on the server, so an agent
created in the browser had none and its turns ran on a person's token.

What is pinned is the impersonation guard: a token for `Agent.user` signs in AS that user,
so this must never become "mint a token for a person".
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership
from apps.workspaces.testing import a_member, a_workspace

pytestmark = pytest.mark.django_db
URL = "/api/agents/scout/canopy-token"


@pytest.fixture
def ws():
    return a_workspace("scouts")


@pytest.fixture
def owner(ws):  # the AGENT's owner — a workspace admin (may invite), not a workspace owner
    return a_member(ws, email="olive@dimagi.com", role=WorkspaceMembership.ADMIN)


@pytest.fixture
def agent(ws, owner):
    return Agent.objects.create(slug="scout", name="Scout", workspace=ws, owner=owner,
                                email="scout@dimagi-ai.com")


def _post(user, body=None):
    c = Client()
    c.force_login(user)
    return c.post(URL, body or {}, content_type="application/json")


def test_an_agent_with_no_login_gets_a_new_one_and_a_non_expiring_token(owner, agent, ws):
    resp = _post(owner)
    assert resp.status_code == 201, resp.content
    body = resp.json()
    assert (body["user_email"], body["created_user"]) == ("scout@dimagi-ai.com", True)
    agent.refresh_from_db()
    login = agent.user
    assert login.email == "scout@dimagi-ai.com" and not login.has_usable_password()
    assert WorkspaceMembership.objects.get(workspace=ws, user=login).role == WorkspaceMembership.EDITOR
    token = PersonalToken.objects.get(pk=body["token_id"])
    assert token.user == login and token.expires_at is None
    assert body["raw"] and body["raw"] not in (token.label, str(token.pk))


def test_creating_the_login_takes_someone_who_could_invite_it(ws, agent):
    """A new login is a new member, so an agent owner who is only an editor cannot make
    one — but can mint for the login once it exists and is token-only."""
    editor = a_member(ws, email="ed@dimagi.com", role=WorkspaceMembership.EDITOR)
    agent.owner = editor
    agent.save()
    resp = _post(editor)
    assert resp.status_code == 403 and "invite" in resp.json()["detail"]
    assert agent.user is None and not get_user_model().objects.filter(
        email="scout@dimagi-ai.com").exists()
    admin = a_member(ws, email="ad@dimagi.com", role=WorkspaceMembership.ADMIN)
    assert _post(admin).status_code == 403  # a workspace admin is not the AGENT's admin
    AgentAdmin.objects.create(agent=agent, user=admin)
    assert _post(admin).status_code == 201
    assert _post(editor).status_code == 201  # the login exists and has never signed in


def test_running_it_again_mints_for_the_same_login(owner, agent):
    first = _post(owner).json()
    second = _post(owner).json()
    assert second["created_user"] is False
    assert second["user_email"] == first["user_email"]
    assert second["raw"] != first["raw"]


def test_an_address_that_is_already_a_canopy_user_is_never_adopted(owner, agent, ws):
    """Otherwise an agent admin could name a colleague's address and get their token."""
    get_user_model().objects.create_user(username="scout@dimagi-ai.com", email="scout@dimagi-ai.com")
    resp = _post(owner)
    assert resp.status_code == 409
    agent.refresh_from_db()
    assert agent.user is None and not PersonalToken.objects.exists()


def test_a_linked_login_that_has_signed_in_needs_a_workspace_owner(owner, agent, ws):
    person = a_member(ws, email="scout@dimagi-ai.com", role=WorkspaceMembership.EDITOR)
    person.last_login = timezone.now()
    person.save()
    agent.user = person
    agent.save()
    assert _post(owner).status_code == 403
    assert not PersonalToken.objects.exists()
    boss = a_member(ws, email="boss@dimagi.com", role=WorkspaceMembership.OWNER)
    resp = _post(boss)
    assert resp.status_code == 201 and resp.json()["user_email"] == "scout@dimagi-ai.com"


def test_only_the_agents_admins_may_and_a_non_member_sees_nothing(agent, ws):
    other = a_member(ws, email="ed@dimagi.com", role=WorkspaceMembership.EDITOR)
    assert _post(other).status_code == 403
    outsider = get_user_model().objects.create_user(username="x@else.org", email="x@else.org")
    assert _post(outsider).status_code == 404
    assert not PersonalToken.objects.exists()


def test_with_no_address_anywhere_it_says_so(owner, agent):
    agent.email = ""
    agent.save()
    assert _post(owner).status_code == 422
    resp = _post(owner, {"email": "scout@dimagi-ai.com"})
    assert resp.status_code == 201
    agent.refresh_from_db()
    assert agent.email == "scout@dimagi-ai.com"

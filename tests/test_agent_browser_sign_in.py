"""An agent's own Google account signs in at a browser like any account
(`apps/common/auth_adapter.py`): as the agent's login, with exactly the permissions its
tokens already have. There is no switch for it."""
from __future__ import annotations

from unittest.mock import Mock

import pytest
from django.contrib.auth.models import User
from django.test import Client, RequestFactory, override_settings

from apps.agents.models import Agent
from apps.common.auth_adapter import CustomSocialAccountAdapter
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

DOMAINS = "dimagi.com,dimagi-ai.com"


@pytest.fixture()
def w():
    owner = User.objects.create_user("jj", "jj@dimagi.com", "pw")
    ws = Workspace.objects.create(slug="fleet", display_name="Fleet", created_by=owner)
    WorkspaceMembership.objects.create(user=owner, workspace=ws, role=WorkspaceMembership.OWNER)
    login = User.objects.create_user("eva@dimagi-ai.com", "eva@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=ws, role=WorkspaceMembership.EDITOR)
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=owner, user=login)
    return {"owner": owner, "ws": ws, "login": login, "agent": agent}


def _google(email="eva@dimagi-ai.com", *, is_existing=False, user=None):
    sociallogin = Mock()
    sociallogin.account = Mock()
    sociallogin.account.extra_data = {"email": email, "hd": email.rpartition("@")[2]}
    sociallogin.is_existing = is_existing
    if user is not None:
        sociallogin.user = user
    address = Mock()
    address.email, address.verified = email, True
    sociallogin.email_addresses = [address]
    sociallogin.state = {}
    sociallogin.connect = Mock()
    return sociallogin


def _sign_in(sociallogin):
    request = RequestFactory().get("/accounts/google/login/callback/")
    CustomSocialAccountAdapter().pre_social_login(request, sociallogin)
    return request


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN=DOMAINS)
def test_the_agents_google_account_signs_in_as_the_agents_login(w):
    sociallogin = _google()
    _sign_in(sociallogin)
    sociallogin.connect.assert_called_once()
    assert sociallogin.connect.call_args.args[1] == w["login"]


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN=DOMAINS)
def test_an_already_linked_account_signs_in_without_a_second_link(w):
    sociallogin = _google(is_existing=True, user=w["login"])
    _sign_in(sociallogin)
    sociallogin.connect.assert_not_called()


def test_the_session_has_the_agents_own_permissions(w):
    c = Client()
    c.force_login(w["login"])
    me = c.get("/api/me/")
    assert me.status_code == 200 and me.json()["email"] == "eva@dimagi-ai.com"
    # Not the owner's: an agent's login is not its own admin.
    assert c.get("/api/agents/eva/test-vault-key").status_code == 403


def test_there_is_no_switch(w):
    c = Client()
    c.force_login(w["owner"])
    assert c.get("/api/agents/eva/browser-sign-in").status_code == 404

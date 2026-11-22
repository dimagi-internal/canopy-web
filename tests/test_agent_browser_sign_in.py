"""Browser sign-in for testing (`apps/agents/browser_sign_in.py`).

An agent's own Google account is refused at the browser sign-in, except while a person
who administers the agent has a grant open for it: switched on at a browser behind a
passkey step-up, for 1 to 30 days, never by a token or by the agent's own login, audited
as a setting. A session made under it is the agent's login, and ends with the grant.
"""
from __future__ import annotations

from datetime import timedelta
from unittest.mock import Mock

import pytest
from allauth.core.exceptions import ImmediateHttpResponse
from django.contrib.auth.models import User
from django.contrib.auth.signals import user_logged_in
from django.test import Client, RequestFactory, override_settings
from django.utils import timezone

from apps.agents import browser_sign_in
from apps.agents.models import Agent, AgentAdmin, AgentBrowserSignIn
from apps.common.auth_adapter import AGENT_BROWSER_SIGN_IN_ATTR, CustomSocialAccountAdapter
from apps.events.models import SettingChange
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.token_helpers import bearer_headers

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
    c = Client()
    c.force_login(owner)
    grant_step_up(c)
    return {"owner": owner, "ws": ws, "login": login, "agent": agent, "c": c}


URL = "/api/agents/eva/browser-sign-in"


def _browser(user, *, step_up=True):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c


def _member(w, name, role=WorkspaceMembership.EDITOR):
    u = User.objects.create_user(name, f"{name}@dimagi.com", "pw")
    WorkspaceMembership.objects.create(user=u, workspace=w["ws"], role=role)
    return u


def _on(c, **body):
    return c.put(URL, body, content_type="application/json")


# --- the switch ---------------------------------------------------------------------


def test_it_is_off_by_default(w):
    r = w["c"].get(URL)
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["expires_at"] is None
    assert r.json()["login_email"] == "eva@dimagi-ai.com"


def test_the_owner_switches_it_on_for_seven_days_by_default_and_it_is_audited(w):
    r = _on(w["c"], reason="extension e2e")
    assert r.status_code == 200, r.content
    body = r.json()
    assert body["enabled"] is True and body["granted_by_email"] == "jj@dimagi.com"
    assert body["reason"] == "extension e2e"
    grant = AgentBrowserSignIn.objects.get(agent=w["agent"])
    assert timedelta(days=6, hours=23) < grant.expires_at - grant.granted_at <= timedelta(days=7)
    change = SettingChange.objects.filter(path__endswith="/browser-sign-in").latest("id")
    assert "browser-sign-in" in str(change.changes)


def test_days_are_one_to_thirty(w):
    assert _on(w["c"], days=0).status_code == 422
    assert _on(w["c"], days=31).status_code == 422
    assert _on(w["c"], days=30).status_code == 200
    grant = AgentBrowserSignIn.objects.get(agent=w["agent"])
    assert grant.expires_at - grant.granted_at == timedelta(days=30)


def test_switching_on_again_replaces_the_grant_one_at_a_time(w):
    _on(w["c"], days=1)
    _on(w["c"], days=7)
    assert AgentBrowserSignIn.objects.filter(agent=w["agent"], revoked_at__isnull=True).count() == 1
    assert AgentBrowserSignIn.objects.filter(agent=w["agent"]).count() == 2


def test_an_agent_admin_may(w):
    sam = _member(w, "sam")
    AgentAdmin.objects.create(agent=w["agent"], user=sam, granted_by=w["owner"])
    assert _on(_browser(sam)).status_code == 200


def test_a_workspace_owner_may(w):
    boss = _member(w, "boss", WorkspaceMembership.OWNER)
    assert _on(_browser(boss)).status_code == 200


def test_another_member_may_not(w):
    for role in (WorkspaceMembership.EDITOR, WorkspaceMembership.ADMIN):
        other = _member(w, f"m-{role}", role)
        c = _browser(other)
        assert c.get(URL).status_code == 403
        assert _on(c).status_code == 403
        assert c.delete(URL).status_code == 403
    assert not AgentBrowserSignIn.objects.exists()


def test_no_step_up_is_asked_for_one(w):
    r = _on(_browser(w["owner"], step_up=False))
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"
    assert not AgentBrowserSignIn.objects.exists()


def test_a_token_cannot_switch_it_on_the_owners_included(w):
    raw, _ = PersonalToken.create_for_user(user=w["owner"], label="t")
    r = Client().put(URL, {}, content_type="application/json", **bearer_headers(raw))
    assert r.status_code == 403
    assert not AgentBrowserSignIn.objects.exists()


def test_the_agents_own_token_never_can_even_as_its_own_admin(w):
    AgentAdmin.objects.create(agent=w["agent"], user=w["login"], granted_by=w["owner"])
    raw, _ = PersonalToken.create_for_user(user=w["login"], label="harness")
    h = bearer_headers(raw)
    assert Client().put(URL, {}, content_type="application/json", **h).status_code == 403
    assert Client().get(URL, **h).status_code == 403
    assert Client().delete(URL, **h).status_code == 403
    assert not AgentBrowserSignIn.objects.exists()


def test_a_session_made_under_the_grant_cannot_extend_it(w):
    _on(w["c"], days=1)
    c = _browser(w["login"])  # what a browser signed in as the agent holds
    assert _on(c, days=30).status_code == 403
    assert AgentBrowserSignIn.objects.get(agent=w["agent"]).expires_at < timezone.now() + timedelta(days=2)


def test_an_agent_with_no_login_has_nothing_to_switch_on(w):
    w["agent"].user = None
    w["agent"].save(update_fields=["user"])
    r = _on(w["c"])
    assert r.status_code == 409 and r.json()["error"] == "no_agent_login"


def test_switching_off_needs_no_step_up_and_is_audited(w):
    _on(w["c"])
    c = _browser(w["owner"], step_up=False)
    r = c.delete(URL)
    assert r.status_code == 200 and r.json()["enabled"] is False
    assert AgentBrowserSignIn.objects.get(agent=w["agent"]).revoked_by == w["owner"]
    assert SettingChange.objects.filter(path__endswith="/browser-sign-in", method="DELETE").exists()


def test_an_expired_grant_reads_as_off(w):
    _on(w["c"])
    AgentBrowserSignIn.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert w["c"].get(URL).json()["enabled"] is False
    assert browser_sign_in.grant_for_login(w["login"].pk) is None


# --- the sign-in ----------------------------------------------------------------------


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
def test_without_a_grant_the_agents_google_account_is_refused_as_before(w):
    sociallogin = _google()
    with pytest.raises(ImmediateHttpResponse) as exc:
        _sign_in(sociallogin)
    assert exc.value.response.status_code == 403
    assert "an agent" in exc.value.response.content.decode()
    sociallogin.connect.assert_not_called()


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN=DOMAINS)
def test_with_a_grant_it_signs_in_as_the_agents_login(w):
    _on(w["c"])
    sociallogin = _google()
    request = _sign_in(sociallogin)
    sociallogin.connect.assert_called_once()
    assert sociallogin.connect.call_args.args[1] == w["login"]
    assert getattr(request, AGENT_BROWSER_SIGN_IN_ATTR).agent == w["agent"]


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN=DOMAINS)
def test_an_already_linked_account_signs_in_without_a_second_link(w):
    _on(w["c"])
    sociallogin = _google(is_existing=True, user=w["login"])
    _sign_in(sociallogin)
    sociallogin.connect.assert_not_called()


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN=DOMAINS)
@pytest.mark.parametrize("end", ["expired", "revoked"])
def test_an_ended_grant_refuses_again(w, end):
    _on(w["c"])
    if end == "expired":
        AgentBrowserSignIn.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    else:
        w["c"].delete(URL)
    with pytest.raises(ImmediateHttpResponse):
        _sign_in(_google(is_existing=True, user=w["login"]))


@override_settings(AUTH_ALLOWED_EMAIL_DOMAIN=DOMAINS)
def test_another_agents_grant_opens_nothing(w):
    other_login = User.objects.create_user("hal@dimagi-ai.com", "hal@dimagi-ai.com", "pw")
    Agent.objects.create(slug="hal", name="Hal", workspace=w["ws"], owner=w["owner"], user=other_login)
    _on(w["c"])  # eva's
    with pytest.raises(ImmediateHttpResponse):
        _sign_in(_google("hal@dimagi-ai.com"))


# --- the session ----------------------------------------------------------------------


def _signed_in_as_agent(w):
    """A browser session the grant opened: logged in, then marked the way the
    `user_logged_in` receiver marks it."""
    grant = browser_sign_in.grant_for_login(w["login"].pk)
    c = Client()
    c.force_login(w["login"])
    request = RequestFactory().get("/")
    request.session = c.session
    setattr(request, AGENT_BROWSER_SIGN_IN_ATTR, grant)
    user_logged_in.send(sender=User, request=request, user=w["login"])
    request.session.save()
    return c, request.session


def test_the_session_is_marked_and_ends_no_later_than_the_grant(w):
    _on(w["c"], days=1)
    grant = AgentBrowserSignIn.objects.get(agent=w["agent"])
    _, session = _signed_in_as_agent(w)
    assert session[browser_sign_in.SESSION_KEY] == {"grant": grant.pk, "user": w["login"].pk}
    assert session.get_expiry_date() <= grant.expires_at


def test_an_ordinary_sign_in_is_not_marked(w):
    request = RequestFactory().get("/")
    c = _browser(w["owner"], step_up=False)
    request.session = c.session
    user_logged_in.send(sender=User, request=request, user=w["owner"])
    assert browser_sign_in.SESSION_KEY not in request.session


def test_the_session_has_the_agents_own_permissions(w):
    _on(w["c"])
    c, _ = _signed_in_as_agent(w)
    me = c.get("/api/me/")
    assert me.status_code == 200 and me.json()["email"] == "eva@dimagi-ai.com"
    # Not the owner's: an agent's login is not its own admin.
    assert c.get("/api/agents/eva/test-vault-key").status_code == 403


@pytest.mark.parametrize("end", ["revoked", "expired"])
def test_ending_the_grant_signs_the_session_out_on_its_next_request(w, end):
    _on(w["c"])
    c, _ = _signed_in_as_agent(w)
    assert c.get("/api/me/").status_code == 200
    if end == "revoked":
        assert w["c"].delete(URL).status_code == 200
    else:
        AgentBrowserSignIn.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    assert c.get("/api/me/").status_code == 401
    assert "_auth_user_id" not in c.session

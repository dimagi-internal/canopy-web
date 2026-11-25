"""An agent login's test key also confirms its OWN page permission without a passkey
(`apps/agents/credentials/login_test_key.py::may_confirm_own_host_grant`).

The agent test key (`Agent.test_vault_key`) is off by default; an agent admin turns it on
from a browser behind a passkey step-up (its gates are `test_agent_test_vault_key.py`).
While it is on, the agent's own login may confirm its OWN waiting act-as-you permission on
a host site's page without a passkey, with its own canopy credential — never the page's
panel token, never a person's permission, never another agent's page — except a sibling agent's in
the same workspace whose own test key is on too (the login lends only its own identity).
Turning the test key off stops every unexpired page permission the login holds.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from apps.agents.models import Agent
from apps.events.models import SettingChange
from apps.harness import act_as
from apps.host_grants import host_grants
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import DelegatedToken, PersonalToken
from tests.fixtures import passkey_confirm
from tests.fixtures import site_gateway as sg
from tests.token_helpers import bearer_client, bearer_headers

pytestmark = pytest.mark.django_db


@pytest.fixture()
def w():
    x = sg.world()
    login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    x["agent"].user = login
    x["agent"].save(update_fields=["user"])
    raw, _ = PersonalToken.create_for_user(user=login, label="harness")
    admin = Client()
    admin.force_login(x["owner"])
    grant_step_up(admin)
    x.update(login=login, login_pat=raw, admin=admin)
    return x


def _url(w):
    return f"/api/agents/{w['agent'].slug}/test-vault-key"


def _turn_on(w):
    r = w["admin"].put(_url(w), {"enabled": True}, content_type="application/json")
    assert r.status_code == 200, r.content
    return r.json()


def _waiting(w, **over):
    """A page's permission for the agent's own login, waiting to be confirmed."""
    fields = {"user": w["login"], "agent": w["agent"], "confirmed_at": None, **over}
    return sg.grant(w, **fields)


def _confirm(client, grant, **body):
    return client.post(f"/api/me/host-grants/{grant.pk}/confirm-as-agent", body,
                       content_type="application/json")


def _agent(w):
    return bearer_client(w["login_pat"])


# --- the switch: the agent test key ------------------------------------------------


def test_it_is_off_by_default_and_the_login_cannot_confirm(w):
    assert w["admin"].get(_url(w)).json()["enabled"] is False
    grant = _waiting(w)
    r = _confirm(_agent(w), grant)
    assert r.status_code == 403 and r.json()["error"] == "test_key_off"
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_turning_the_test_key_off_works_from_a_token_and_stops_what_it_confirmed(w):
    _turn_on(w)
    grant = _waiting(w)
    waiting = _waiting(w, scope="data:read")
    assert _confirm(_agent(w), grant).status_code == 200
    assert host_grants.live_for(user=w["login"]) == [grant]
    owner_pat, _ = PersonalToken.create_for_user(user=w["owner"], label="t")
    r = Client().put(_url(w), {"enabled": False}, content_type="application/json", **bearer_headers(owner_pat))
    assert r.status_code == 200, r.content
    assert r.json()["enabled"] is False
    for g in (grant, waiting):
        g.refresh_from_db()
        assert g.revoked_at is not None
    assert host_grants.live_for(user=w["login"]) == []
    # And with it off again, nothing new is confirmed.
    again = _waiting(w, scope="data:write")
    assert _confirm(_agent(w), again).json()["error"] == "test_key_off"


def test_there_is_no_separate_page_permission_switch(w):
    assert not hasattr(w["agent"], "test_page_permission")
    assert w["admin"].get(f"/api/agents/{w['agent'].slug}/test-page-permission").status_code == 404


def test_an_act_as_token_never_reaches_the_switch():
    assert act_as.excluded("/api/agents/ace/test-vault-key") is not None


# --- confirming -------------------------------------------------------------------


def test_with_it_on_the_login_finds_and_confirms_its_own_waiting_grant(w):
    _turn_on(w)
    grant = _waiting(w, expires_at=timezone.now() + timedelta(hours=2))
    agent = _agent(w)
    waiting = agent.get("/api/me/host-grants/waiting")
    assert waiting.status_code == 200, waiting.content
    assert [g["id"] for g in waiting.json()] == [grant.pk]
    assert waiting.json()[0]["confirmed"] is False
    r = _confirm(agent, grant)
    assert r.status_code == 200, r.content
    assert r.json()["confirmed"] is True
    grant.refresh_from_db()
    assert grant.confirmed_at is not None
    assert grant.expires_at <= grant.confirmed_at + timedelta(seconds=host_grants.CONFIRM_SECONDS)
    assert host_grants.live_for(user=w["login"]) == [grant]
    assert agent.get("/api/me/host-grants/waiting").json() == []
    change = SettingChange.objects.filter(operation__icontains="confirm_my_host_grant_as_agent").latest("id")
    assert "WITHOUT a passkey" in str(change.changes) or "WITHOUT a passkey" in change.summary
    # Confirming again has nothing to do.
    assert _confirm(agent, grant).status_code == 409


def test_the_login_may_ask_for_a_shorter_window(w):
    _turn_on(w)
    grant = _waiting(w, expires_at=timezone.now() + timedelta(hours=2))
    assert _confirm(_agent(w), grant, window_seconds=30).status_code == 200
    grant.refresh_from_db()
    assert grant.expires_at == grant.confirmed_at + timedelta(seconds=30)


def test_a_persons_grant_is_never_confirmed_by_an_agent(w):
    _turn_on(w)
    persons = sg.grant(w, confirmed_at=None)  # the visitor's, a person's
    assert _confirm(_agent(w), persons).status_code == 404
    persons.refresh_from_db()
    assert persons.confirmed_at is None


def test_a_person_is_refused_here_and_confirms_with_their_passkey(w):
    _turn_on(w)
    persons = sg.grant(w, confirmed_at=None)
    browser = Client()
    browser.force_login(w["visitor"])
    r = _confirm(browser, persons)
    assert r.status_code == 403 and r.json()["error"] == "a_person"
    persons.refresh_from_db()
    assert persons.confirmed_at is None


def test_the_panel_token_cannot_confirm_without_a_passkey(w):
    """The panel's token is held by the host site: it confirms nothing, switch or not."""
    _turn_on(w)
    grant = _waiting(w)
    raw, _ = DelegatedToken.issue(app=w["app"], user=w["login"], ttl_seconds=600, host_grant=grant)
    assert _confirm(bearer_client(raw), grant).status_code == 403
    # Not with the login's own browser session riding along either: the panel's token
    # still names the page.
    both = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    both.force_login(w["login"])
    r = _confirm(both, grant)
    assert r.status_code == 403 and r.json()["error"] == "not_own_credential"
    # And the panel's own passkey confirm still refuses a login that is not a person.
    assert passkey_confirm.begin(raw).status_code == 403
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_another_agents_page_is_not_confirmed_this_way(w):
    """A sibling agent WITHOUT its own test key on: refused, and the message says what
    would allow it."""
    _turn_on(w)
    other = Agent.objects.create(slug="other", name="Other", workspace=w["ws"], owner=w["owner"])
    grant = _waiting(w, agent=other)
    r = _confirm(_agent(w), grant)
    assert r.status_code == 403 and r.json()["error"] == "another_agents_page"
    detail = str(r.json())
    assert "test key" in detail and "workspace" in detail
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def _sibling(w, *, test_key=True, workspace=None):
    return Agent.objects.create(slug="sibling", name="Sibling", workspace=workspace or w["ws"],
                                owner=w["owner"], test_vault_key=test_key)


def test_a_sibling_agents_page_with_its_test_key_on_is_confirmed(w):
    """Both agents' test keys on, same workspace, the login's own grant: the login lends
    its own identity to the sibling agent's page."""
    _turn_on(w)
    grant = _waiting(w, agent=_sibling(w))
    r = _confirm(_agent(w), grant)
    assert r.status_code == 200, r.content
    grant.refresh_from_db()
    assert grant.confirmed_at is not None
    assert host_grants.live_for(user=w["login"]) == [grant]


def test_a_sibling_in_another_workspace_is_refused(w):
    _turn_on(w)
    elsewhere = type(w["ws"]).objects.create(slug="elsewhere", display_name="Elsewhere",
                                             created_by=w["owner"])
    grant = _waiting(w, agent=_sibling(w, workspace=elsewhere))
    r = _confirm(_agent(w), grant)
    assert r.status_code == 403 and r.json()["error"] == "another_agents_page"
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_a_sibling_page_is_refused_when_the_logins_own_test_key_is_off(w):
    grant = _waiting(w, agent=_sibling(w))
    r = _confirm(_agent(w), grant)
    assert r.status_code == 403 and r.json()["error"] == "test_key_off"
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_a_sibling_page_never_confirms_someone_elses_grant(w):
    """Both test keys on and the same workspace still lend only the login's OWN identity:
    a person's grant, or a contact's, on the sibling's page is never confirmed here."""
    _turn_on(w)
    sibling = _sibling(w)
    persons = sg.grant(w, agent=sibling, confirmed_at=None)  # the visitor's
    assert _confirm(_agent(w), persons).status_code == 404
    contacts = _waiting(w, agent=sibling, contact=w["contact"], scope="data:read")
    assert _confirm(_agent(w), contacts).status_code == 404
    for g in (persons, contacts):
        g.refresh_from_db()
        assert g.confirmed_at is None


def test_a_sibling_page_still_refuses_the_panel_token(w):
    _turn_on(w)
    grant = _waiting(w, agent=_sibling(w))
    raw, _ = DelegatedToken.issue(app=w["app"], user=w["login"], ttl_seconds=600, host_grant=grant)
    assert _confirm(bearer_client(raw), grant).status_code == 403
    grant.refresh_from_db()
    assert grant.confirmed_at is None


def test_a_confirmed_sibling_page_is_not_confirmed_again(w):
    _turn_on(w)
    grant = _waiting(w, agent=_sibling(w))
    assert _confirm(_agent(w), grant).status_code == 200
    assert _confirm(_agent(w), grant).status_code == 409


def test_a_page_for_any_agent_is_the_logins_own(w):
    _turn_on(w)
    grant = _waiting(w, agent=None)
    assert _confirm(_agent(w), grant).status_code == 200


def test_another_agents_login_with_its_test_key_off_is_refused(w):
    _turn_on(w)
    other_login = User.objects.create_user("hal-login", "hal@dimagi-ai.com", "pw")
    Agent.objects.create(slug="hal", name="Hal", workspace=w["ws"], owner=w["owner"], user=other_login)
    grant = sg.grant(w, user=other_login, confirmed_at=None)
    raw, _ = PersonalToken.create_for_user(user=other_login, label="t")
    r = _confirm(bearer_client(raw), grant)
    assert r.status_code == 403 and r.json()["error"] == "test_key_off"


def test_an_expired_or_stopped_grant_is_not_found(w):
    _turn_on(w)
    stopped = _waiting(w)
    host_grants.revoke(stopped)
    assert _confirm(_agent(w), stopped).status_code == 404
    expired = _waiting(w, scope="data:read", expires_at=timezone.now() - timedelta(seconds=1))
    assert _confirm(_agent(w), expired).status_code == 404

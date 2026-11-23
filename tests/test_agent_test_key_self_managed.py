"""An agent's own login manages its own test vault key (`PUT /api/vault/test-key`,
`apps/agents/credentials/login_test_key.py::set_own`) and makes a new key in place of the
old one (`PUT /api/vault/keyset` with `replace`), with no passkey.

The key only ever receives copies of conversations the agent is already in, and its own
connections are for logins its harness already holds, so a person's passkey guards
nothing here. What a new key or the switch going off does to what the old key held is the
same: every conversation copy, page permission and connection is dropped. A person, any
credential that is not the login's own, and an act-as token are refused; an admin's
switch on the agent page still works and still turns it off for good.
"""
from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.credentials import login_test_key
from apps.agents.models import Agent
from apps.canopy_sessions.models import Session, SessionKeyWrap
from apps.connections.models import Connection
from apps.events.models import SettingChange
from apps.harness import act_as
from apps.host_grants import host_grants
from apps.passkeys.models import UserVaultKeyset
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import DelegatedToken, PersonalToken
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures import site_gateway as sg
from tests.fixtures.agent_test_key import passphrase_keyset as _passphrase_keyset
from tests.fixtures.connections import sealed_to_owner, sf_app
from tests.fixtures.sensitive import SENSITIVE, world
from tests.token_helpers import bearer_client

pytestmark = pytest.mark.django_db

URL = "/api/vault/test-key"


@pytest.fixture()
def w():
    x = world()
    login = User.objects.create_user("labsdata-login", "labsdata@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(user=login, workspace=x["ws"], role=WorkspaceMembership.EDITOR)
    x["agent"].user = login
    x["agent"].save(update_fields=["user"])
    raw, _ = PersonalToken.create_for_user(user=login, label="harness")
    browser = Client()
    browser.force_login(login)  # no step-up: an agent's login has no passkey
    x.update(login=login, login_pat=raw, browser=browser)
    return x


def _set(client, enabled):
    return client.put(URL, {"enabled": enabled}, content_type="application/json")


def _make_key(client, login, *, replace=False):
    body, priv = _passphrase_keyset(login)
    return client.put("/api/vault/keyset", {**body, "replace": replace}, content_type="application/json"), priv


def test_the_login_turns_its_own_test_key_on_with_no_passkey_and_it_is_audited(w):
    assert w["browser"].get(URL).json() == {**w["browser"].get(URL).json(), "agent": "labsdata", "enabled": False}
    r = _set(w["browser"], True)
    assert r.status_code == 200, r.content
    assert r.json()["enabled"] is True and r.json()["has_key"] is False
    w["agent"].refresh_from_db()
    assert w["agent"].test_vault_key is True
    change = SettingChange.objects.filter(path=URL).latest("id")
    assert "test-vault-key" in str(change.changes)
    # Its own token works as well as its own browser session.
    assert _set(bearer_client(w["login_pat"]), False).json()["enabled"] is False


def test_turning_it_off_itself_deletes_the_key_and_every_copy(w):
    _set(w["browser"], True)
    assert _make_key(w["browser"], w["login"])[0].status_code == 200
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["login"], metadata=dict(SENSITIVE))
    assert SessionKeyWrap.objects.filter(session=s, user=w["login"]).exists()
    r = _set(w["browser"], False)
    assert r.status_code == 200
    assert r.json() == {**r.json(), "enabled": False, "has_key": False, "conversations": 0}
    assert not UserVaultKeyset.objects.filter(user=w["login"]).exists()
    assert not SessionKeyWrap.objects.filter(user=w["login"]).exists()


def test_a_person_cannot_use_it(w):
    r = _set(w["c"], True)
    assert r.status_code == 403 and r.json()["error"] == "a_person"
    assert w["c"].get(URL).status_code == 403


def test_it_only_ever_reaches_the_logins_own_agent(w):
    other_login = User.objects.create_user("hal-login", "hal@dimagi-ai.com", "pw")
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=w["ws"], owner=w["user"], user=other_login)
    assert _set(w["browser"], True).json()["agent"] == "labsdata"
    hal.refresh_from_db()
    assert hal.test_vault_key is False


def test_an_act_as_token_never_reaches_it():
    assert act_as.excluded(URL) is not None
    assert act_as.excluded("/api/vault/keyset") is not None


def test_a_panel_token_is_not_the_logins_own_credential():
    x = sg.world()
    login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    x["agent"].user = login
    x["agent"].save(update_fields=["user"])
    grant = sg.grant(x, user=login, agent=x["agent"], confirmed_at=None)
    raw, _ = DelegatedToken.issue(app=x["app"], user=login, ttl_seconds=600, host_grant=grant)
    r = _set(bearer_client(raw), True)
    assert r.status_code in (401, 403)
    x["agent"].refresh_from_db()
    assert x["agent"].test_vault_key is False


def test_a_new_key_drops_what_the_old_one_held(w):
    _set(w["browser"], True)
    _, old = _make_key(w["browser"], w["login"])
    s = Session.objects.create(workspace=w["ws"], agent=w["agent"], created_by=w["login"], metadata=dict(SENSITIVE))
    assert SessionKeyWrap.objects.filter(session=s, user=w["login"]).exists()
    conn = sealed_to_owner({"user": w["login"]}, w["login"], app=sf_app(w["ws"]))
    # A second key without `replace` is refused; with it, the new key starts with nothing.
    assert _make_key(w["browser"], w["login"])[0].json()["error"] == "test_key_exists"
    r, new = _make_key(w["browser"], w["login"], replace=True)
    assert r.status_code == 200, r.content
    assert not SessionKeyWrap.objects.filter(user=w["login"]).exists()
    left = Connection.objects.filter(pk=conn.pk).first()
    assert left is None or left.state == Connection.REVOKING
    w["agent"].refresh_from_db()
    assert w["agent"].test_vault_key is True  # the switch stays on: only the key changed


def test_a_new_key_stops_the_page_permissions_it_held():
    x = sg.world()
    login = User.objects.create_user("ace-login", "ace@dimagi-ai.com", "pw")
    x["agent"].user = login
    x["agent"].test_vault_key = True
    x["agent"].save(update_fields=["user", "test_vault_key"])
    browser = Client()
    browser.force_login(login)
    assert _make_key(browser, login)[0].status_code == 200
    grant = sg.grant(x, user=login, agent=x["agent"])
    assert host_grants.live_for(user=login) == [grant]
    assert _make_key(browser, login, replace=True)[0].status_code == 200
    grant.refresh_from_db()
    assert grant.revoked_at is not None


def test_a_new_key_is_still_passphrase_only(w):
    _set(w["browser"], True)
    _make_key(w["browser"], w["login"])
    body, _ = _passphrase_keyset(w["login"], prf=True)
    r = w["browser"].put("/api/vault/keyset", {**body, "replace": True}, content_type="application/json")
    assert r.status_code == 422 and r.json()["error"] == "test_key_passphrase_only"


def test_an_admin_still_turns_it_off_and_the_switch_on_the_agent_page_still_works(w):
    grant_step_up(w["c"])
    admin_url = f"/api/agents/{w['agent'].slug}/test-vault-key"
    assert w["c"].put(admin_url, {"enabled": True}, content_type="application/json").json()["enabled"] is True
    assert _make_key(w["browser"], w["login"])[0].status_code == 200
    assert w["c"].put(admin_url, {"enabled": False}, content_type="application/json").json()["has_key"] is False
    assert login_test_key.state(w["agent"])["enabled"] is False

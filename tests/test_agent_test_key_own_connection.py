"""An agent login's test key also holds the login's OWN connection, for its own screens
(`apps/agents/credentials/login_test_key.py::may_share_own_connection`).

While an admin has the agent test key on, the agent's own login connects a provider as
itself, sealed to its test key, and shares it with no passkey, lent to nobody: at most it
names canopy-web, which a screen's `as: viewer` call needs. So a headless test of a browser
extension signs in to Salesforce as the agent and uses the screens as a person does.
Anything broader, someone else's connection, another credential, or the switch off, is
refused. Turning the switch off disconnects every connection the login owns.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.contrib.auth.models import User
from django.test import Client

from apps.agents.credentials import login_test_key
from apps.connections import call_service, server_key, services
from apps.connections.models import Connection
from apps.passkeys.models import UserVaultKeyset
from apps.tokens.models import PersonalToken
from apps.workspaces.models import WorkspaceMembership
from tests.fixtures.connections import ShareWorld, browser_share, sealed_to_owner, vault_key
from tests.token_helpers import bearer_client

pytestmark = pytest.mark.django_db


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


@pytest.fixture
def w():
    world = ShareWorld()
    login = User.objects.create_user("eva-login", "eva@dimagi-ai.com", "pw")
    WorkspaceMembership.objects.create(workspace=world.ws, user=login, role=WorkspaceMembership.EDITOR)
    world.eva.user = login
    world.eva.test_vault_key = True
    world.eva.save(update_fields=["user", "test_vault_key"])
    world.login = login
    world.login_priv = vault_key(login)  # the test key the harness made, by the passphrase path
    return world


def _own(w) -> Connection:
    return sealed_to_owner({"user": w.login}, w.login, app=w.app)


def _browser(user) -> Client:
    c = Client()
    c.force_login(user)  # no step-up: an agent's login has no passkey
    return c


def _put(client, conn, body):
    return client.put(f"/api/connections/{conn.pk}/share", body, content_type="application/json")


def _body(w, conn, **kw):
    return browser_share(conn, w.login, w.login_priv, **kw)


def test_owner_keyset_is_the_test_key_while_it_is_on(w):
    assert services.owner_keyset("user", w.login, w.login).user_id == w.login.pk
    w.eva.test_vault_key = False
    w.eva.save(update_fields=["test_vault_key"])
    with pytest.raises(services.ConnectionError) as e:
        services.owner_keyset("user", w.login, w.login)
    assert e.value.code == "no_vault_key"


def test_the_login_shares_its_own_connection_with_canopy_web_and_no_passkey(w, key):
    conn = _own(w)
    r = _put(_browser(w.login), conn, _body(w, conn, canopy_web=server_key.thumbprint()))
    assert r.status_code == 200, r.json()
    conn.refresh_from_db()
    assert conn.state == Connection.ACTIVE and conn.server_use is True
    assert conn.statement["agents"] == [] and conn.statement["my_computers"] == []


def test_a_screen_then_calls_with_it_as_the_viewer(w, key):
    conn = _own(w)
    assert _put(_browser(w.login), conn, _body(w, conn, canopy_web=server_key.thumbprint())).status_code == 200
    surface = SimpleNamespace(workspace=w.ws.slug)
    assert call_service._viewer_connection(w.login, "salesforce", surface).pk == conn.pk


def test_with_its_own_token_too(w, key):
    conn = _own(w)
    raw, _ = PersonalToken.create_for_user(user=w.login, label="harness")
    r = _put(bearer_client(raw), conn, _body(w, conn, canopy_web=server_key.thumbprint()))
    assert r.status_code == 200, r.json()


@pytest.mark.parametrize("broader", ["agents", "my_computers", "endorsers"])
def test_lending_it_to_anyone_is_refused(w, key, broader):
    conn = _own(w)
    body = _body(w, conn, canopy_web=server_key.thumbprint())
    body["statement"][broader] = [str(w.eva.pk)] if broader == "agents" else ["A" * 43]
    r = _put(_browser(w.login), conn, body)
    assert r.status_code == 403 and r.json()["error"] == "test_key_share_too_broad"
    conn.refresh_from_db()
    assert conn.state == Connection.UNSHARED


def test_with_the_switch_off_it_is_refused(w, key):
    conn = _own(w)
    body = _body(w, conn, canopy_web=server_key.thumbprint())
    w.eva.test_vault_key = False
    w.eva.save(update_fields=["test_vault_key"])
    r = _put(_browser(w.login), conn, body)
    assert r.status_code == 403 and r.json()["error"] == "not_a_person"


def test_another_agent_login_cannot_share_it(w, key):
    conn = _own(w)
    other = User.objects.create_user("echo-login", "echo@dimagi-ai.com", "pw")
    w.echo.user, w.echo.test_vault_key = other, True
    w.echo.save(update_fields=["user", "test_vault_key"])
    r = _put(_browser(other), conn, _body(w, conn, canopy_web=server_key.thumbprint()))
    assert r.status_code == 404


def test_the_login_cannot_share_its_agents_connection(w, key):
    conn = sealed_to_owner({"agent": w.eva}, w.person, app=w.app)
    r = _put(_browser(w.login), conn, _body(w, conn, agents=[w.eva]))
    assert r.status_code == 404  # an agent's own connection is its admins' to see, and the login is none


def test_a_person_still_needs_a_passkey(w, key):
    conn = sealed_to_owner({"user": w.person}, w.person, app=w.app)
    body = browser_share(conn, w.person, w.person_priv, canopy_web=server_key.thumbprint())
    r = _put(_browser(w.person), conn, body)
    assert r.status_code == 403 and r.json()["error"] == "step_up_required"


def test_turning_the_switch_off_disconnects_its_connections(w, key):
    conn = _own(w)
    assert _put(_browser(w.login), conn, _body(w, conn, canopy_web=server_key.thumbprint())).status_code == 200
    login_test_key.disable(w.eva)
    assert not UserVaultKeyset.objects.filter(user=w.login).exists()
    left = Connection.objects.filter(pk=conn.pk).first()
    assert left is None or left.state == Connection.REVOKING
    with pytest.raises(services.NotConnected):
        services.connection_for_person(w.login, "salesforce", workspace=w.ws.slug)

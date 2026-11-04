"""Connecting: canopy-web finishes the sign-in and seals the token to its owner.

After the code exchange and every policy check, the refresh token is sealed
(`zks1`, name `refresh`, kv 0, bound to `connection:<uuid>`) to the owner's
member vault key ONLY — the person's, or for an agent the admin connecting it —
and nothing readable is kept: the access token is dropped, `access_sealed` stays
empty, and the row is `unshared` until the owner's browser shares it. The owner
opens it here with nothing but the zk-v1 REFERENCE (`docs/vault/zk-v1/`), so the
bytes are the contract's, not merely this server's own round trip.

A grant refused after the exchange is revoked at Salesforce but only dropped at
Google (revoking there ends every token of that account on that client).
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.forms.models import model_to_dict
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.agents.credentials import zk_seal
from apps.agents.models import Agent, AgentAdmin
from apps.connections import problems, services
from apps.connections.models import Connection, ConnectionApp, ConnectionGrant
from apps.connections.providers.salesforce import SalesforceProvider
from apps.events.models import Event
from apps.passkeys.models import UserVaultKeyset
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import (
    REDIRECT,
    REVOKE_URL,
    FakeGoogle,
    FakeSF,
    _resp,
    _token_body,
    sf_app,
    vault_key,
)
from tests.fixtures.vault_zk import ref as zk_ref

pytestmark = pytest.mark.django_db

FAKE_REFRESH, FAKE_ACCESS = "5Aep_refresh_1", "00D!access_1"
G = FakeGoogle.G
GOOGLE_SCOPES = [f"{G}gmail.readonly", f"{G}calendar.readonly"]


class World:
    def __init__(self):
        users = get_user_model().objects
        self.person = users.create_user(username="jj", email="jj@dimagi.com")
        self.person_priv = vault_key(self.person)
        self.ws = Workspace.objects.create(slug="w", display_name="W", created_by=self.person)
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.person, role=WorkspaceMembership.EDITOR)
        self.app = sf_app(self.ws)
        self.ada = users.create_user(username="ada", email="ada@dimagi.com")
        self.ada_priv = vault_key(self.ada)
        WorkspaceMembership.objects.create(workspace=self.ws, user=self.ada, role=WorkspaceMembership.EDITOR)
        self.eva = Agent.objects.create(slug="eva", name="Eva", workspace=self.ws, owner=self.person)
        AgentAdmin.objects.get_or_create(agent=self.eva, user=self.ada)

    def google(self, allowed: str = ""):
        services.configure_app(self.ws, "google", client_id="123.apps.googleusercontent.com",
                               client_secret="GOCSPX-s", scopes=GOOGLE_SCOPES,
                               options={"allowed_domains": allowed}, by=self.person)


@pytest.fixture
def world():
    return World()


@pytest.fixture
def fake_provider():
    with FakeSF() as fake:
        yield fake


@pytest.fixture
def fake_google():
    with FakeGoogle() as fake:
        yield fake


def connect(world, *, by=None, agent=None, provider="salesforce") -> Connection:
    """Begin and complete one flow through the service layer, as `by` (default
    the person), for `agent` when given."""
    by = by or world.person
    owner = ("agent", agent.pk) if agent else ("user", by.pk)
    session = {}
    app = ConnectionApp.objects.get(workspace=world.ws, provider=provider)
    url = services.begin(session, app=app, owner=owner, redirect_uri=REDIRECT)
    state = parse_qs(urlparse(url).query)["state"][0]
    return services.complete(session, provider=provider, workspace="w", code="c0de", state=state,
                             redirect_uri=REDIRECT, by=by)


def ref_open(sealed: str, binding: str, name: str, priv, user) -> str:
    """Open the owner's entry with the zk-v1 reference (what their browser does)."""
    return zk_ref.open_sealed_value(sealed, binding, name, "member", str(user.pk), priv)


def _plain(conn, priv, user) -> dict:
    return json.loads(ref_open(conn.sealed, conn.binding, "refresh", priv, user))


# ---- what is stored --------------------------------------------------------------

def test_connect_stores_only_ciphertext_sealed_to_the_owner(world, fake_provider):
    conn = connect(world)
    conn.refresh_from_db()
    assert conn.sealed.startswith("zks1:")
    assert zk_seal.sealed_targets(conn.sealed) == {("member", str(world.person.pk))}
    assert zk_seal.sealed_kv(conn.sealed) == 0
    row = json.dumps(model_to_dict(conn), default=str)
    assert FAKE_REFRESH not in row and FAKE_ACCESS not in row
    assert conn.state == Connection.UNSHARED and conn.access_sealed == "" and conn.access_expires_at is None
    assert conn.sealed_sig is None                                      # R4: canopy-web has no writer key
    assert conn.sealed_digest == hashlib.sha256(conn.sealed.encode()).hexdigest()
    assert (conn.user_id, conn.agent_id, conn.signer_id, conn.connected_by_id) == \
        (world.person.pk, None, world.person.pk, world.person.pk)
    assert (conn.account_id, conn.account_label) == ("00Dorg/005user", "jj@dimagi.com")
    assert conn.instance_url == "https://dimagi.my.salesforce.com" and conn.scopes == "api refresh_token"
    assert conn.statement is None and conn.statement_version == 0


def test_the_owner_opens_it_and_finds_the_spec_plaintext(world, fake_provider):
    conn = connect(world)
    plain = _plain(conn, world.person_priv, world.person)
    assert set(plain) == {"v", "provider", "refresh_token", "client_id", "login_host", "instance_url",
                          "account_id", "owner_key", "issued_at", "scopes", "statement_floor"}
    assert "client_secret" not in plain
    # Connecting comes before any statement; the owner's browser raises it at each share.
    assert plain["statement_floor"] == 0 and type(plain["statement_floor"]) is int
    assert plain["v"] == 1 and plain["provider"] == "salesforce"
    assert plain["refresh_token"] == FAKE_REFRESH and plain["client_id"] == "3MVG-client"
    assert plain["login_host"] == "login.salesforce.com"
    assert plain["instance_url"] == "https://dimagi.my.salesforce.com"
    assert plain["account_id"] == "00Dorg/005user" and plain["scopes"] == "api refresh_token"
    assert plain["owner_key"] == zk_ref.jkt(world.person_priv.public_key()) == conn.owner_key
    assert abs(plain["issued_at"] - time.time()) < 60 and isinstance(plain["issued_at"], int)
    # Compact JSON (contract §0.2): what the owner's browser and the CLI parse.
    raw = ref_open(conn.sealed, conn.binding, "refresh", world.person_priv, world.person)
    assert raw == json.dumps(plain, separators=(",", ":"))


def test_the_envelope_is_bound_to_this_connection(world, fake_provider):
    conn = connect(world)
    other = f"connection:{uuid.uuid4()}"
    with pytest.raises(Exception):  # noqa: B017 - AES-GCM refuses a different AAD
        ref_open(conn.sealed, other, "refresh", world.person_priv, world.person)
    with pytest.raises(Exception):  # noqa: B017
        ref_open(conn.sealed, conn.binding, "access", world.person_priv, world.person)


def test_an_agent_connection_is_sealed_to_the_connecting_admin(world, fake_provider):
    conn = connect(world, by=world.ada, agent=world.eva)
    assert (conn.agent_id, conn.user_id, conn.signer_id) == (world.eva.pk, None, world.ada.pk)
    assert zk_seal.sealed_targets(conn.sealed) == {("member", str(world.ada.pk))}
    assert _plain(conn, world.ada_priv, world.ada)["owner_key"] == zk_ref.jkt(world.ada_priv.public_key())
    # Not the agent's owner (who did not connect it), not anyone else.
    assert ("member", str(world.person.pk)) not in zk_seal.sealed_targets(conn.sealed)
    ev = Event.objects.get(kind="connections.connected")
    assert ev.workspace_id == "w" and ev.payload["owner"] == "agent:eva" and ev.payload["by"] == "ada@dimagi.com"
    assert FAKE_REFRESH not in json.dumps(ev.payload)


def test_scopes_are_what_was_granted_else_what_was_asked(world):
    with FakeSF(token=[_resp(body=_token_body(scope="refresh_token api web"))]):
        assert connect(world).scopes == "api refresh_token web"
    Connection.objects.all().delete()
    with FakeSF(token=[_resp(body=_token_body(scope=None))]):
        conn = connect(world)
    assert conn.scopes == "api refresh_token id"
    assert _plain(conn, world.person_priv, world.person)["scopes"] == "api refresh_token id"


def test_google_connects_with_no_hosts_in_the_plaintext(world, fake_google):
    world.google()
    conn = connect(world, provider="google")
    plain = _plain(conn, world.person_priv, world.person)
    assert (plain["provider"], plain["login_host"], plain["instance_url"]) == ("google", "", "")
    assert plain["refresh_token"] == "1//refresh" and plain["client_id"] == "123.apps.googleusercontent.com"
    assert conn.account_label == "someone@gmail.com" and conn.state == Connection.UNSHARED
    assert fake_google.revokes() == []


# ---- the vault key (R8) -------------------------------------------------------------

def test_owner_keyset_is_the_persons_or_the_connecting_admins(world):
    assert services.owner_keyset("user", world.person, world.person).user_id == world.person.pk
    assert services.owner_keyset("agent", world.eva, world.ada).user_id == world.ada.pk
    UserVaultKeyset.objects.filter(user=world.ada).delete()
    with pytest.raises(services.ConnectionError) as e:
        services.owner_keyset("agent", world.eva, world.ada)
    assert e.value.code == "no_vault_key" and "vault key" in str(e.value)
    p = problems.problem_for(e.value, title="Salesforce", owner="eva's")
    assert p.status_code == 409 and p.problem_members["error"] == "no_vault_key"


def test_start_without_a_vault_key_is_refused_and_says_where_to_set_one_up(world):
    UserVaultKeyset.objects.filter(user=world.person).delete()
    client = Client()
    client.force_login(world.person)
    resp = client.get(reverse("connections_start", args=["salesforce", "w"]), {"return_to": "/settings"})
    assert resp.status_code == 302
    landed = urlparse(resp["Location"])
    q = {k: v[0] for k, v in parse_qs(landed.query).items()}
    assert landed.path == "/settings"
    assert q == {"connection": "salesforce", "status": "error", "reason": "no_vault_key",
                 "setup": "/settings#vault-key"}
    assert not client.session.get(services.SESSION_KEY)               # nothing parked, nothing to finish


def test_an_admin_without_a_vault_key_cannot_start_an_agents_connect(world):
    UserVaultKeyset.objects.filter(user=world.ada).delete()
    client = Client()
    client.force_login(world.ada)
    resp = client.post(reverse("connections_start", args=["salesforce", "w"]), {"agent": "eva"})
    assert parse_qs(urlparse(resp["Location"]).query)["reason"] == ["no_vault_key"]


def test_a_vault_key_gone_by_the_callback_exchanges_nothing(world, fake_provider):
    session = {}
    url = services.begin(session, app=world.app, owner=("user", world.person.pk), redirect_uri=REDIRECT)
    state = parse_qs(urlparse(url).query)["state"][0]
    UserVaultKeyset.objects.filter(user=world.person).delete()
    with pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="salesforce", workspace="w", code="c", state=state,
                          redirect_uri=REDIRECT, by=world.person)
    assert e.value.code == "no_vault_key"
    assert fake_provider.calls == [] and not Connection.objects.exists()


# ---- refusals after the exchange ------------------------------------------------------

def test_google_wrong_domain_is_dropped_without_revoking(world, fake_google):
    world.google(allowed="dimagi.com")
    fake_google.user = {"sub": "9", "email": "x@gmail.com", "email_verified": True}
    with pytest.raises(services.ConnectionError) as e:
        connect(world, provider="google")
    assert e.value.code == "wrong_account"
    assert fake_google.revokes() == [] and Connection.objects.count() == 0


def test_google_agent_own_account_mismatch_is_dropped_without_revoking(world, fake_google):
    world.google()
    world.eva.google_account = "eva@dimagi-ai.com"
    world.eva.save()
    with pytest.raises(services.ConnectionError) as e:            # the admin's browser is someone@gmail.com
        connect(world, by=world.ada, agent=world.eva, provider="google")
    assert e.value.code == "wrong_account"
    assert fake_google.revokes() == [] and Connection.objects.count() == 0


def test_google_insufficient_scope_is_dropped_without_revoking(world, fake_google):
    world.google()
    fake_google.granted = f"openid {G}userinfo.email {G}gmail.readonly"      # Calendar unticked
    with pytest.raises(services.ConnectionError) as e:
        connect(world, provider="google")
    assert e.value.code == "insufficient_scope"
    assert fake_google.revokes() == [] and not Connection.objects.exists()


def test_salesforce_refusal_is_revoked(world):
    fake = FakeSF(identity=_resp(403, {"error": "nope"}))
    with fake, pytest.raises(services.ConnectionError) as e:
        connect(world)
    assert e.value.code == "provider_error"
    assert [(c[1], c[2]["token"]) for c in fake.calls if c[1] == REVOKE_URL] == [(REVOKE_URL, FAKE_REFRESH)]
    assert not Connection.objects.exists()


def test_insufficient_scope_is_refused(world):
    fake = FakeSF(token=[_resp(body=_token_body(scope="refresh_token"))])
    with fake, pytest.raises(services.ConnectionError) as e:
        connect(world)
    assert e.value.code == "insufficient_scope"
    assert [c[2]["token"] for c in fake.calls if c[1] == REVOKE_URL] == [FAKE_REFRESH]
    assert not Connection.objects.exists()


def test_an_app_changed_during_the_exchange_is_refused_and_revoked(world):
    session = {}
    url = services.begin(session, app=world.app, owner=("user", world.person.pk), redirect_uri=REDIRECT)
    state = parse_qs(urlparse(url).query)["state"][0]
    fake = FakeSF()
    real_post = fake.post

    def post(url, **kw):  # the admin removes the app while the code is exchanged
        if url.endswith("/token"):
            ConnectionApp.objects.filter(pk=world.app.pk).update(removed_at=timezone.now())
        return real_post(url, **kw)
    fake.post = post
    with fake, pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="salesforce", workspace="w", code="c", state=state,
                          redirect_uri=REDIRECT, by=world.person)
    assert e.value.code == "bad_state"
    assert [c[2]["token"] for c in fake.calls if c[1] == REVOKE_URL] == [FAKE_REFRESH]
    assert not Connection.objects.exists()


# ---- connecting the same account again (R6) -------------------------------------------

def test_connecting_the_same_account_again_replaces_the_envelope_and_unshares(world, fake_provider):
    first = connect(world, by=world.ada, agent=world.eva)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=world.ws, owner=world.ada)
    ConnectionGrant.objects.create(connection=first, agent=echo, granted_by=world.ada)
    Connection.objects.filter(pk=first.pk).update(
        state=Connection.ACTIVE, sealed_sig={"by": {}}, access_sealed="zks1:old-access",
        access_sig={"by": {}}, access_expires_at=timezone.now(), refresh_failed_at=timezone.now(),
        statement={"v": 1, "version": 3}, statement_sig="sig", statement_version=3)
    old_sealed = Connection.objects.get(pk=first.pk).sealed

    with FakeSF(token=[_resp(body=_token_body(access_token="00D!a2", refresh_token="5Aep_r2"))]):
        again = connect(world, by=world.ada, agent=world.eva)
    again.refresh_from_db()
    assert again.pk == first.pk and Connection.objects.count() == 1
    assert again.sealed != old_sealed and again.sealed_digest == hashlib.sha256(again.sealed.encode()).hexdigest()
    assert _plain(again, world.ada_priv, world.ada)["refresh_token"] == "5Aep_r2"
    assert again.state == Connection.UNSHARED and again.sealed_sig is None
    assert _plain(again, world.ada_priv, world.ada)["statement_floor"] == 0
    assert (again.access_sealed, again.access_sig, again.access_expires_at) == ("", None, None)
    assert again.refresh_failed_at is None
    # Its grants and statement stay: the share screen re-shares with version + 1.
    assert list(again.grants.values_list("agent__slug", flat=True)) == ["echo"]
    assert again.statement_version == 3


@pytest.mark.parametrize("winner_key_is_mine", [True, False])
def test_a_concurrent_first_connect_is_sealed_to_the_winners_row(world, fake_provider, winner_key_is_mine):
    """Another completion for the same account inserted the row between our lookup
    and our insert: we update the winner, and the envelope names ITS binding. A winner
    sealed to another key is not ours to replace, on this path either (R10)."""
    winner_key = zk_ref.jkt(world.person_priv.public_key()) if winner_key_is_mine else "someone-elses-key"
    winner = Connection.objects.create(app=world.app, provider="salesforce", user=world.person,
                                       account_id="00Dorg/005user", sealed="zks1:theirs", owner_key=winner_key)
    real = Connection.objects.select_for_update
    misses = []

    def select_for_update(*a, **kw):  # our lookup ran before the winner committed: it misses once
        if not misses:
            misses.append(1)
            return Connection.objects.none()
        return real(*a, **kw)
    if not winner_key_is_mine:
        with mock.patch.object(Connection.objects, "select_for_update", side_effect=select_for_update), \
                pytest.raises(services.ConnectionError) as e:
            connect(world)
        assert e.value.code == "owner_changed" and misses
        winner.refresh_from_db()
        assert (winner.sealed, winner.owner_key) == ("zks1:theirs", "someone-elses-key")
        assert [c[2]["token"] for c in fake_provider.calls if c[1] == REVOKE_URL] == [FAKE_REFRESH]
        return
    with mock.patch.object(Connection.objects, "select_for_update", side_effect=select_for_update):
        conn = connect(world)
    assert misses and conn.pk == winner.pk and Connection.objects.count() == 1
    conn.refresh_from_db()
    assert _plain(conn, world.person_priv, world.person)["refresh_token"] == FAKE_REFRESH


# ---- the browser lands on the connected screen ----------------------------------------------

def test_callback_lands_on_connected(world, fake_provider):
    client = Client()
    client.force_login(world.person)
    start = client.get(reverse("connections_start", args=["salesforce", "w"]), {"return_to": "/settings?tab=x"})
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    resp = client.get(reverse("connections_callback", args=["salesforce", "w"]), {"code": "c0de", "state": state})
    conn = Connection.objects.get()
    landed = urlparse(resp["Location"])
    assert resp.status_code == 302
    assert landed.path == f"/w/w/connections/{conn.pk}/connected"
    # `just_connected` is what lets the screen store an agent's connection on arrival, once.
    assert parse_qs(landed.query) == {"return_to": ["/settings?tab=x"], "just_connected": ["1"]}


def test_an_agents_callback_lands_on_its_connected_screen(world, fake_provider):
    client = Client()
    client.force_login(world.ada)
    start = client.post(reverse("connections_start", args=["salesforce", "w"]), {"agent": "eva"})
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    resp = client.get(reverse("connections_callback", args=["salesforce", "w"]), {"code": "c0de", "state": state})
    conn = Connection.objects.get(agent=world.eva)
    assert urlparse(resp["Location"]).path == f"/w/w/connections/{conn.pk}/connected"
    assert parse_qs(urlparse(resp["Location"]).query)["just_connected"] == ["1"]


def test_unshared_connection_is_listed_as_not_shared_yet(world, fake_provider):
    """Review Focus 5: the owner closed the tab before sharing."""
    connect(world)
    client = Client()
    client.force_login(world.person)
    (row,) = client.get("/api/connections/").json()["connections"]
    assert row["state"] == "unshared"
    assert row["statement"] == {"version": 0, "agents": [], "my_computers": [], "endorsers": []}
    assert row["holders"] == []
    assert row["provider_calls"] is False  # no key here: canopy-web can't be used, so the lists show no switch


# ---- a row's owner key never changes -------------------------------------------------

def _reconnect_refused(world, fake, **kw):
    with fake, pytest.raises(services.ConnectionError) as e:
        connect(world, **kw)
    assert e.value.code == "owner_changed"


def test_another_admin_reconnecting_an_agents_connection_is_refused(world, fake_provider):
    first = connect(world, by=world.ada, agent=world.eva)
    before = model_to_dict(Connection.objects.get(pk=first.pk))
    fake = FakeSF(token=[_resp(body=_token_body(refresh_token="5Aep_other"))])
    _reconnect_refused(world, fake, by=world.person, agent=world.eva)
    assert model_to_dict(Connection.objects.get(pk=first.pk)) == before
    assert Connection.objects.count() == 1
    # Salesforce: the new grant is revoked, the old one is left alone.
    assert [c[2]["token"] for c in fake.calls if c[1] == REVOKE_URL] == ["5Aep_other"]
    assert _plain(Connection.objects.get(), world.ada_priv, world.ada)["refresh_token"] == FAKE_REFRESH


def test_a_person_who_replaced_their_vault_key_is_refused(world, fake_provider):
    first = connect(world)
    before = model_to_dict(Connection.objects.get(pk=first.pk))
    vault_key(world.person)
    _reconnect_refused(world, FakeSF(token=[_resp(body=_token_body(refresh_token="5Aep_other"))]))
    assert model_to_dict(Connection.objects.get(pk=first.pk)) == before


def test_a_changed_owner_at_google_is_discarded_without_a_revoke(world, fake_google):
    world.google()
    first = connect(world, provider="google")
    before = model_to_dict(Connection.objects.get(pk=first.pk))
    vault_key(world.person)
    with pytest.raises(services.ConnectionError) as e:
        connect(world, provider="google")
    assert e.value.code == "owner_changed"
    assert fake_google.revokes() == [] and model_to_dict(Connection.objects.get(pk=first.pk)) == before


def test_disconnecting_first_lets_the_new_owner_connect_on_a_new_row(world, fake_provider):
    first = connect(world, by=world.ada, agent=world.eva)
    services.disconnect(first, by=world.ada)
    again = connect(world, by=world.person, agent=world.eva)
    assert again.pk != first.pk and again.owner_key == zk_ref.jkt(world.person_priv.public_key())


def test_owner_changed_is_a_conflict_and_the_callback_lands_with_the_reason(world, fake_provider):
    connect(world)
    vault_key(world.person)
    problem = problems.problem_for(services.ConnectionError("owner_changed", "disconnect first"),
                                   title="Salesforce", owner="Your")
    assert problem.status_code == 409 and problem.problem_members["error"] == "owner_changed"
    client = Client()
    client.force_login(world.person)
    start = client.get(reverse("connections_start", args=["salesforce", "w"]), {"return_to": "/settings"})
    state = parse_qs(urlparse(start["Location"]).query)["state"][0]
    resp = client.get(reverse("connections_callback", args=["salesforce", "w"]), {"code": "c0de", "state": state})
    landed = parse_qs(urlparse(resp["Location"]).query)
    assert landed["reason"] == ["owner_changed"] and landed["status"] == ["error"]


# ---- the host rule (contract §0.2): what every machine would refuse does not connect ----

@pytest.mark.parametrize("url", [
    "https://na1.salesforce.com",               # legacy instance: no My Domain
    "http://acme.my.salesforce.com",            # not https
    "https://.my.salesforce.com",               # no label
    "https://my.salesforce.com",
    "https://acme.my.salesforce.com.evil.example",
    "https://evil.example",
    "https://acme.my.salesforce.com:8443",      # nothing but the host
    "https://user@acme.my.salesforce.com",
    "https://acme.my.salesforce.com/services",
    "https://acme.my.salesforce.com?x=1",
    "https://[bad",
    "",
])
def test_an_instance_url_no_machine_would_accept_is_refused_and_revoked(world, url, caplog):
    fake = FakeSF(token=[_resp(body=_token_body(instance_url=url))])
    with caplog.at_level("WARNING", logger="apps.connections.services"), fake, \
            pytest.raises(services.ConnectionError) as e:
        connect(world)
    assert e.value.code == "provider_error"
    assert not Connection.objects.exists()
    assert [c[2]["token"] for c in fake.calls if c[1] == REVOKE_URL] == [FAKE_REFRESH]
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("connections.provider_error ")]
    assert any("during=connect" in line and "error=bad_instance_url" in line for line in lines)
    assert FAKE_REFRESH not in "".join(lines)


@pytest.mark.parametrize("url,want", [
    ("https://Acme.MY.Salesforce.com", "https://acme.my.salesforce.com"),
    ("HTTPS://acme.my.salesforce.com/", "https://acme.my.salesforce.com"),
])
def test_an_instance_url_is_sealed_and_stored_normalised(world, url, want):
    with FakeSF(token=[_resp(body=_token_body(instance_url=url))]):
        conn = connect(world)
    conn.refresh_from_db()
    assert conn.instance_url == want
    assert _plain(conn, world.person_priv, world.person)["instance_url"] == want


def test_a_missing_login_url_seals_the_default_login_host(world):
    ConnectionApp.objects.filter(pk=world.app.pk).update(options={"access_token_seconds": 1800})
    with FakeSF():
        conn = connect(world)
    assert _plain(conn, world.person_priv, world.person)["login_host"] == "login.salesforce.com"


def test_a_stored_login_url_that_bypassed_the_settings_check_is_refused(world):
    ConnectionApp.objects.filter(pk=world.app.pk).update(options={"login_url": "https://evil.example"})
    fake = FakeSF()
    fake.post = lambda url, data=None, headers=None, timeout=None, allow_redirects=True: (
        _resp(body=_token_body()) if url.endswith("/token") else _resp())
    with fake, pytest.raises(services.ConnectionError) as e:
        connect(world)
    assert e.value.code == "provider_error" and not Connection.objects.exists()


def test_a_my_domain_login_host_is_sealed_lower_case(world):
    app = SalesforceProvider(mock.Mock(options={"login_url": "https://Acme.MY.salesforce.com/"}))
    token = mock.Mock(instance_url="https://acme.my.salesforce.com")
    assert app.sealed_hosts(token) == ("acme.my.salesforce.com", "https://acme.my.salesforce.com")


@pytest.mark.parametrize("url", ["https://.my.salesforce.com", "https://a_b.my.salesforce.com",
                                 "https://login.salesforce.com.evil.example"])
def test_settings_refuse_a_login_url_no_machine_would_accept(url):
    with pytest.raises(ValueError, match="Login URL"):
        SalesforceProvider.clean_options({"login_url": url})


def test_a_google_connection_seals_no_hosts(world, fake_google):
    world.google()
    conn = connect(world, provider="google")
    plain = _plain(conn, world.person_priv, world.person)
    assert (plain["login_host"], plain["instance_url"]) == ("", "")

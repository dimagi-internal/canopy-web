"""The connections service layer: connect, refresh under lock, disconnect, grants.

Salesforce is faked at the `requests` boundary (as `tests/test_github_connect.py`),
so the provider's real HTTP handling runs under every test here. What is pinned:

- a rejected grant can arrive as **HTTP 200 with an `error` key**;
- the refresh token **rotates**, and failing to store the new one bricks the
  connection on second use;
- two refreshes racing must spend the refresh token once (re-check under lock);
- `invalid_grant` stamps `refresh_failed_at`, but a network blip must NOT;
- the identity URL comes from the token response, so the bearer token is only
  ever sent to a Salesforce host.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import IntegrityError
from django.utils import timezone

from apps.agents.models import Agent
from apps.common.encryption import decrypt_secret, encrypt_secret
from apps.connections import providers, services
from apps.connections.models import Connection, ConnectionGrant
from apps.connections.providers.base import TokenResponse
from apps.events.models import Event
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import (
    ID_URL,
    REDIRECT,
    REVOKE_URL,
    SF,
    FakeSF,
    _resp,
    _stored,
    _token_body,
)

pytestmark = pytest.mark.django_db



@pytest.fixture(autouse=True)
def configured(settings):
    for k, v in SF.items():
        setattr(settings, k, v)
    return settings


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def ws(user):
    w = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.EDITOR)
    return w


@pytest.fixture
def eva(ws, user):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user)


@pytest.fixture
def echo(ws, user):
    return Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)


def _begin(session, owner, **kw):
    return services.begin(session, provider="salesforce", owner=owner, redirect_uri=REDIRECT, **kw)


def _connect(session, owner, by, fake=None):
    url = _begin(session, owner)
    state = parse_qs(urlparse(url).query)["state"][0]
    with fake or FakeSF():
        return services.complete(session, provider="salesforce", code="c0de", state=state,
                                 redirect_uri=REDIRECT, by=by)


# ---- begin / complete --------------------------------------------------------

def test_begin_parks_state_and_pkce(user):
    session = {}
    url = _begin(session, ("user", user.pk), return_to="/settings")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://login.salesforce.com/services/oauth2/authorize?")
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["api refresh_token"]
    assert q["redirect_uri"] == [REDIRECT]
    state = q["state"][0]
    parked = session[services.SESSION_KEY][state]
    assert parked["provider"] == "salesforce"
    assert (parked["owner_kind"], parked["owner_id"]) == ("user", user.pk)
    assert parked["return_to"] == "/settings"
    # The challenge is the S256 of the parked verifier, and the verifier never leaves.
    import base64
    import hashlib
    expect = base64.urlsafe_b64encode(hashlib.sha256(parked["verifier"].encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == [expect.decode()]
    assert parked["verifier"] not in url


def test_begin_not_configured(settings, user):
    settings.SALESFORCE_CLIENT_SECRET = ""
    with pytest.raises(services.NotConfigured):
        _begin({}, ("user", user.pk))


def test_complete_rejects_unknown_or_reused_state(user):
    session = {}
    url = _begin(session, ("user", user.pk))
    state = parse_qs(urlparse(url).query)["state"][0]
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="salesforce", code="c", state="forged",
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state"
    assert fake.calls == []
    with FakeSF():
        services.complete(session, provider="salesforce", code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:  # replay
        services.complete(session, provider="salesforce", code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state"
    assert fake.calls == []


def test_complete_exchanges_with_verifier_and_stores_encrypted(user):
    session = {}
    url = _begin(session, ("user", user.pk))
    state = parse_qs(urlparse(url).query)["state"][0]
    verifier = session[services.SESSION_KEY][state]["verifier"]
    with FakeSF() as fake:
        conn = services.complete(session, provider="salesforce", code="c0de", state=state,
                                 redirect_uri=REDIRECT, by=user)
    _, _, data = fake.token_calls()[0]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "c0de" and data["code_verifier"] == verifier
    assert data["redirect_uri"] == REDIRECT
    # identity read with the new access token at the token response's `id` URL
    get = [c for c in fake.calls if c[0] == "GET"][0]
    assert get[1] == ID_URL and get[2]["Authorization"] == "Bearer 00D!access_1"
    conn.refresh_from_db()
    assert conn.user_id == user.pk and conn.agent_id is None
    assert conn.account_id == "00Dorg/005user" and conn.account_label == "jj@dimagi.com"
    assert conn.instance_url == "https://dimagi.my.salesforce.com"
    assert conn.connected_by_id == user.pk
    assert "5Aep_refresh_1" not in conn.refresh_token_enc
    assert decrypt_secret(conn.refresh_token_enc) == "5Aep_refresh_1"
    assert decrypt_secret(conn.access_token_enc) == "00D!access_1"
    assert conn.access_token_expires_at > timezone.now() + timedelta(minutes=29)


def test_token_error_with_200_is_failure(user):
    # An access token alongside the error must not make it a success.
    fake = FakeSF(token=[_resp(200, _token_body(error="invalid_grant", error_description="expired"))])
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, fake)
    assert e.value.code == "provider_error"
    assert not Connection.objects.exists()


@pytest.mark.parametrize("failure", [
    requests.Timeout("slow"), requests.ConnectionError("down"),
])
def test_complete_network_failure_is_provider_error(user, failure):
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, FakeSF(token=[failure]))
    assert e.value.code == "provider_error"
    assert not Connection.objects.exists()


def test_complete_non_json_body_is_provider_error(user):
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, FakeSF(token=[_resp(200, text="<html>")]))
    assert e.value.code == "provider_error"


def test_complete_identity_failure_stores_nothing(user):
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, FakeSF(identity=_resp(403, {"error": "nope"})))
    assert e.value.code == "provider_error"
    assert not Connection.objects.exists()


def test_complete_refuses_foreign_identity_url(user):
    fake = FakeSF(token=[_resp(body=_token_body(id="https://evil.example/id/00Dorg/005user"))])
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, fake)
    assert e.value.code == "provider_error"
    assert not Connection.objects.exists()
    assert [c for c in fake.calls if c[0] == "GET"] == []


def test_complete_agent_deleted_since_begin_is_bad_state(user, eva):
    session = {}
    url = _begin(session, ("agent", eva.pk))
    state = parse_qs(urlparse(url).query)["state"][0]
    eva.delete()
    with FakeSF(), pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="salesforce", code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state"
    assert not Connection.objects.exists()


def test_complete_concurrent_insert_updates_the_winner(user):
    """Another completion for the same account inserted between lookup and insert."""
    # The winner's row is committed; our update_or_create saw no row and its insert collides.
    _stored({"user": user}, user, refresh="5Aep_theirs")
    with mock.patch.object(Connection.objects, "update_or_create",
                           side_effect=IntegrityError("duplicate key")):
        conn = _connect({}, ("user", user.pk), user)
    assert Connection.objects.count() == 1
    conn.refresh_from_db()
    assert decrypt_secret(conn.refresh_token_enc) == "5Aep_refresh_1"
    assert decrypt_secret(conn.access_token_enc) == "00D!access_1"


def test_complete_upserts_same_account(user):
    first = _connect({}, ("user", user.pk), user)
    Connection.objects.filter(pk=first.pk).update(refresh_failed_at=timezone.now())
    fake = FakeSF(token=[_resp(body=_token_body(access_token="00D!a2", refresh_token="5Aep_r2"))])
    again = _connect({}, ("user", user.pk), user, fake)
    assert again.pk == first.pk
    assert Connection.objects.count() == 1
    again.refresh_from_db()
    assert again.refresh_failed_at is None
    assert decrypt_secret(again.refresh_token_enc) == "5Aep_r2"


def test_complete_for_agent_owner(user, eva):
    conn = _connect({}, ("agent", eva.pk), user)
    assert conn.agent_id == eva.pk and conn.user_id is None
    assert services.owner_of(conn) == ("agent", eva.pk)
    assert list(services.connections_for(("agent", eva.pk))) == [conn]
    assert list(services.connections_for(("user", user.pk))) == []


# ---- identity host check (the bearer token goes only to Salesforce) ----------

@pytest.mark.parametrize("id_url", [
    "http://login.salesforce.com/id/00D/005",           # not https
    "https://evil.example/id/00D/005",                   # foreign host
    "https://salesforce.com.evil.example/id/00D/005",   # suffix trick
    "https://evilsalesforce.com/id/00D/005",             # no dot boundary
    "",
])
def test_identity_refuses_non_salesforce_host(id_url):
    token = TokenResponse(access_token="tok", refresh_token="", expires_at=None,
                          instance_url="", raw={"id": id_url})
    with FakeSF() as fake, pytest.raises(providers.ProviderError):
        providers.get("salesforce").identity(token)
    assert fake.calls == []


@pytest.mark.parametrize("id_url", [
    "https://login.salesforce.com/id/00D/005",
    "https://test.salesforce.com/id/00D/005",
    "https://dimagi.my.salesforce.com/id/00D/005",
    "https://dimagi--sb.sandbox.my.force.com/id/00D/005",
])
def test_identity_allows_salesforce_hosts(id_url):
    token = TokenResponse(access_token="tok", refresh_token="", expires_at=None,
                          instance_url="", raw={"id": id_url})
    with FakeSF():
        ident = providers.get("salesforce").identity(token)
    assert ident.account_id == "00Dorg/005user"


def test_identity_allows_configured_login_host(settings):
    settings.SALESFORCE_LOGIN_URL = "https://sso.dimagi.example"
    token = TokenResponse(access_token="tok", refresh_token="", expires_at=None,
                          instance_url="", raw={"id": "https://sso.dimagi.example/id/00D/005"})
    with FakeSF():
        assert providers.get("salesforce").identity(token).label == "jj@dimagi.com"


# ---- access_token_for --------------------------------------------------------

def test_cached_token_reused_without_http(user):
    conn = _stored({"user": user}, user)
    with FakeSF() as fake:
        tok = services.access_token_for(conn)
    assert fake.calls == []
    assert tok.token == "00D!cached"
    assert tok.instance_url == "https://dimagi.my.salesforce.com"
    assert tok.account_label == "jj@dimagi.com"
    conn.refresh_from_db()
    assert conn.last_used_at is not None


def test_token_inside_margin_is_refreshed(user):
    conn = _stored({"user": user}, user, expires_in=timedelta(minutes=4))
    with FakeSF() as fake:
        tok = services.access_token_for(conn)
    assert len(fake.token_calls()) == 1
    assert tok.token == "00D!access_1"


def test_min_life_decides_whether_the_cache_is_enough(user):
    """A token with 10 minutes left is enough by default (5 min margin) but not
    for a caller that needs 15 (the runner's re-stage interval)."""
    conn = _stored({"user": user}, user, expires_in=timedelta(minutes=10))
    with FakeSF() as fake:
        assert services.access_token_for(conn).token == "00D!cached"
        assert fake.token_calls() == []
        tok = services.access_token_for(conn, min_life=timedelta(seconds=900))
    assert len(fake.token_calls()) == 1 and tok.token == "00D!access_1"


def test_refresh_true_skips_a_fresh_cache(user):
    conn = _stored({"user": user}, user, expires_in=timedelta(hours=1), refresh="5Aep_old")
    fake = FakeSF(token=[_resp(body=_token_body(access_token="00D!forced", refresh_token="5Aep_new"))])
    with fake:
        tok = services.access_token_for(conn, refresh=True)
    assert len(fake.token_calls()) == 1 and tok.token == "00D!forced"
    conn.refresh_from_db()
    assert decrypt_secret(conn.refresh_token_enc) == "5Aep_new"


def test_access_token_lifetime_is_the_setting(user, settings):
    settings.SALESFORCE_ACCESS_TOKEN_SECONDS = 600
    conn = _stored({"user": user}, user, access="")
    with FakeSF():
        tok = services.access_token_for(conn)
    assert timezone.now() + timedelta(minutes=9) < tok.expires_at <= timezone.now() + timedelta(minutes=10)


def test_refresh_stores_rotated_refresh_token(user):
    conn = _stored({"user": user}, user, access="", refresh="5Aep_old")
    fake = FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token="5Aep_new"))])
    with fake:
        tok = services.access_token_for(conn)
    _, _, data = fake.token_calls()[0]
    assert data["grant_type"] == "refresh_token" and data["refresh_token"] == "5Aep_old"
    assert tok.token == "00D!new"
    # SALESFORCE_ACCESS_TOKEN_SECONDS (default 1800): Salesforce sends no expires_in.
    assert timezone.now() + timedelta(minutes=29) < tok.expires_at <= timezone.now() + timedelta(minutes=30)
    conn.refresh_from_db()
    assert decrypt_secret(conn.refresh_token_enc) == "5Aep_new"
    assert decrypt_secret(conn.access_token_enc) == "00D!new"
    assert conn.refresh_failed_at is None and conn.last_used_at is not None


def test_refresh_without_rotation_keeps_refresh_token(user):
    conn = _stored({"user": user}, user, access="", refresh="5Aep_keep")
    fake = FakeSF(token=[_resp(body=_token_body(access_token="00D!new", refresh_token=None,
                                                instance_url="https://moved.my.salesforce.com"))])
    with fake:
        services.access_token_for(conn)
    conn.refresh_from_db()
    assert decrypt_secret(conn.refresh_token_enc) == "5Aep_keep"
    assert conn.instance_url == "https://moved.my.salesforce.com"


def test_refresh_inside_lock_rechecks_cache(user):
    """Review Focus 2: another worker refreshed while this one waited for the lock."""
    conn = _stored({"user": user}, user, access="", refresh="5Aep_old")
    real = services._cached_token
    seen = {"n": 0}

    def racing(c, min_life):
        seen["n"] += 1
        if seen["n"] == 1:
            # The pre-lock check finds nothing; meanwhile the other worker lands.
            Connection.objects.filter(pk=c.pk).update(
                access_token_enc=encrypt_secret("00D!theirs"),
                access_token_expires_at=timezone.now() + timedelta(hours=2),
                refresh_token_enc=encrypt_secret("5Aep_rotated"),
            )
            return ""
        return real(c, min_life)

    with mock.patch.object(services, "_cached_token", side_effect=racing), FakeSF() as fake:
        tok = services.access_token_for(conn)
    assert fake.token_calls() == []
    assert tok.token == "00D!theirs"
    conn.refresh_from_db()
    assert decrypt_secret(conn.refresh_token_enc) == "5Aep_rotated"


def test_invalid_grant_stamps_reconnect(user):
    """Review Focus 1."""
    conn = _stored({"user": user}, user, access="")
    fake = FakeSF(token=[_resp(400, {"error": "invalid_grant", "error_description": "expired access/refresh token"})])
    with fake, pytest.raises(services.ReconnectRequired):
        services.access_token_for(conn)
    conn.refresh_from_db()
    assert conn.refresh_failed_at is not None
    assert conn.needs_reconnect
    # and the next call refuses without spending anything
    with FakeSF() as fake2, pytest.raises(services.ReconnectRequired):
        services.access_token_for(conn)
    assert fake2.calls == []


def test_invalid_grant_as_http_200_stamps_reconnect(user):
    conn = _stored({"user": user}, user, access="")
    with FakeSF(token=[_resp(200, _token_body(error="invalid_grant"))]), pytest.raises(services.ReconnectRequired):
        services.access_token_for(conn)
    conn.refresh_from_db()
    assert conn.refresh_failed_at is not None


@pytest.mark.parametrize("error", ["invalid_client", "unsupported_grant_type"])
def test_non_grant_rejection_is_provider_error_without_stamp(user, error):
    conn = _stored({"user": user}, user, access="")
    with FakeSF(token=[_resp(400, {"error": error})]), pytest.raises(services.ConnectionError) as e:
        services.access_token_for(conn)
    assert not isinstance(e.value, services.ReconnectRequired)
    assert e.value.code == "provider_error"
    conn.refresh_from_db()
    assert conn.refresh_failed_at is None


@pytest.mark.parametrize("failure", [
    requests.Timeout("slow"), requests.ConnectionError("down"),
    _resp(503, text="<html>maintenance"), _resp(200, text="<html>"),
])
def test_transient_refresh_failure_does_not_stamp(user, failure):
    conn = _stored({"user": user}, user, access="")
    with FakeSF(token=[failure]), pytest.raises(services.ConnectionError) as e:
        services.access_token_for(conn)
    assert not isinstance(e.value, services.ReconnectRequired)
    assert e.value.code == "provider_unavailable"
    conn.refresh_from_db()
    assert conn.refresh_failed_at is None
    assert decrypt_secret(conn.refresh_token_enc) == "5Aep_r"


# ---- disconnect --------------------------------------------------------------

def test_disconnect_revokes_and_deletes(user):
    conn = _stored({"user": user}, user, refresh="5Aep_bye")
    with FakeSF() as fake:
        services.disconnect(conn, by=user)
    assert [(c[0], c[1], c[2].get("token")) for c in fake.calls] == [("POST", REVOKE_URL, "5Aep_bye")]
    assert not Connection.objects.exists()


@pytest.mark.parametrize("failure", [_resp(400, {"error": "unsupported_token_type"}),
                                     requests.ConnectionError("down")])
def test_disconnect_revokes_and_deletes_even_if_revoke_fails(user, failure):
    conn = _stored({"user": user}, user)
    with FakeSF(revoke=failure):
        services.disconnect(conn, by=user)
    assert not Connection.objects.exists()


# ---- events ------------------------------------------------------------------

def test_no_token_in_event_payloads(user, eva, echo):
    conn = _connect({}, ("agent", eva.pk), user)
    Connection.objects.filter(pk=conn.pk).update(access_token_enc="", access_token_expires_at=None)
    conn.refresh_from_db()
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!second", refresh_token="5Aep_second"))]):
        services.access_token_for(conn)
    g = services.grant(conn, echo, by=user)
    services.revoke_grant(g, by=user)
    with FakeSF():
        services.disconnect(conn, by=user)

    kinds = list(Event.objects.order_by("id").values_list("kind", flat=True))
    assert kinds == ["connections.connected", "connections.granted",
                     "connections.grant_revoked", "connections.disconnected"]
    for ev in Event.objects.all():
        assert ev.workspace_id == eva.workspace_id
        assert ev.payload["provider"] == "salesforce"
        assert ev.payload["account_label"] == "jj@dimagi.com"
        assert ev.payload["owner"] == "agent:eva"
        blob = json.dumps(ev.payload) + ev.summary
        for secret in ("00D!access_1", "5Aep_refresh_1", "00D!second", "5Aep_second", "enc"):
            assert secret not in blob


def test_person_connection_is_logged_not_evented(user, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    conn = _connect({}, ("user", user.pk), user)
    services.issue_token(conn, to="cli", by=user)  # cached
    Connection.objects.filter(pk=conn.pk).update(access_token_enc="", access_token_expires_at=None)
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!second", refresh_token="5Aep_second"))]):
        services.issue_token(conn, to="cli", by=user)  # refreshed
    with FakeSF():
        services.disconnect(conn, by=user)

    assert not Event.objects.exists()
    lines = [r.getMessage() for r in caplog.records if r.name == "apps.connections.services"]
    kinds = [m.split(" ", 1)[0] for m in lines]
    assert kinds == ["connections.connected", "connections.token_issued", "connections.token_issued",
                     "connections.disconnected"]
    for m in lines:
        assert "provider=salesforce" in m and f"owner=user:{user.pk}" in m
        assert "account_label=jj@dimagi.com" in m and "by=jj@dimagi.com" in m
    assert "refreshed=false" in lines[1] and "refreshed=true" in lines[2]
    blob = caplog.text
    for secret in ("00D!access_1", "5Aep_refresh_1", "00D!second", "5Aep_second"):
        assert secret not in blob


# ---- which connection an agent uses; grants ----------------------------------

def test_connection_for_agent_own_then_granted(user, eva, echo):
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(echo, "salesforce")
    mine = _stored({"agent": eva}, user)
    assert services.connection_for_agent(eva, "salesforce") == mine
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(echo, "salesforce")
    services.grant(mine, echo, by=user)
    assert services.connection_for_agent(echo, "salesforce") == mine


def test_connection_for_agent_never_a_persons(user, eva):
    _stored({"user": user}, user)
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(eva, "salesforce")


def test_connection_for_agent_ambiguous(user, eva, echo, ws):
    other = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=user)
    a = _stored({"agent": eva}, user)
    b = _stored({"agent": other}, user)
    b.account_id = "00Dorg/005other"
    b.save()
    services.grant(a, echo, by=user)
    services.grant(b, echo, by=user)
    with pytest.raises(services.ConnectionError) as e:
        services.connection_for_agent(echo, "salesforce")
    assert e.value.code == "ambiguous"


def test_grant_refuses_own_agent_and_person_connections(user, eva):
    own = _stored({"agent": eva}, user)
    with pytest.raises(services.ConnectionError) as e:
        services.grant(own, eva, by=user)
    assert e.value.code == "own_connection"
    persons = _stored({"user": user}, user)
    with pytest.raises(services.ConnectionError) as e:
        services.grant(persons, eva, by=user)
    assert e.value.code == "not_grantable"
    assert not ConnectionGrant.objects.exists()


def test_grant_needs_admin_of_both_agents(user, eva, ws):
    stranger = get_user_model().objects.create_user(username="s", email="s@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=stranger, role=WorkspaceMembership.EDITOR)
    theirs = Agent.objects.create(slug="theirs", name="Theirs", workspace=ws, owner=stranger)
    conn = _stored({"agent": eva}, user)
    with pytest.raises(PermissionDenied):
        services.grant(conn, theirs, by=user)  # admin of eva, not of theirs
    with pytest.raises(PermissionDenied):
        services.grant(conn, theirs, by=stranger)  # admin of theirs, not of eva
    assert not ConnectionGrant.objects.exists()
    g = services.grant(conn, theirs, by=WorkspaceOwner.of(ws))
    # Revoking needs admin of EITHER agent: someone who administers neither is refused...
    nobody = get_user_model().objects.create_user(username="n", email="n@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=nobody, role=WorkspaceMembership.EDITOR)
    with pytest.raises(PermissionDenied):
        services.revoke_grant(g, by=nobody)
    assert ConnectionGrant.objects.filter(pk=g.pk).exists()
    # ...and the grantee's admin alone may cut it off.
    services.revoke_grant(g, by=stranger)
    assert not ConnectionGrant.objects.filter(pk=g.pk).exists()


def test_revoke_grant_by_owner_agent_admin_only(user, eva, ws):
    stranger = get_user_model().objects.create_user(username="s", email="s@dimagi.com")
    WorkspaceMembership.objects.create(workspace=ws, user=stranger, role=WorkspaceMembership.EDITOR)
    theirs = Agent.objects.create(slug="theirs", name="Theirs", workspace=ws, owner=stranger)
    conn = _stored({"agent": eva}, user)
    g = services.grant(conn, theirs, by=WorkspaceOwner.of(ws))
    services.revoke_grant(g, by=user)  # admin of eva only
    assert not ConnectionGrant.objects.exists()


class WorkspaceOwner:
    @staticmethod
    def of(ws):
        boss = get_user_model().objects.create_user(username="boss", email="boss@dimagi.com")
        WorkspaceMembership.objects.create(workspace=ws, user=boss, role=WorkspaceMembership.OWNER)
        return boss

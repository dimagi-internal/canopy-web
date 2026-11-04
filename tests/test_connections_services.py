"""The connections service layer: connect, disconnect, grants.

Salesforce is faked at the `requests` boundary (as `tests/test_github_connect.py`),
so the provider's real HTTP handling runs under every test here. What is pinned:

- a rejected grant can arrive as **HTTP 200 with an `error` key**;
- the identity URL comes from the token response, so the bearer token is only
  ever sent to a Salesforce host;
- canopy-web never refreshes or revokes a stored token: it holds none.
"""
from __future__ import annotations

import json
import logging
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.connections import providers, services
from apps.connections.models import Connection, ConnectionApp, ConnectionGrant
from apps.connections.providers.base import TokenResponse
from apps.events.models import Event
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import (
    ID_URL,
    REDIRECT,
    REVOKE_URL,
    FakeSF,
    _resp,
    _stored,
    _token_body,
    home,
    in_force,
    sf_app,
    vault_key,
)

pytestmark = pytest.mark.django_db



@pytest.fixture
def user():
    u = get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")
    vault_key(u)  # a connection is sealed to it
    return u


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


def _app_of(owner):
    """The app an owner connects with: an agent's workspace's, a person's home workspace's."""
    kind, pk = owner
    if kind == "agent":
        return sf_app(Agent.objects.get(pk=pk).workspace)
    return sf_app(home(get_user_model().objects.get(pk=pk)))


def _begin(session, owner, **kw):
    return services.begin(session, app=_app_of(owner), owner=owner, redirect_uri=REDIRECT, **kw)


def _complete(session, state, by, code="c0de", **kw):
    """`services.complete` for the workspace the flow was begun in (a forged state: any)."""
    parked = services.pending_for(session, state) or {}
    kw.setdefault("workspace", parked.get("workspace", "w"))
    kw.setdefault("provider", "salesforce")
    return services.complete(session, code=code, state=state, redirect_uri=REDIRECT, by=by, **kw)


def _connect(session, owner, by, fake=None):
    url = _begin(session, owner)
    state = parse_qs(urlparse(url).query)["state"][0]
    with fake or FakeSF():
        return _complete(session, state, by)


def _set_options(conn_or_app, **options):
    app = conn_or_app.app if isinstance(conn_or_app, Connection) else conn_or_app
    ConnectionApp.objects.filter(pk=app.pk).update(options={**app.options, **options})


# ---- begin / complete --------------------------------------------------------

def test_begin_parks_state_and_pkce(user):
    session = {}
    url = _begin(session, ("user", user.pk), return_to="/settings")
    q = parse_qs(urlparse(url).query)
    assert url.startswith("https://login.salesforce.com/services/oauth2/authorize?")
    assert q["code_challenge_method"] == ["S256"]
    assert q["scope"] == ["api refresh_token id"]
    assert q["redirect_uri"] == [REDIRECT]
    state = q["state"][0]
    parked = session[services.SESSION_KEY][state]
    assert parked["provider"] == "salesforce"
    app = sf_app(home(user))
    assert (parked["app"], parked["workspace"]) == (app.pk, app.workspace_id)
    assert q["client_id"] == ["3MVG-client"]
    assert (parked["owner_kind"], parked["owner_id"]) == ("user", user.pk)
    assert parked["return_to"] == "/settings"
    # The challenge is the S256 of the parked verifier, and the verifier never leaves.
    import base64
    import hashlib
    expect = base64.urlsafe_b64encode(hashlib.sha256(parked["verifier"].encode()).digest()).rstrip(b"=")
    assert q["code_challenge"] == [expect.decode()]
    assert parked["verifier"] not in url


def test_begin_not_configured(user):
    app = sf_app(home(user))
    app.client_secret = ""
    app.save()
    with pytest.raises(services.NotConfigured):
        _begin({}, ("user", user.pk))


def test_complete_rejects_unknown_or_reused_state(user):
    session = {}
    url = _begin(session, ("user", user.pk))
    state = parse_qs(urlparse(url).query)["state"][0]
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        _complete(session, "forged", user, code="c")
    assert e.value.code == "bad_state"
    assert fake.calls == []
    with FakeSF():
        _complete(session, state, user, code="c")
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:  # replay
        _complete(session, state, user, code="c")
    assert e.value.code == "bad_state"
    assert fake.calls == []


def test_complete_exchanges_with_the_verifier_and_reads_the_identity(user):
    session = {}
    url = _begin(session, ("user", user.pk))
    state = parse_qs(urlparse(url).query)["state"][0]
    verifier = session[services.SESSION_KEY][state]["verifier"]
    with FakeSF() as fake:
        conn = _complete(session, state, user, code="c0de")
    _, _, data = fake.token_calls()[0]
    assert data["grant_type"] == "authorization_code"
    assert data["code"] == "c0de" and data["code_verifier"] == verifier
    assert data["redirect_uri"] == REDIRECT
    # identity read with the new access token at the token response's `id` URL
    get = [c for c in fake.calls if c[0] == "GET"][0]
    assert get[1] == ID_URL and get[2]["Authorization"] == "Bearer 00D!access_1"
    assert [c for c in fake.calls if c[1] == REVOKE_URL] == []          # a kept grant is not revoked
    assert conn.user_id == user.pk and conn.connected_by_id == user.pk
    assert list(services.connections_for(("user", user.pk))) == [conn]





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
    fake = FakeSF(identity=_resp(403, {"error": "nope"}))
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, fake)
    assert e.value.code == "provider_error"
    assert not Connection.objects.exists()
    # Issued, then refused: the grant is revoked at the provider.
    assert fake.calls[-1][1:] == (REVOKE_URL, {"token": "5Aep_refresh_1", "token_type_hint": "refresh_token",
                                               "client_id": "3MVG-client", "client_secret": "shh"})


def test_complete_without_a_refresh_token_revokes_the_access_token(user):
    fake = FakeSF(token=[_resp(body=_token_body(refresh_token=None))])
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, fake)
    assert e.value.code == "provider_error"
    assert fake.calls[-1][1] == REVOKE_URL
    assert (fake.calls[-1][2]["token"], fake.calls[-1][2]["token_type_hint"]) == ("00D!access_1", "access_token")


@pytest.mark.parametrize("revoke", [_resp(400, {"error": "invalid_token"}), requests.Timeout("slow")])
def test_a_failed_revoke_of_a_refused_grant_is_logged_not_raised(user, revoke, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    fake = FakeSF(token=[_resp(body=_token_body(scope="refresh_token"))], revoke=revoke)   # no `api`
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, fake)
    assert e.value.code == "insufficient_scope"                 # the refusal, not the revoke failure
    kinds = [services.parse_log_line(r.getMessage())[0] for r in caplog.records]
    assert kinds[-2:] == ["provider_error", "revoke_failed"]
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
        _complete(session, state, user, code="c")
    assert e.value.code == "bad_state"
    assert not Connection.objects.exists()











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
        providers.get_class("salesforce")().identity(token)
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
        ident = providers.get_class("salesforce")().identity(token)
    assert ident.account_id == "00Dorg/005user"


def test_identity_refuses_a_non_salesforce_login_host_even_if_an_app_holds_one():
    # Saved options only allow Salesforce hosts; a row that bypassed that gets no exception.
    app = ConnectionApp(provider="salesforce", options={"login_url": "https://sso.dimagi.example"})
    token = TokenResponse(access_token="tok", refresh_token="", expires_at=None,
                          instance_url="", raw={"id": "https://sso.dimagi.example/id/00D/005"})
    with FakeSF() as fake, pytest.raises(providers.ProviderError):
        providers.for_app(app).identity(token)
    assert fake.calls == []


# ---- disconnect --------------------------------------------------------------

def test_disconnect_deletes_and_calls_no_provider(user):
    conn = _stored({"user": user}, user)
    with FakeSF() as fake:
        services.disconnect(conn, by=user)
    assert fake.calls == []                                    # canopy-web holds no token to revoke
    assert not Connection.objects.exists()


# ---- events ------------------------------------------------------------------

def test_an_agents_connection_is_evented(user, eva, echo):
    conn = _stored({"agent": eva}, user)
    cid = str(conn.pk)
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=user)
    with FakeSF():
        services.disconnect(conn, by=user)

    kinds = list(Event.objects.order_by("id").values_list("kind", flat=True))
    # Never shared, so no machine could revoke it: deleted at once (R7).
    assert kinds == ["connections.disconnected", "connections.grant_revoked", "connections.revoke_unconfirmed"]
    for ev in Event.objects.all():
        assert ev.workspace_id == eva.workspace_id
        assert ev.payload["connection"] == cid
        assert ev.payload["provider"] == "salesforce"
        assert ev.payload["account_label"] == "jj@dimagi.com"
        assert ev.payload["owner"] == "agent:eva"
        assert "zks1:" not in json.dumps(ev.payload) + ev.summary


def test_person_connection_is_logged_not_evented(user, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    conn = _stored({"user": user}, user)
    with FakeSF():
        services.disconnect(conn, by=user)

    assert not Event.objects.exists()
    lines = [r.getMessage() for r in caplog.records if r.name == "apps.connections.services"]
    assert [m.split(" ", 1)[0] for m in lines] == ["connections.disconnected", "connections.revoke_unconfirmed"]
    assert "provider=salesforce" in lines[0] and f"owner=user:{user.pk}" in lines[0]
    assert "account_label=jj@dimagi.com" in lines[0] and "by=jj@dimagi.com" in lines[0]


# ---- which connection an agent uses; grants ----------------------------------

def test_connection_for_agent_own_then_granted(user, eva, echo):
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(echo, "salesforce")
    mine = in_force(_stored({"agent": eva}, user))
    assert services.connection_for_agent(eva, "salesforce") == mine
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(echo, "salesforce")
    in_force(mine, echo, by=user)
    assert services.connection_for_agent(echo, "salesforce") == mine


def test_connection_for_agent_never_a_persons(user, eva):
    _stored({"user": user}, user)
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(eva, "salesforce")


def test_connection_for_agent_ambiguous(user, eva, echo, ws):
    other = Agent.objects.create(slug="hal", name="Hal", workspace=ws, owner=user)
    a = in_force(_stored({"agent": eva}, user), echo, by=user)
    b = _stored({"agent": other}, user)
    b.account_id = "00Dorg/005other"
    b.save()
    in_force(b, echo, by=user)
    with pytest.raises(services.ConnectionError) as e:
        services.connection_for_agent(echo, "salesforce")
    assert e.value.code == "ambiguous"
    assert a.pk != b.pk


def test_a_stale_grant_neither_answers_nor_makes_it_ambiguous(user, ws, echo):
    """Review Focus 3, at lookup: lender A stopped administering echo; lender B's
    grant still works, so echo uses B's — never `ambiguous` forever."""
    from apps.agents.models import AgentAdmin

    users = get_user_model().objects
    lender_a = users.create_user(username="a", email="a@dimagi.com")
    lender_b = users.create_user(username="b", email="b@dimagi.com")
    for u in (lender_a, lender_b):
        WorkspaceMembership.objects.create(workspace=ws, user=u, role=WorkspaceMembership.EDITOR)
        AgentAdmin.objects.create(agent=echo, user=u)
    stale = in_force(_stored({"user": lender_a}, lender_a, app=sf_app(ws)), echo, by=lender_a)
    live = _stored({"user": lender_b}, lender_b, app=sf_app(ws))
    live.account_id = "00Dorg/005b"
    live.save()
    in_force(live, echo, by=lender_b)
    with pytest.raises(services.ConnectionError):
        services.connection_for_agent(echo, "salesforce")      # both in force: ambiguous
    AgentAdmin.objects.filter(agent=echo, user=lender_a).delete()
    assert services.connection_for_agent(echo, "salesforce") == live
    assert services.providers_for_agent(echo) == ["salesforce"]
    assert stale.grants.filter(agent=echo).exists()           # the row stays; it is just not in force


def test_an_own_connection_not_shared_yet_does_not_shadow_a_lent_one(user, eva, echo):
    in_force(_stored({"agent": echo}, user, account_id="00Dorg/005echo"))
    Connection.objects.filter(agent=echo).update(state=Connection.UNSHARED)
    lent = in_force(_stored({"agent": eva}, user), echo, by=user)
    assert services.connection_for_agent(echo, "salesforce") == lent
    Connection.objects.filter(pk=lent.pk).update(state=Connection.REVOKING)
    with pytest.raises(services.NotConnected):
        services.connection_for_agent(echo, "salesforce")
    assert services.providers_for_agent(echo) == []


def test_complete_refuses_a_grant_missing_a_required_scope(user, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, FakeSF(token=[_resp(body=_token_body(scope="refresh_token"))]))
    assert e.value.code == "insufficient_scope"
    assert not Connection.objects.exists()
    kind, pairs = services.parse_log_line(caplog.records[-1].getMessage())
    assert kind == "provider_error" and pairs["error"] == "insufficient_scope" and pairs["missing"] == "api"
    assert pairs["during"] == "connect" and pairs["owner"] == f"user:{user.pk}"
    assert caplog.records[-1].levelno == logging.WARNING


# ---- connect-time failures are logged -------------------------------------------

def test_a_missing_refresh_token_is_logged(user, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    with pytest.raises(services.ConnectionError):
        _connect({}, ("user", user.pk), user, FakeSF(token=[_resp(body=_token_body(refresh_token=None))]))
    kind, pairs = services.parse_log_line(caplog.records[-1].getMessage())
    assert (kind, pairs["during"], pairs["error"]) == ("provider_error", "connect", "no_refresh_token")


@pytest.mark.parametrize("error,logged", [("server_error", "server_error"),
                                          ("x\ny <script>", "unrecognised"), ("access_denied", None)])
def test_an_authorize_error_is_logged_unless_declined(user, eva, caplog, error, logged):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    session = {}
    url = _begin(session, ("agent", eva.pk))
    state = parse_qs(urlparse(url).query)["state"][0]
    reason = services.authorize_refused(session, provider="salesforce", state=state, error=error, by=user)
    assert reason == ("access_denied" if logged is None else "provider_error")
    assert services.pending_for(session, state) is None             # spent either way
    lines = [r for r in caplog.records if r.getMessage().startswith("connections.")]
    if logged is None:
        assert lines == []
    else:
        kind, pairs = services.parse_log_line(lines[-1].getMessage())
        assert (kind, pairs["during"], pairs["error"], pairs["owner"]) == \
            ("provider_error", "authorize", logged, "agent:eva")


# ---- the log line format --------------------------------------------------------

@pytest.mark.parametrize("value", ["plain", "", "two words", "a=b", 'say "hi"', "line\nbreak", "tab\there",
                                   "naïve-ümlaut", "日本", "\x07bell", "back\\slash"])
def test_log_line_round_trips_any_value(value):
    line = services.log_line("x", {"a": value, "b": "end"})
    assert "\n" not in line
    assert services.parse_log_line(line) == ("x", {"a": value, "b": "end"})


def test_non_ascii_stays_readable_in_the_line():
    assert services.log_line("x", {"by": "rené@dimagi.com"}) == "connections.x by=rené@dimagi.com"


# ---- client authentication ------------------------------------------------------

def test_basic_client_auth_is_used_for_the_exchange_and_revoke(user, monkeypatch):
    monkeypatch.setattr(providers.get_class("salesforce"), "client_auth", "basic")
    seen = []

    def post(url, data=None, headers=None, timeout=None, auth=None, allow_redirects=True):
        seen.append((url, dict(data or {}), auth))
        return _resp(body=_token_body())
    p = providers.for_app(_app_of(("user", user.pk)))
    with mock.patch.multiple("apps.connections.providers.base.requests", post=post):
        p.exchange_code("c0de", REDIRECT, "verifier")
        p.revoke("5Aep_r")
    for url, data, auth in seen:
        assert auth == ("3MVG-client", "shh") and "client_secret" not in data
    assert seen[-1][1] == {"token": "5Aep_r", "token_type_hint": "refresh_token"}


@pytest.mark.parametrize("value", [" para", "line sep", "nel\u0085here", "\x0bvt", "\x1cfs", "zw​sp"])
def test_log_line_escapes_every_line_separator(value):
    line = services.log_line("x", {"a": value, "by": "rené"})
    assert len(line.splitlines()) == 1 and line.isprintable()
    assert "by=rené" in line                                   # printable non-ASCII stays readable
    assert services.parse_log_line(line) == ("x", {"a": value, "by": "rené"})


def test_a_full_scope_grant_satisfies_api(user):
    """A Connected App set to `full` reports "full refresh_token", not `api`."""
    p = providers.for_app(_app_of(("user", user.pk)))
    assert p.missing_scopes(("full", "refresh_token")) == ()
    with pytest.raises(services.ConnectionError) as e:
        _connect({}, ("user", user.pk), user, FakeSF(token=[_resp(body=_token_body(scope="full"))]))
    assert e.value.code == "insufficient_scope"



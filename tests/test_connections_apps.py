"""A workspace's connection app: the OAuth client every connection in it is made with.

Pins the service layer of per-workspace apps — creating, rotating and removing
one, what a provider accepts as its options and scopes — and how the flow and
the token lookups use it: a callback finishes only for the app it was begun
with, a person's token comes only from a workspace they can still read, and a
provider can refuse the account that signed in (`check_owner`).
Salesforce is faked at the `requests` boundary, as everywhere in these suites.
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model

from apps.agents.models import Agent
from apps.connections import providers, services
from apps.connections.models import Connection, ConnectionApp, ConnectionGrant
from apps.connections.problems import problem_for
from apps.connections.providers.base import ProviderError
from apps.connections.providers.salesforce import SalesforceProvider
from apps.events.models import Event
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import REDIRECT, REVOKE_URL, TOKEN_URL, FakeSF, _resp, _stored, _token_body, sf_app

pytestmark = pytest.mark.django_db

OPTIONS = {"login_url": "https://login.salesforce.com", "access_token_seconds": 1800}


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def ws(user):
    w = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.ADMIN)
    return w


@pytest.fixture
def ws2(user):
    w = Workspace.objects.create(slug="w2", display_name="W2", created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.EDITOR)
    return w


@pytest.fixture
def eva(ws, user):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user)


def _configure(ws, by, **kw):
    kw.setdefault("client_id", "3MVG-client")
    kw.setdefault("client_secret", "shh")
    kw.setdefault("options", OPTIONS)
    return services.configure_app(ws, "salesforce", by=by, **kw)


def _flow(session, app, owner):
    url = services.begin(session, app=app, owner=owner, redirect_uri=REDIRECT)
    return parse_qs(urlparse(url).query)


# ---- configuring an app ----------------------------------------------------------

def test_create_stores_the_secret_encrypted_and_records_without_it(ws, user):
    app = _configure(ws, user, options={"login_url": "https://dimagi.my.salesforce.com/some/path"})
    app.refresh_from_db()
    assert app.client_id == "3MVG-client" and app.client_secret == "shh"
    assert "shh" not in app.client_secret_enc
    # Cleaned: host only, defaults filled.
    assert app.options == {"login_url": "https://dimagi.my.salesforce.com", "access_token_seconds": 1800}
    assert app.configured_by == user and app.scopes == ""
    ev = Event.objects.get(kind="connections.app_configured")
    assert ev.workspace_id == "w" and ev.level == "info"
    assert ev.payload["provider"] == "salesforce" and ev.payload["workspace"] == "w"
    assert ev.payload["by"] == "jj@dimagi.com" and ev.payload["client_id"] == "3MVG-client"
    assert "shh" not in json.dumps(ev.payload) + ev.summary


def test_rotating_the_secret_keeps_connections_refreshing_with_the_new_one(ws, user, eva):
    app = _configure(ws, user)
    conn = _stored({"agent": eva}, user, app=app, access="")
    _configure(ws, user, client_secret="shh-rotated")
    with FakeSF() as fake:
        tok = services.access_token_for(conn)
    assert tok.token == "00D!access_1"
    _, _, data = fake.token_calls()[0]
    assert data["client_id"] == "3MVG-client" and data["client_secret"] == "shh-rotated"
    assert Connection.objects.filter(pk=conn.pk).exists()


def test_a_blank_secret_on_update_keeps_the_stored_one(ws, user):
    _configure(ws, user)
    app = _configure(ws, user, client_secret="", options={**OPTIONS, "access_token_seconds": 900})
    app.refresh_from_db()
    assert app.client_secret == "shh" and app.options["access_token_seconds"] == 900
    assert ConnectionApp.objects.count() == 1


def test_the_client_id_is_locked_while_connections_use_it(ws, user, eva):
    app = _configure(ws, user)
    _configure(ws, user, client_id="3MVG-other")                     # no connections yet: fine
    app.refresh_from_db()
    assert app.client_id == "3MVG-other"
    _stored({"agent": eva}, user, app=app)
    with pytest.raises(services.ConnectionError) as e:
        _configure(ws, user, client_id="3MVG-third", client_secret="new")
    assert e.value.code == "client_id_locked"
    app.refresh_from_db()
    assert app.client_id == "3MVG-other" and app.client_secret == "shh"   # nothing half-applied
    assert problem_for(e.value, title="Salesforce", owner="Your").status_code == 409


@pytest.mark.parametrize("kw", [
    {"client_id": ""},
    {"client_secret": ""},                                   # a new app needs a secret
    {"scopes": ["chatter_api"]},                             # Salesforce offers no extra scopes
    {"options": {"login_url": "http://login.salesforce.com"}},
    {"options": {"login_url": "https://evil.example"}},
    {"options": {"login_url": "https://login.salesforce.com.evil.example"}},
    {"options": {"login_url": "https://my.salesforce.com.evil.example"}},
    {"options": {"login_url": "https://.my.salesforce.com"}},
    {"options": {"login_url": "not a url"}},
    {"options": {**OPTIONS, "access_token_seconds": 299}},
    {"options": {**OPTIONS, "access_token_seconds": 86401}},
    {"options": {**OPTIONS, "access_token_seconds": "soon"}},
    {"options": {**OPTIONS, "access_token_seconds": True}},
    {"options": {**OPTIONS, "surprise": "x"}},
])
def test_invalid_settings_are_refused(ws, user, kw):
    with pytest.raises(services.ConnectionError) as e:
        _configure(ws, user, **kw)
    assert e.value.code == "invalid_app" and str(e.value)
    assert not ConnectionApp.objects.exists() and not Event.objects.exists()
    assert problem_for(e.value, title="Salesforce", owner="Your").status_code == 400


@pytest.mark.parametrize("login_url", ["https://login.salesforce.com", "https://test.salesforce.com",
                                       "https://dimagi.my.salesforce.com",
                                       "https://dimagi--sb.sandbox.my.salesforce.com"])
def test_salesforce_login_hosts_accepted(login_url):
    assert SalesforceProvider.clean_options({"login_url": login_url})["login_url"] == login_url


def test_an_omitted_login_url_is_production():
    assert SalesforceProvider.clean_options({}) == OPTIONS


def test_an_unknown_provider_is_refused(ws, user):
    with pytest.raises(providers.UnknownProvider):
        services.configure_app(ws, "nope", client_id="x", client_secret="y", by=user)


# ---- removing an app -------------------------------------------------------------

def test_remove_disconnects_every_connection_and_records(ws, user, eva, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    app = _configure(ws, user)
    echo = Agent.objects.create(slug="echo", name="Echo", workspace=ws, owner=user)
    agents = _stored({"agent": eva}, user, app=app, refresh="5Aep_eva")
    ConnectionGrant.objects.create(connection=agents, agent=echo, granted_by=user)
    person = _stored({"user": user}, user, app=app, refresh="5Aep_jj")
    other_ws = sf_app(Workspace.objects.create(slug="else", display_name="E", created_by=user))
    elsewhere = _stored({"user": user}, user, app=other_ws)
    with FakeSF() as fake:
        services.remove_app(app, by=user)
    revoked = sorted(c[2]["token"] for c in fake.calls if c[1] == REVOKE_URL)
    assert revoked == ["5Aep_eva", "5Aep_jj"]
    assert not Connection.objects.filter(pk__in=[agents.pk, person.pk]).exists()
    assert not ConnectionGrant.objects.exists() and not ConnectionApp.objects.filter(pk=app.pk).exists()
    assert Connection.objects.filter(pk=elsewhere.pk).exists()          # another workspace's app: untouched
    kinds = list(Event.objects.order_by("id").values_list("kind", flat=True))
    assert kinds == ["connections.app_configured", "connections.disconnected", "connections.app_removed"]
    removed = Event.objects.get(kind="connections.app_removed")
    assert removed.workspace_id == "w" and removed.payload["client_id"] == "3MVG-client"
    assert Event.objects.get(kind="connections.disconnected").payload["by"] == "jj@dimagi.com"
    # The person's disconnect is their history: a log line, by the admin.
    lines = [services.parse_log_line(r.getMessage()) for r in caplog.records]
    assert ("disconnected", f"user:{user.pk}", "jj@dimagi.com") in [
        (k, p.get("owner"), p.get("by")) for k, p in lines]
    assert "shh" not in caplog.text


# ---- the flow runs against one app -----------------------------------------------

def test_begin_uses_the_apps_client_and_parks_the_app(ws, user, eva):
    app = sf_app(ws, client_id="ws-client", login_url="https://test.salesforce.com")
    session = {}
    q = _flow(session, app, ("agent", eva.pk))
    assert q["client_id"] == ["ws-client"]
    parked = services.pending_for(session, q["state"][0])
    assert (parked["app"], parked["provider"], parked["workspace"]) == (app.pk, "salesforce", "w")


def test_a_callback_for_another_workspace_is_bad_state_and_exchanges_nothing(ws, ws2, user):
    """Review Focus 1: state parked for workspace B's app, callback for workspace A."""
    sf_app(ws)
    app_b = sf_app(ws2, client_id="b-client")
    session = {}
    state = _flow(session, app_b, ("user", user.pk))["state"][0]
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="salesforce", workspace="w", code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state" and fake.calls == []
    assert not Connection.objects.exists()
    assert services.pending_for(session, state) is None                # spent


def test_a_callback_for_another_provider_is_bad_state(ws, user):
    session = {}
    state = _flow(session, sf_app(ws), ("user", user.pk))["state"][0]
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="google", workspace="w", code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state" and fake.calls == []


def test_an_app_removed_between_the_legs_is_bad_state(ws, user):
    app = sf_app(ws)
    session = {}
    state = _flow(session, app, ("user", user.pk))["state"][0]
    services.remove_app(app, by=user)
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="salesforce", workspace="w", code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state" and fake.calls == []


def test_complete_stores_the_connection_against_its_app(ws, user):
    app = sf_app(ws)
    session = {}
    state = _flow(session, app, ("user", user.pk))["state"][0]
    with FakeSF() as fake:
        conn = services.complete(session, provider="salesforce", workspace=ws, code="c", state=state,
                                 redirect_uri=REDIRECT, by=user)
    assert conn.app_id == app.pk
    assert fake.token_calls()[0][2]["client_secret"] == "shh"


def test_app_for_requires_a_set_up_app(ws):
    with pytest.raises(services.NotConfigured):
        services.app_for(ws, "salesforce")
    app = sf_app(ws)
    assert services.app_for("w", "salesforce") == app
    app.client_secret = ""
    app.save()
    with pytest.raises(services.NotConfigured):
        services.app_for(ws, "salesforce")
    with pytest.raises(providers.UnknownProvider):
        services.app_for(ws, "nope")


# ---- the provider reads its app --------------------------------------------------

class ChattySalesforce(SalesforceProvider):
    """Salesforce with a menu of extra scopes, to pin how chosen ones are used."""

    name = "chatty"
    scope_choices = {"chatter_api": "Chatter", "web": "Web"}


def test_chosen_scopes_are_requested_and_required(ws, user, monkeypatch):
    monkeypatch.setitem(providers.CLASSES, "chatty", ChattySalesforce)
    app = services.configure_app(ws, "chatty", client_id="c", client_secret="s", options=OPTIONS,
                                 scopes=["web", "chatter_api"], by=user)
    assert app.scopes == "chatter_api web"                           # the menu's order
    p = providers.for_app(app)
    assert p.requested_scopes() == ("api", "refresh_token", "chatter_api", "web")
    assert p.missing_scopes(("api", "refresh_token", "web")) == ("chatter_api",)
    assert p.missing_scopes(()) == ()                                 # unsaid: what was asked
    session = {}
    state = _flow(session, app, ("user", user.pk))["state"][0]
    with FakeSF(token=[_resp(body=_token_body(scope="api refresh_token web"))]), \
            pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="chatty", workspace=ws, code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "insufficient_scope"


def test_lifetime_comes_from_the_apps_options(ws):
    app = sf_app(ws, access_token_seconds=600)
    assert providers.for_app(app).access_token_lifetime() == timedelta(seconds=600)
    app.options = {"login_url": "https://login.salesforce.com"}
    assert providers.for_app(app).access_token_lifetime() == timedelta(seconds=1800)


def test_token_and_revoke_go_to_the_apps_login_host(ws, user, eva):
    app = sf_app(ws, login_url="https://test.salesforce.com")
    conn = _stored({"agent": eva}, user, app=app, access="")
    seen = []

    def post(url, data=None, headers=None, timeout=None):
        seen.append(url)
        return _resp(body=_token_body())
    with mock.patch.multiple("apps.connections.providers.base.requests", post=post):
        services.access_token_for(conn)
        services.disconnect(conn, by=user)
    assert seen == ["https://test.salesforce.com/services/oauth2/token",
                    "https://test.salesforce.com/services/oauth2/revoke"]
    assert TOKEN_URL not in seen


# ---- check_owner -------------------------------------------------------------------

class PickySalesforce(SalesforceProvider):
    name = "picky"

    def check_owner(self, identity, *, owner_kind, owner):
        if owner_kind == "agent" and identity.label != f"{owner.slug}@dimagi-ai.com":
            raise ProviderError(f"signed in as {identity.label}", code="wrong_account")


def test_check_owner_refusal_stores_nothing(ws, user, eva, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    monkeypatch.setitem(providers.CLASSES, "picky", PickySalesforce)
    app = sf_app(ws, provider="picky", **OPTIONS)
    session = {}
    state = _flow(session, app, ("agent", eva.pk))["state"][0]
    with FakeSF(), pytest.raises(services.ConnectionError) as e:   # identity: jj@dimagi.com
        services.complete(session, provider="picky", workspace=ws, code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "wrong_account"
    assert not Connection.objects.exists() and not Event.objects.exists()
    kind, pairs = services.parse_log_line(caplog.records[-1].getMessage())
    assert (kind, pairs["during"], pairs["error"], pairs["owner"]) == \
        ("provider_error", "connect", "wrong_account", "agent:eva")
    assert problem_for(e.value, title="Salesforce", owner="eva's").status_code == 409


def test_check_owner_acceptance_connects(ws, user, monkeypatch):
    monkeypatch.setitem(providers.CLASSES, "picky", PickySalesforce)
    app = sf_app(ws, provider="picky", **OPTIONS)
    session = {}
    state = _flow(session, app, ("user", user.pk))["state"][0]
    with FakeSF():
        conn = services.complete(session, provider="picky", workspace=ws, code="c", state=state,
                                 redirect_uri=REDIRECT, by=user)
    assert conn.provider == "picky" and conn.app_id == app.pk


# ---- a person's token comes only from a workspace they can still read ----------------

def test_a_person_who_left_the_workspace_is_not_connected(ws, user):
    """Review Focus 4."""
    _stored({"user": user}, user, app=sf_app(ws))
    assert services.connection_for_person(user, "salesforce").app.workspace_id == "w"
    WorkspaceMembership.objects.filter(user=user, workspace=ws).delete()
    with pytest.raises(services.NotConnected):
        services.connection_for_person(user, "salesforce")


def test_connections_in_two_workspaces_are_ambiguous_unless_one_is_named(ws, ws2, user):
    a = _stored({"user": user}, user, app=sf_app(ws))
    b = _stored({"user": user}, user, app=sf_app(ws2))
    with pytest.raises(services.ConnectionError) as e:
        services.connection_for_person(user, "salesforce")
    assert e.value.code == "ambiguous"
    assert services.connection_for_person(user, "salesforce", workspace="w") == a
    assert services.connection_for_person(user, "salesforce", workspace="w2") == b
    with pytest.raises(services.NotConnected):
        services.connection_for_person(user, "salesforce", workspace="elsewhere")


def test_apps_for_person_are_the_set_up_ones_they_can_read(ws, ws2, user):
    stranger_ws = Workspace.objects.create(slug="theirs", display_name="T", created_by=user)
    sf_app(stranger_ws)
    a = sf_app(ws)
    b = sf_app(ws2)
    assert services.apps_for_person(user, "salesforce") == [a, b]
    b.client_secret = ""
    b.save()
    assert services.apps_for_person(user, "salesforce") == [a]
    assert services.apps_for_person(user, "nope") == []

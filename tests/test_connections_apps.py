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
from django.db import IntegrityError, transaction

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
    assert e.value.code == "app_locked"
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
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:   # identity: jj@dimagi.com
        services.complete(session, provider="picky", workspace=ws, code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "wrong_account"
    assert not Connection.objects.exists() and not Event.objects.exists()
    # The refused account's grant is revoked, not left live at the provider.
    assert fake.calls[-1] == ("POST", REVOKE_URL, {"token": "5Aep_refresh_1", "token_type_hint": "refresh_token",
                                                   "client_id": "3MVG-client", "client_secret": "shh"})
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


# ---- fix round 1 -------------------------------------------------------------------

def test_the_login_host_is_locked_while_connections_use_it(ws, user, eva):
    app = _configure(ws, user)
    _configure(ws, user, client_secret="", options={"login_url": "https://test.salesforce.com"})  # none yet
    _configure(ws, user, client_secret="", options={"login_url": "https://login.salesforce.com"})
    _stored({"agent": eva}, user, app=app)
    with pytest.raises(services.ConnectionError) as e:
        _configure(ws, user, client_secret="", options={"login_url": "https://test.salesforce.com"})
    assert e.value.code == "app_locked" and "login_url" in str(e.value)
    app.refresh_from_db()
    assert app.options["login_url"] == "https://login.salesforce.com"
    # An option that does not name the server may still change.
    _configure(ws, user, client_secret="", options={"access_token_seconds": 900})
    app.refresh_from_db()
    assert app.options == {"login_url": "https://login.salesforce.com", "access_token_seconds": 900}


def test_an_update_that_omits_options_keeps_the_stored_ones(ws, user):
    _configure(ws, user, options={"login_url": "https://test.salesforce.com", "access_token_seconds": 900})
    app = _configure(ws, user, client_secret="", options={})
    assert app.options == {"login_url": "https://test.salesforce.com", "access_token_seconds": 900}
    app = _configure(ws, user, client_secret="", options=None)
    assert app.options["login_url"] == "https://test.salesforce.com"


def test_a_new_client_id_needs_its_secret(ws, user):
    _configure(ws, user)
    with pytest.raises(services.ConnectionError) as e:
        _configure(ws, user, client_id="3MVG-new", client_secret="")
    assert e.value.code == "invalid_app"
    app = ConnectionApp.objects.get()
    assert (app.client_id, app.client_secret) == ("3MVG-client", "shh")


def test_two_first_saves_at_once_end_as_one_app(ws, user, monkeypatch):
    """The other request's insert committed after our lock found no row: our insert
    collides with it, and the retry updates it instead of failing."""
    sf_app(ws, client_id="theirs", secret="their-secret")
    real, calls = services._save_app, []

    def racing(*args):
        calls.append(1)
        if len(calls) == 1:
            raise IntegrityError("duplicate key connectionapp_unique_workspace_provider")
        return real(*args)
    monkeypatch.setattr(services, "_save_app", racing)
    app = _configure(ws, user, client_id="ours", client_secret="our-secret")
    assert len(calls) == 2 and ConnectionApp.objects.count() == 1
    app.refresh_from_db()
    assert (app.client_id, app.client_secret) == ("ours", "our-secret")


def test_a_client_id_change_between_the_legs_is_bad_state(ws, user):
    app = sf_app(ws)
    session = {}
    state = _flow(session, app, ("user", user.pk))["state"][0]
    sf_app(ws, client_id="3MVG-other", secret="other")
    with FakeSF() as fake, pytest.raises(services.ConnectionError) as e:
        services.complete(session, provider="salesforce", workspace=ws, code="c", state=state,
                          redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state" and fake.calls == []


def test_a_client_id_change_during_the_exchange_stores_nothing(ws, user):
    """The app changed client after the exchange began: the grant belongs to the old
    client, so it is refused under the app row's lock rather than stored."""
    app = sf_app(ws)
    session = {}
    state = _flow(session, app, ("user", user.pk))["state"][0]
    with FakeSF() as fake:
        real_post = fake.post

        def post(url, **kw):
            ConnectionApp.objects.filter(pk=app.pk).update(client_id="3MVG-other")
            return real_post(url, **kw)
        with mock.patch("apps.connections.providers.base.requests.post", post), \
                pytest.raises(services.ConnectionError) as e:
            services.complete(session, provider="salesforce", workspace=ws, code="c", state=state,
                              redirect_uri=REDIRECT, by=user)
    assert e.value.code == "bad_state"
    assert not Connection.objects.exists()
    # The grant the exchange produced is revoked rather than left live at Salesforce.
    assert fake.calls[-1][1] == REVOKE_URL and fake.calls[-1][2]["token"] == "5Aep_refresh_1"


def test_remove_catches_a_connection_made_while_it_ran(ws, user, eva, monkeypatch):
    app = _configure(ws, user)
    _stored({"agent": eva}, user, app=app, refresh="5Aep_first")
    real = services.disconnect

    def racing(conn, *, by):
        real(conn, by=by)
        # Another request's connect commits now, after the snapshot was taken.
        _stored({"user": user}, user, app=app, refresh="5Aep_late")
    monkeypatch.setattr(services, "disconnect", racing)
    with FakeSF() as fake:
        services.remove_app(app, by=user)
    revoked = [c[2]["token"] for c in fake.calls if c[1] == REVOKE_URL]
    assert revoked == ["5Aep_first", "5Aep_late"]
    assert not Connection.objects.exists() and not ConnectionApp.objects.exists()


def test_late_connections_are_recorded_as_disconnected_by_the_admin(ws, user, eva, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    app = _configure(ws, user)
    monkeypatch.setattr(services, "disconnect", lambda conn, *, by: None)   # the snapshot "misses" them
    _stored({"user": user}, user, app=app)
    with FakeSF():
        services.remove_app(app, by=user)
    lines = [services.parse_log_line(r.getMessage()) for r in caplog.records]
    assert ("disconnected", f"user:{user.pk}", "jj@dimagi.com") in [
        (k, p.get("owner"), p.get("by")) for k, p in lines]


def test_a_refresh_sends_the_secret_the_app_holds_under_the_lock(ws, user, eva, monkeypatch):
    app = sf_app(ws)
    conn = _stored({"agent": eva}, user, app=app, access="")
    real, seen = services._cached_token, {"n": 0}

    def rotating(c, min_life):
        seen["n"] += 1
        if seen["n"] == 1:   # rotated while this caller waited for the lock
            fresh = ConnectionApp.objects.get(pk=app.pk)
            fresh.client_secret = "shh-rotated"
            fresh.save()
            return ""
        return real(c, min_life)
    monkeypatch.setattr(services, "_cached_token", rotating)
    with FakeSF() as fake:
        services.access_token_for(conn)
    assert fake.token_calls()[0][2]["client_secret"] == "shh-rotated"


def test_a_workspace_with_a_connection_app_cannot_be_deleted_from_under_it(user):
    """PROTECT: a workspace delete never takes an app's connections without
    `remove_app` revoking them first."""
    from django.db.models import ProtectedError
    gone = Workspace.objects.create(slug="gone", display_name="G", created_by=user)
    _stored({"user": user}, user, app=sf_app(gone))
    with pytest.raises(ProtectedError), transaction.atomic():
        gone.delete()
    assert Connection.objects.count() == 1


def test_the_workspace_delete_route_says_to_remove_its_connection_apps_first(user):
    from django.test import Client

    from apps.passkeys.testing import grant_step_up
    gone = Workspace.objects.create(slug="gone", display_name="G", created_by=user)
    WorkspaceMembership.objects.create(workspace=gone, user=user, role=WorkspaceMembership.OWNER)
    app = _stored({"user": user}, user, app=sf_app(gone)).app
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    r = c.delete("/api/workspaces/gone/")
    assert r.status_code == 409
    detail = r.json()["detail"]
    assert "1 connection apps" in detail
    assert "Remove its connection apps in Settings → Connections first." in detail
    with FakeSF() as fake:
        services.remove_app(app, by=user)
    assert [u for _, u, _ in fake.calls if u == REVOKE_URL]     # removing the app revoked the grant
    assert c.delete("/api/workspaces/gone/").status_code == 204
    assert not Workspace.objects.filter(slug="gone").exists() and not Connection.objects.exists()


def test_a_locked_option_stored_in_another_spelling_is_not_a_change(ws, user):
    """The lock compares cleaned values: a stored `https://login.salesforce.com/`
    (a row that predates cleaning) and a saved `https://login.salesforce.com` are
    the same login host, so saving does not trip `app_locked`."""
    app = sf_app(ws)
    ConnectionApp.objects.filter(pk=app.pk).update(options={"login_url": "https://login.salesforce.com/",
                                                            "access_token_seconds": 1800})
    _stored({"user": user}, user, app=app)
    saved = services.configure_app(ws, "salesforce", client_id=app.client_id,
                                   options={"login_url": "https://login.salesforce.com"}, by=user)
    assert saved.options["login_url"] == "https://login.salesforce.com"
    with pytest.raises(services.ConnectionError) as e:
        services.configure_app(ws, "salesforce", client_id=app.client_id,
                               options={"login_url": "https://test.salesforce.com"}, by=user)
    assert e.value.code == "app_locked"


def test_an_unreadable_late_token_is_recorded_after_the_removal_commits(ws, user, monkeypatch):
    from django.db import connection as db

    app = _configure(ws, user)
    monkeypatch.setattr(services, "disconnect", lambda conn, *, by: None)   # the snapshot "misses" it
    late = _stored({"user": user}, user, app=app)
    Connection.objects.filter(pk=late.pk).update(refresh_token_enc="not-fernet")
    depth_outside = len(db.atomic_blocks)
    seen = []
    real = services.record

    def spy(subject, kind, **kw):
        seen.append((kind, kw.get("error"), len(db.atomic_blocks)))
        return real(subject, kind, **kw)
    monkeypatch.setattr(services, "record", spy)
    with FakeSF() as fake:
        services.remove_app(app, by=user)
    assert ("revoke_failed", "undecryptable", depth_outside) in seen
    assert [c for c in fake.calls if c[1] == REVOKE_URL] == []
    assert not Connection.objects.exists() and not ConnectionApp.objects.exists()

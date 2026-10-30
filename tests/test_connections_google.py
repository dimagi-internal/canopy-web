"""Google (Gmail, Calendar) as a connection provider, faked at the `requests`
boundary and driven through the browser connect flow, the token refresh and
disconnect.

Any Google account connects by default — a personal @gmail.com one or another
organisation's — unless the workspace's app lists `allowed_domains`; an agent
must connect its own Google account (`Agent.google_identity()`) when it has one.
"""
from __future__ import annotations

from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.common.encryption import decrypt_secret
from apps.connections import providers, services
from apps.connections.models import Connection
from apps.connections.providers.google import (
    AUTHORIZE_URL,
    REVOKE_URL,
    TOKEN_URL,
    USERINFO_URL,
    GoogleProvider,
)
from apps.workspaces.models import Workspace, WorkspaceMembership

pytestmark = pytest.mark.django_db

G = "https://www.googleapis.com/auth/"
BASE = f"openid {G}userinfo.email"
READ_MAIL, CALENDAR = f"{G}gmail.readonly", f"{G}calendar.readonly"


class FakeGoogle:
    """Google at the `requests` boundary: the token response carries `expires_in`
    and the granted `scope` as space-separated URLs; a refresh omits
    `refresh_token`; userinfo answers for whoever `self.user` says signed in."""

    def __init__(self):
        self.calls: list[tuple[str, str, dict]] = []
        self.n = 0
        self.granted = f"{BASE} {READ_MAIL} {CALENDAR}"
        self.user = {"sub": "1098", "email": "someone@gmail.com", "email_verified": True}

    def _resp(self, body, status=200):
        r = mock.Mock(status_code=status)
        r.json.return_value = body
        return r

    def post(self, url, data=None, headers=None, timeout=None):
        assert timeout == 15
        self.calls.append(("POST", url, dict(data or {})))
        if url == TOKEN_URL:
            self.n += 1
            body = {"access_token": f"ya29.access-{self.n}", "expires_in": 3599, "token_type": "Bearer",
                    "scope": self.granted, "id_token": "eyJ.not.read"}
            if data["grant_type"] == "authorization_code":
                body["refresh_token"] = "1//refresh"
            return self._resp(body)
        if url == REVOKE_URL:
            return self._resp({})
        raise AssertionError(url)

    def get(self, url, headers=None, timeout=None):
        assert url == USERINFO_URL and timeout == 15
        self.calls.append(("GET", url, dict(headers or {})))
        return self._resp(self.user)

    def __enter__(self):
        self._p = mock.patch.multiple("apps.connections.providers.base.requests", post=self.post, get=self.get)
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


@pytest.fixture(autouse=True)
def google():
    with FakeGoogle() as fake:
        yield fake


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def ws(user):
    """The workspace with Google set up, mail and calendar read chosen, no domain rule."""
    w = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.EDITOR)
    services.configure_app(w, "google", client_id="123.apps.googleusercontent.com", client_secret="GOCSPX-s",
                           scopes=[READ_MAIL, CALENDAR], by=user)
    return w


def _domains(ws, user, value):
    return services.configure_app(ws, "google", client_id="123.apps.googleusercontent.com",
                                  options={"allowed_domains": value}, scopes=[READ_MAIL, CALENDAR], by=user)


def _agent(user, ws, **kw):
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user, **kw)
    AgentAdmin.objects.get_or_create(agent=agent, user=user)
    return agent


def _connect(user, **agent):
    client = Client()
    client.force_login(user)
    start = reverse("connections_start", args=["google", "w"])
    params = {"return_to": "/settings", **agent}
    resp = client.post(start, params) if agent else client.get(start, params)
    assert resp.status_code == 302, resp.content
    authorize = urlparse(resp["Location"])
    query = {k: v[0] for k, v in parse_qs(authorize.query).items()}
    landed = client.get(reverse("connections_callback", args=["google", "w"]),
                        {"code": "4/code", "state": query["state"]})
    return authorize, query, landed["Location"]


# ---- a person connects ---------------------------------------------------------------

def test_a_person_connects_with_offline_access_and_the_chosen_scopes(google, user, ws):
    authorize, query, landed = _connect(user)
    assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == AUTHORIZE_URL
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert "login_hint" not in query and "hd" not in query
    assert query["scope"] == f"{BASE} {READ_MAIL} {CALENDAR}"
    assert query["client_id"] == "123.apps.googleusercontent.com"
    assert query["code_challenge_method"] == "S256"
    assert "status=connected" in landed

    conn = Connection.objects.get(provider="google", user=user)
    assert conn.account_id == "1098" and conn.account_label == "someone@gmail.com"
    assert conn.instance_url == ""                                     # the API host is fixed
    assert conn.scopes == " ".join(sorted(google.granted.split()))
    assert timezone.now() + timedelta(seconds=3500) < conn.access_token_expires_at \
        <= timezone.now() + timedelta(seconds=3599)
    # The userinfo lookup carried the new access token as a bearer.
    assert ("GET", USERINFO_URL, {"Authorization": "Bearer ya29.access-1", "Accept": "application/json"}) \
        in google.calls


@pytest.mark.parametrize("email", ["someone@gmail.com", "x@marshellis.com"])
def test_with_no_domain_rule_any_google_account_connects(google, user, ws, email):
    google.user = {"sub": "1", "email": email, "email_verified": True}
    _, _, landed = _connect(user)
    assert "status=connected" in landed
    assert Connection.objects.get(provider="google").account_label == email


def test_a_grant_missing_a_chosen_scope_is_refused(google, user, ws):
    google.granted = f"{BASE} {READ_MAIL}"                    # the person unticked Calendar
    _, _, landed = _connect(user)
    assert "reason=insufficient_scope" in landed
    assert not Connection.objects.exists()


def test_a_grant_missing_the_identity_scope_is_refused(google, user, ws):
    google.granted = f"openid {READ_MAIL} {CALENDAR}"
    _, _, landed = _connect(user)
    assert "reason=insufficient_scope" in landed
    assert not Connection.objects.exists()


@pytest.mark.parametrize("user_info", [
    {"sub": "1", "email": "someone@gmail.com", "email_verified": False},
    {"sub": "1", "email": "someone@gmail.com"},                       # verification not stated
    {"email": "someone@gmail.com", "email_verified": True},           # no sub
    {"sub": "1", "email_verified": True},                             # no email
])
def test_an_unverified_or_incomplete_identity_is_refused(google, user, ws, user_info):
    google.user = user_info
    _, _, landed = _connect(user)
    assert "status=error" in landed and "reason=provider_error" in landed
    assert not Connection.objects.exists()


# ---- allowed domains ----------------------------------------------------------------

def test_one_allowed_domain_hints_it_and_refuses_another(google, user, ws):
    _domains(ws, user, "Dimagi.com")
    _, query, landed = _connect(user)
    assert query["hd"] == "dimagi.com"
    assert "reason=wrong_account" in landed                  # someone@gmail.com
    assert not Connection.objects.exists()

    google.user = {"sub": "2", "email": "JJ@Dimagi.com", "email_verified": True}
    _, _, landed = _connect(user)
    assert "status=connected" in landed
    assert Connection.objects.get().account_label == "jj@dimagi.com"


def test_several_allowed_domains_send_no_hd(google, user, ws):
    _domains(ws, user, "dimagi.com, dimagi-ai.com")
    google.user = {"sub": "3", "email": "eva@dimagi-ai.com", "email_verified": True}
    _, query, landed = _connect(user)
    assert "hd" not in query
    assert "status=connected" in landed


def test_allowed_domains_are_normalised_and_checked(user, ws):
    app = _domains(ws, user, " Dimagi.com,dimagi-ai.com  dimagi.com ")
    assert app.options == {"allowed_domains": "dimagi.com, dimagi-ai.com"}
    assert providers.for_app(app).allowed_domains == ("dimagi.com", "dimagi-ai.com")
    assert _domains(ws, user, "  ").options == {}                    # blank: any account
    for bad in ("dimagi", "@dimagi.com", "dimagi.com/x", "-x.com", "x..com", "jj@dimagi.com"):
        with pytest.raises(services.ConnectionError) as e:
            _domains(ws, user, bad)
        assert e.value.code == "invalid_app", bad


def test_allowed_domains_may_change_while_connections_exist(google, user, ws):
    """It gates new connects only: a connection made before stays."""
    _connect(user)
    _domains(ws, user, "dimagi.com")
    conn = Connection.objects.get(account_label="someone@gmail.com")
    assert services.access_token_for(conn, refresh=True).token == "ya29.access-2"


# ---- an agent connects --------------------------------------------------------------

def test_an_agent_is_hinted_and_must_sign_in_as_its_own_account(google, user, ws):
    """Review Focus 5."""
    eva = _agent(user, ws, google_account="Eva@Dimagi-AI.com")
    _, query, landed = _connect(user, agent="eva")
    assert query["prompt"] == "consent select_account" and query["access_type"] == "offline"
    assert query["login_hint"] == "eva@dimagi-ai.com"
    assert "reason=wrong_account" in landed                  # the admin's browser was someone@gmail.com
    assert not Connection.objects.exists()

    google.user = {"sub": "4", "email": "EVA@dimagi-ai.com", "email_verified": True}
    _, _, landed = _connect(user, agent="eva")
    assert "status=connected" in landed
    assert Connection.objects.get(agent=eva).account_label == "eva@dimagi-ai.com"


def test_an_agent_with_no_google_identity_is_not_hinted_and_takes_any_account(google, user, ws):
    eva = _agent(user, ws)                                    # no email, no google_account
    assert eva.google_identity() == ""
    _, query, landed = _connect(user, agent="eva")
    assert query["prompt"] == "consent select_account" and "login_hint" not in query
    assert "status=connected" in landed


# ---- refresh and disconnect ---------------------------------------------------------

def test_refresh_keeps_the_refresh_token_google_does_not_resend(google, user, ws):
    _connect(user)
    conn = Connection.objects.get()
    token = services.access_token_for(conn, refresh=True)
    assert token.token == "ya29.access-2" and token.refreshed
    refresh = [c for c in google.calls if c[1] == TOKEN_URL][-1][2]
    assert refresh == {"grant_type": "refresh_token", "refresh_token": "1//refresh",
                       "client_id": "123.apps.googleusercontent.com", "client_secret": "GOCSPX-s"}
    conn.refresh_from_db()
    assert decrypt_secret(conn.refresh_token_enc) == "1//refresh"
    assert conn.access_token_seconds == 3599
    assert GoogleProvider.rotates_refresh_token is False


def test_disconnect_revokes_the_refresh_token_at_google(google, user, ws):
    _connect(user)
    services.disconnect(Connection.objects.get(), by=user)
    method, url, data = google.calls[-1]
    assert (method, url, data["token"]) == ("POST", REVOKE_URL, "1//refresh")
    assert not Connection.objects.exists()


# ---- the provider itself ------------------------------------------------------------

def test_google_is_registered_with_its_scope_choices():
    cls = providers.get_class("google")
    assert cls is GoogleProvider and cls.locked_options == ()
    assert cls.required_scopes == ("openid", f"{G}userinfo.email")
    assert dict(cls.scope_choices) == {
        f"{G}gmail.readonly": "Read mail", f"{G}gmail.compose": "Write drafts",
        f"{G}gmail.send": "Send mail", f"{G}gmail.modify": "Label and archive mail",
        f"{G}calendar.readonly": "Read calendars", f"{G}calendar.events": "Edit calendar events",
    }
    # Every scope at once still fits a connection's stored scopes.
    assert len(" ".join((*cls.scopes, *cls.scope_choices))) <= Connection._meta.get_field("scopes").max_length

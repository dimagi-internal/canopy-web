"""A provider is one module: nothing else in canopy-web names one.

`Acme` below is everything a new provider (Sage Intacct next) is: a
`Provider` subclass with endpoints, its own authorize parameters and an
identity lookup, used with a workspace's app (client id and secret). It is registered for these tests only, and
then driven through every surface — the Settings list, the browser connect flow
for a person and for an agent, the CLI token route, the runner's turn routes
and disconnect — without touching shared code. It also differs from Salesforce
where the next providers do: the token response carries `expires_in`, there is
no per-account API host, and refresh does not rotate the refresh token.
"""
from __future__ import annotations

import uuid
from datetime import timedelta
from unittest import mock
from urllib.parse import parse_qs, urlparse

import pytest
import requests
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from apps.agents.models import Agent, AgentAdmin
from apps.common.encryption import decrypt_secret
from apps.connections import providers
from apps.connections.models import Connection
from apps.connections.providers.base import Identity, Provider
from apps.harness.models import Runner, RunnerAccessToken, RunnerAssignment, RunnerDeviceKey, Turn
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import sf_app

pytestmark = pytest.mark.django_db

AUTHORIZE = "https://login.acme.test/oauth/authorize"
TOKEN = "https://login.acme.test/oauth/token"
REVOKE = "https://login.acme.test/oauth/revoke"
USERINFO = "https://api.acme.test/v1/me"


class AcmeProvider(Provider):
    name = "acme"
    title = "Acme Books"
    scopes = ("books.read", "offline_access")
    required_scopes = ("books.read",)
    rotates_refresh_token = False

    def authorize_url_base(self) -> str:
        return AUTHORIZE

    def token_url(self) -> str:
        return TOKEN

    def revoke_url(self) -> str:
        return REVOKE

    def authorize_params(self, *, owner_kind, owner):
        # Google's shape: an agent's account is named up front (`login_hint`).
        if owner_kind == "agent":
            return {"access_type": "offline", "prompt": "select_account consent", "login_hint": owner.email}
        return {"access_type": "offline", "prompt": "consent"}

    def identity(self, token):
        resp = self._call(requests.get, USERINFO, "identity lookup",
                          headers={"Authorization": f"Bearer {token.access_token}"})
        body = self._json(resp, "identity lookup")
        return Identity(account_id=body["sub"], label=body["email"])


class FakeAcme:
    """Acme at the `requests` boundary: tokens carry `expires_in`, no instance URL,
    and a refresh never returns a new refresh token."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.n = 0
        self.granted = "books.read offline_access"

    def _resp(self, body):
        r = mock.Mock(status_code=200)
        r.json.return_value = body
        return r

    def post(self, url, data=None, headers=None, timeout=None):
        assert timeout == 15
        self.calls.append((url, dict(data or {})))
        if url == TOKEN:
            self.n += 1
            body = {"access_token": f"acme-access-{self.n}", "expires_in": 1200, "token_type": "Bearer",
                    "scope": self.granted}
            if data["grant_type"] == "authorization_code":
                body["refresh_token"] = "acme-refresh"
            return self._resp(body)
        if url == REVOKE:
            return self._resp({})
        raise AssertionError(url)

    def get(self, url, headers=None, timeout=None):
        assert url == USERINFO and timeout == 15
        return self._resp({"sub": "acme-user-1", "email": "jj@acme.test"})

    def __enter__(self):
        self._p = mock.patch.multiple("apps.connections.providers.base.requests", post=self.post, get=self.get)
        self._p.start()
        return self

    def __exit__(self, *exc):
        self._p.stop()


@pytest.fixture(autouse=True)
def acme(monkeypatch):
    monkeypatch.setitem(providers.CLASSES, "acme", AcmeProvider)
    with FakeAcme() as fake:
        yield fake


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def ws(user):
    """The workspace with Acme set up: its admin saved the client id and secret."""
    w = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.EDITOR)
    sf_app(w, provider="acme", client_id="acme-client", secret="acme-secret")
    return w


@pytest.fixture
def eva(user, ws):
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user, email="eva@agents.test")
    AgentAdmin.objects.get_or_create(agent=agent, user=user)
    return agent


def _connect(client, **agent):
    start = reverse("connections_start", args=["acme", "w"])
    params = {"return_to": "/settings", **agent}
    resp = client.post(start, params) if agent else client.get(start, params)
    assert resp.status_code == 302, resp.content
    authorize = urlparse(resp["Location"])
    query = {k: v[0] for k, v in parse_qs(authorize.query).items()}
    landed = client.get(reverse("connections_callback", args=["acme", "w"]),
                        {"code": "acme-code", "state": query["state"]})
    return authorize, query, landed


def test_a_person_connects_lists_and_fetches_a_token(acme, user, ws):
    client = Client()
    client.force_login(user)
    listed = client.get("/api/connections/").json()
    assert {"name": "acme", "title": "Acme Books", "workspace": "w", "workspace_name": "W",
            "configured": True} in listed["providers"]

    authorize, query, landed = _connect(client)
    assert f"{authorize.scheme}://{authorize.netloc}{authorize.path}" == AUTHORIZE
    assert query["access_type"] == "offline" and query["prompt"] == "consent"
    assert query["scope"] == "books.read offline_access" and query["code_challenge_method"] == "S256"
    assert landed.status_code == 302 and "connection=acme&status=connected" in landed["Location"]

    conn = Connection.objects.get(provider="acme", user=user)
    assert conn.account_id == "acme-user-1" and conn.account_label == "jj@acme.test"
    assert conn.instance_url == ""                                   # no per-account API host
    # Expiry from the response's `expires_in` (1200 s), not a configured lifetime.
    assert timezone.now() + timedelta(seconds=1100) < conn.access_token_expires_at \
        <= timezone.now() + timedelta(seconds=1200)

    row = client.get("/api/connections/").json()["connections"][0]
    assert row["title"] == "Acme Books" and row["scopes"] == ["books.read", "offline_access"]

    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    pat = Client(HTTP_AUTHORIZATION=f"Bearer {raw}")
    got = pat.post("/api/me/connections/acme/token")
    assert got.status_code == 200 and got.json()["access_token"] == "acme-access-1"
    assert got.json()["instance_url"] == ""
    refreshed = pat.post("/api/me/connections/acme/token?refresh=true")
    assert refreshed.json()["access_token"] == "acme-access-2"
    conn.refresh_from_db()
    assert decrypt_secret(conn.refresh_token_enc) == "acme-refresh"   # not rotated: kept

    assert client.delete(f"/api/connections/{conn.pk}").status_code == 204
    assert acme.calls[-1] == (REVOKE, {"token": "acme-refresh", "token_type_hint": "refresh_token",
                                       "client_id": "acme-client", "client_secret": "acme-secret"})


def test_an_agent_connects_and_its_turn_lists_and_fetches_the_token(acme, user, eva):
    client = Client()
    client.force_login(user)
    _, query, landed = _connect(client, agent="eva")
    assert query["prompt"] == "select_account consent"          # the provider's own agent parameter
    assert query["login_hint"] == "eva@agents.test"             # it was given the agent itself
    assert "status=connected" in landed["Location"]
    assert Connection.objects.filter(provider="acme", agent=eva).exists()

    runner = Runner.objects.create(name="box", kind=Runner.CLOUD, owner=user, workspace=eva.workspace,
                                   last_heartbeat_at=timezone.now(), status=Runner.ONLINE)
    RunnerAssignment.objects.create(agent=eva, runner=runner, rank=0)
    key = RunnerDeviceKey.objects.create(runner=runner, kind=RunnerDeviceKey.SOFTWARE,
                                         jkt=uuid.uuid4().hex, status=RunnerDeviceKey.ACTIVE)
    bearer, _ = RunnerAccessToken.mint(key)
    turn = Turn.objects.create(agent=eva, origin=Turn.ORIGIN_EMAIL, prompt="x", idempotency_key="k",
                               status=Turn.CLAIMED, claimed_by=runner)
    box = Client(HTTP_AUTHORIZATION=f"Bearer {bearer}")
    base = f"/api/harness/runners/{runner.pk}/turns/{turn.pk}"
    assert box.get(f"{base}/connections").json() == {"providers": ["acme"]}
    got = box.post(f"{base}/connection-token", {"provider": "acme"}, content_type="application/json")
    assert got.status_code == 200 and got.json()["access_token"] == "acme-access-1"


def test_an_unregistered_provider_is_404_everywhere(user):
    client = Client()
    client.force_login(user)
    assert client.get(reverse("connections_start", args=["nope", "w"])).status_code == 404
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    assert Client(HTTP_AUTHORIZATION=f"Bearer {raw}").post("/api/me/connections/nope/token").status_code == 404


def test_a_grant_with_fewer_scopes_than_required_is_refused(acme, user, ws):
    acme.granted = "offline_access"                           # the person unticked books.read
    client = Client()
    client.force_login(user)
    _, _, landed = _connect(client)
    assert "reason=insufficient_scope" in landed["Location"]
    assert not Connection.objects.filter(provider="acme").exists()


def test_the_stored_scopes_are_the_granted_ones(acme, user, ws):
    acme.granted = "books.read offline_access books.write"
    client = Client()
    client.force_login(user)
    _connect(client)
    assert Connection.objects.get(provider="acme").scopes == "books.read books.write offline_access"

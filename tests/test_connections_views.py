"""The browser legs of the connections flow (start, callback), person and agent.

The service layer is covered in `test_connections_services.py`; this pins what
only the views decide: who may start a flow for whom, where the browser lands
afterwards, that `return_to` cannot leave the site, and that an agent flow is
re-authorized at completion (admin can be revoked between the two legs).
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.agents.models import Agent
from apps.connections.models import Connection
from apps.workspaces.models import Workspace, WorkspaceMembership

from .test_connections_services import SF, FakeSF

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
def client(user):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def eva(user):
    w = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.EDITOR)
    return Agent.objects.create(slug="eva", name="Eva", workspace=w, owner=user)


def _start(client, **params):
    return client.get(reverse("connections_start", args=["salesforce"]), params)


def _state(resp):
    return parse_qs(urlparse(resp["Location"]).query)["state"][0]


def _callback(client, state, **extra):
    return client.get(reverse("connections_callback", args=["salesforce"]),
                      {"code": "c0de", "state": state, **extra})


def _landing(resp):
    u = urlparse(resp["Location"])
    return u.path, {k: v[0] for k, v in parse_qs(u.query).items()}


def test_start_redirects_to_salesforce_for_self(client):
    resp = _start(client)
    assert resp.status_code == 302
    loc = urlparse(resp["Location"])
    assert loc.netloc == "login.salesforce.com" and loc.path == "/services/oauth2/authorize"
    q = parse_qs(loc.query)
    assert q["redirect_uri"][0].endswith(reverse("connections_callback", args=["salesforce"]))
    assert q["code_challenge_method"] == ["S256"]


def test_full_person_flow_connects_and_returns(client, user):
    state = _state(_start(client, return_to="/settings?tab=x"))
    with FakeSF():
        resp = _callback(client, state)
    path, q = _landing(resp)
    assert path == "/settings"
    assert q == {"tab": "x", "connection": "salesforce", "status": "connected"}
    conn = Connection.objects.get()
    assert conn.user_id == user.pk and conn.agent_id is None


def test_start_for_agent_requires_admin(client, user, eva):
    other = get_user_model().objects.create_user(username="x", email="x@dimagi.com")
    c = Client()
    c.force_login(other)
    assert _start(c, agent="eva").status_code == 403
    assert _start(client, agent="eva").status_code == 302
    assert _start(client, agent="nope").status_code == 404


def test_agent_flow_stores_agent_owned_connection(client, eva):
    state = _state(_start(client, agent="eva"))
    with FakeSF():
        resp = _callback(client, state)
    assert _landing(resp)[1]["status"] == "connected"
    assert Connection.objects.get().agent_id == eva.pk


def test_callback_refuses_when_admin_revoked_between_legs(client, user, eva):
    state = _state(_start(client, agent="eva"))
    eva.owner = None
    eva.save()
    WorkspaceMembership.objects.filter(user=user).delete()
    assert not eva.is_admin(user)
    with FakeSF() as fake:
        resp = _callback(client, state)
    assert _landing(resp)[1] == {"connection": "salesforce", "status": "error", "reason": "forbidden"}
    assert not Connection.objects.exists()
    assert fake.token_calls() == []


@pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example/x", "settings", "/\\evil.example", ""])
def test_return_to_rejects_offsite(client, bad):
    state = _state(_start(client, return_to=bad))
    with FakeSF():
        resp = _callback(client, state)
    assert _landing(resp)[0] == "/settings"
    assert "evil" not in resp["Location"]


def test_not_configured_redirects_with_reason(client, settings):
    settings.SALESFORCE_CLIENT_SECRET = ""
    resp = _start(client, return_to="/settings")
    assert _landing(resp) == ("/settings", {"connection": "salesforce", "status": "error",
                                            "reason": "not_configured"})


def test_unknown_provider_is_404(client):
    assert client.get(reverse("connections_start", args=["nope"])).status_code == 404


def test_callback_bad_state_and_provider_denied(client):
    resp = _callback(client, "bogus")
    assert _landing(resp)[1]["reason"] == "bad_state"
    state = _state(_start(client))
    resp = client.get(reverse("connections_callback", args=["salesforce"]),
                      {"error": "access_denied", "state": state})
    assert _landing(resp)[1]["reason"] == "access_denied"
    assert not Connection.objects.exists()


def test_start_requires_login():
    resp = Client().get(reverse("connections_start", args=["salesforce"]))
    assert resp.status_code == 302 and "salesforce.com" not in resp["Location"]

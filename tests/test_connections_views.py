"""The browser legs of the connections flow (start, callback), person and agent.

The service layer is covered in `test_connections_services.py`; this pins what
only the views decide: who may start a flow for whom, where the browser lands
afterwards, that `return_to` cannot leave the site, and that an agent flow is
re-authorized at completion (admin can be revoked between the two legs).
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse, urlsplit

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from apps.agents.models import Agent
from apps.connections.models import Connection
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import FakeSF, _resp, _token_body, sf_app

pytestmark = pytest.mark.django_db


@pytest.fixture
def user():
    return get_user_model().objects.create_user(username="jj", email="jj@dimagi.com")


@pytest.fixture
def ws(user):
    """The person's one workspace, with Salesforce set up: where their flow connects."""
    w = Workspace.objects.create(slug="w", display_name="W", created_by=user)
    WorkspaceMembership.objects.create(workspace=w, user=user, role=WorkspaceMembership.EDITOR)
    sf_app(w)
    return w


@pytest.fixture
def client(user, ws):
    c = Client()
    c.force_login(user)
    return c


@pytest.fixture
def eva(user, ws):
    return Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=user)


def _start(client, **params):
    """An agent's flow starts with a POST (the panel's form); a person's with a GET link."""
    url = reverse("connections_start", args=["salesforce", "w"])
    return client.post(url, params) if "agent" in params else client.get(url, params)


def _state(resp):
    return parse_qs(urlparse(resp["Location"]).query)["state"][0]


def _callback(client, state, **extra):
    return client.get(reverse("connections_callback", args=["salesforce", "w"]),
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
    assert q["redirect_uri"][0].endswith(reverse("connections_callback", args=["salesforce", "w"]))
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


@pytest.mark.parametrize("bad", ["https://evil.example/x", "//evil.example/x", "settings", "/\\evil.example", "",
                                 "/\t/evil.example", "/\r\n/evil.example", "/x\nLocation: //evil.example"])
def test_return_to_rejects_offsite(client, bad):
    state = _state(_start(client, return_to=bad))
    with FakeSF():
        resp = _callback(client, state)
    assert _landing(resp)[0] == "/settings"
    assert "evil" not in resp["Location"]


def test_not_configured_redirects_with_reason(client, ws):
    app = sf_app(ws)
    app.client_secret = ""
    app.save()
    resp = _start(client, return_to="/settings")
    assert _landing(resp) == ("/settings", {"connection": "salesforce", "status": "error",
                                            "reason": "not_configured"})


def test_an_agents_start_uses_its_own_workspaces_app(client, user, eva):
    # The person's workspace has an app; eva's own has another, with its own client.
    other = Workspace.objects.create(slug="eva-home", display_name="E", created_by=user)
    WorkspaceMembership.objects.create(workspace=other, user=user, role=WorkspaceMembership.OWNER)
    eva.workspace = other
    eva.save()
    sf_app(other, client_id="eva-client")
    url = reverse("connections_start", args=["salesforce", "eva-home"])
    q = parse_qs(urlparse(client.post(url, {"agent": "eva"})["Location"]).query)
    assert q["client_id"] == ["eva-client"]
    assert q["redirect_uri"][0].endswith("/auth/connections/salesforce/eva-home/callback/")
    eva.workspace.connection_apps.all().delete()
    resp = client.post(url, {"agent": "eva", "return_to": "/settings"})
    assert _landing(resp)[1]["reason"] == "not_configured"


def test_a_persons_start_uses_the_named_workspaces_app(user, ws):
    c = Client()
    c.force_login(user)
    second = Workspace.objects.create(slug="w2", display_name="W2", created_by=user)
    WorkspaceMembership.objects.create(workspace=second, user=user, role=WorkspaceMembership.EDITOR)
    sf_app(second, client_id="w2-client")
    for slug, client_id in (("w", "3MVG-client"), ("w2", "w2-client")):
        q = _authorize_query(c.get(reverse("connections_start", args=["salesforce", slug])))
        assert q["client_id"] == [client_id]
        assert q["redirect_uri"][0] == f"http://testserver/auth/connections/salesforce/{slug}/callback/"
    ws.connection_apps.all().delete()
    assert _landing(_start(c))[1]["reason"] == "not_configured"     # w names no app now
    location = c.get(reverse("connections_start", args=["salesforce", "w2"]))["Location"]
    assert urlsplit(location).hostname == "login.salesforce.com"


def test_unknown_provider_is_404(client):
    assert client.get(reverse("connections_start", args=["nope", "w"])).status_code == 404


def test_callback_bad_state_and_provider_denied(client):
    resp = _callback(client, "bogus")
    assert _landing(resp)[1]["reason"] == "bad_state"
    state = _state(_start(client))
    resp = client.get(reverse("connections_callback", args=["salesforce", "w"]),
                      {"error": "access_denied", "state": state})
    assert _landing(resp)[1]["reason"] == "access_denied"
    assert not Connection.objects.exists()


def test_start_requires_login(ws):
    resp = Client().get(reverse("connections_start", args=["salesforce", "w"]))
    assert resp.status_code == 302 and "salesforce.com" not in resp["Location"]


# ---- an agent's connect signs in as the agent's Salesforce user (I3) ---------------

def _authorize_query(resp):
    return parse_qs(urlparse(resp["Location"]).query)


def test_agent_start_forces_a_salesforce_login(client, eva):
    q = _authorize_query(_start(client, agent="eva"))
    assert q["prompt"] == ["login consent"]


def test_person_start_does_not_force_a_login(client):
    assert "prompt" not in _authorize_query(_start(client))


def test_agent_start_by_get_is_405(client, eva):
    resp = client.get(reverse("connections_start", args=["salesforce", "w"]), {"agent": "eva"})
    assert resp.status_code == 405


def test_agent_start_needs_a_csrf_token(user, eva):
    c = Client(enforce_csrf_checks=True)
    c.force_login(user)
    url = reverse("connections_start", args=["salesforce", "w"])
    assert c.post(url, {"agent": "eva"}).status_code == 403
    c.get("/api/csrf/")
    token = c.cookies[settings_csrf_cookie()].value
    resp = c.post(url, {"agent": "eva", "csrfmiddlewaretoken": token})
    assert resp.status_code == 302 and urlsplit(resp["Location"]).hostname == "login.salesforce.com"


def settings_csrf_cookie():
    from django.conf import settings

    return settings.CSRF_COOKIE_NAME


# ---- the callback finishes only for whoever started it -----------------------------

def test_person_flow_finished_by_another_user_is_forbidden(client, user):
    state = _state(_start(client))
    other = get_user_model().objects.create_user(username="x", email="x@dimagi.com")
    session = client.session
    # Same browser session, now signed in as someone else (the parked state survives).
    pending = session["connections_oauth"]
    client.force_login(other)
    s2 = client.session
    s2["connections_oauth"] = pending
    s2.save()
    with FakeSF() as fake:
        resp = _callback(client, state)
    assert _landing(resp)[1] == {"connection": "salesforce", "status": "error", "reason": "forbidden"}
    assert not Connection.objects.exists() and fake.token_calls() == []


def test_revoked_admin_with_provider_error_is_still_forbidden(client, user, eva):
    state = _state(_start(client, agent="eva"))
    eva.owner = None
    eva.save()
    WorkspaceMembership.objects.filter(user=user).delete()
    resp = client.get(reverse("connections_callback", args=["salesforce", "w"]),
                      {"error": "access_denied", "state": state})
    assert _landing(resp)[1]["reason"] == "forbidden"
    assert not Connection.objects.exists()


def test_a_grant_without_a_required_scope_lands_as_insufficient_scope(client):
    state = _state(_start(client))
    with FakeSF(token=[_resp(body=_token_body(scope="refresh_token"))]):
        resp = _callback(client, state)
    assert _landing(resp)[1] == {"connection": "salesforce", "status": "error", "reason": "insufficient_scope"}
    assert not Connection.objects.exists()


def test_a_provider_error_at_authorize_lands_as_provider_error(client):
    state = _state(_start(client))
    resp = client.get(reverse("connections_callback", args=["salesforce", "w"]),
                      {"error": "temporarily_unavailable", "state": state})
    assert _landing(resp)[1]["reason"] == "provider_error"

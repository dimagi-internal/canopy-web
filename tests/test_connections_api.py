"""The JSON routes of connections: my status/disconnect, an agent's, grants, my CLI token.

The service layer is pinned in `test_connections_services.py`; this pins what
only the routes decide: whose rows a caller can see or delete, who may administer
an agent's connection, that the CLI token route refuses a browser session, the
409 bodies a client branches on, and that a token never reaches a log or event.
"""
from __future__ import annotations

import logging
from datetime import timedelta

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.connections.models import Connection, ConnectionGrant
from apps.events.models import Event
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership

from .test_connections_services import REVOKE_URL, SF, FakeSF, _resp, _stored, _token_body

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def configured(settings):
    for k, v in SF.items():
        setattr(settings, k, v)
    return settings


def _mk(username):
    return get_user_model().objects.create_user(username=username, email=f"{username}@dimagi.com")


@pytest.fixture
def user():
    return _mk("jj")


@pytest.fixture
def other():
    return _mk("other")


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


@pytest.fixture
def client(user):
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    return c


def _pat(user) -> dict:
    raw, _ = PersonalToken.create_for_user(user=user, label="test")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


# ---- list / disconnect (mine) ---------------------------------------------------

def test_list_shows_providers_and_only_my_connections(client, user, other):
    mine = _stored({"user": user}, user)
    _stored({"user": other}, other)
    body = client.get("/api/connections/").json()
    assert body["providers"] == [{"name": "salesforce", "title": "Salesforce", "configured": True}]
    assert [c["id"] for c in body["connections"]] == [mine.pk]
    c = body["connections"][0]
    assert c["provider"] == "salesforce" and c["account_label"] == "jj@dimagi.com"
    assert c["needs_reconnect"] is False and c["connected_by_email"] == "jj@dimagi.com"
    assert "token" not in str(c).lower()


def test_list_reports_unconfigured_provider(client, settings):
    settings.SALESFORCE_CLIENT_ID = ""
    assert client.get("/api/connections/").json()["providers"][0]["configured"] is False


def test_delete_mine_revokes_and_removes(client, user):
    conn = _stored({"user": user}, user)
    with FakeSF() as fake:
        resp = client.delete(f"/api/connections/{conn.pk}")
    assert resp.status_code == 204
    assert not Connection.objects.filter(pk=conn.pk).exists()
    assert [c[1] for c in fake.calls] == [REVOKE_URL]


def test_delete_someone_elses_is_404(client, other):
    theirs = _stored({"user": other}, other)
    with FakeSF():
        assert client.delete(f"/api/connections/{theirs.pk}").status_code == 404
    assert Connection.objects.filter(pk=theirs.pk).exists()


def test_delete_an_agents_connection_through_the_person_route_is_404(client, user, eva):
    conn = _stored({"agent": eva}, user)
    with FakeSF():
        assert client.delete(f"/api/connections/{conn.pk}").status_code == 404
    assert Connection.objects.filter(pk=conn.pk).exists()


# ---- an agent's ------------------------------------------------------------------

def test_agent_list_has_own_and_granted(client, user, eva, echo):
    own = _stored({"agent": echo}, user)
    shared = Connection.objects.create(
        provider="salesforce", account_id="00Dorg/005eva", account_label="eva@dimagi-ai.com",
        agent=eva, connected_by=user)
    ConnectionGrant.objects.create(connection=shared, agent=echo, granted_by=user)
    rows = client.get("/api/agents/echo/connections").json()["connections"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[own.pk]["granted_from"] is None
    assert by_id[shared.pk]["granted_from"] == "eva"
    # The owner sees it as its own, not as granted-from.
    eva_rows = client.get("/api/agents/eva/connections").json()["connections"]
    assert [(r["id"], r["granted_from"]) for r in eva_rows] == [(shared.pk, None)]


def test_agent_routes_need_admin(user, other, ws, eva):
    WorkspaceMembership.objects.create(workspace=ws, user=other, role=WorkspaceMembership.EDITOR)
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(other)
    grant_step_up(c)
    assert c.get("/api/agents/eva/connections").status_code in (403, 404)
    with FakeSF():
        assert c.delete(f"/api/agents/eva/connections/{conn.pk}").status_code in (403, 404)
    assert Connection.objects.filter(pk=conn.pk).exists()


def test_agent_disconnect_needs_step_up_in_a_browser(user, eva):
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(user)  # no step-up
    with FakeSF():
        resp = c.delete(f"/api/agents/eva/connections/{conn.pk}")
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"
    assert Connection.objects.filter(pk=conn.pk).exists()


def test_agent_disconnect_with_step_up(client, user, eva):
    conn = _stored({"agent": eva}, user)
    with FakeSF():
        assert client.delete(f"/api/agents/eva/connections/{conn.pk}").status_code == 204
    assert not Connection.objects.filter(pk=conn.pk).exists()


def test_agent_disconnect_with_a_pat_needs_no_step_up(user, eva):
    conn = _stored({"agent": eva}, user)
    with FakeSF():
        resp = Client().delete(f"/api/agents/eva/connections/{conn.pk}", **_pat(user))
    assert resp.status_code == 204


def test_agent_disconnect_of_another_agents_connection_is_404(client, user, eva, echo):
    conn = _stored({"agent": echo}, user)
    with FakeSF():
        assert client.delete(f"/api/agents/eva/connections/{conn.pk}").status_code == 404
    assert Connection.objects.filter(pk=conn.pk).exists()


# ---- grants ----------------------------------------------------------------------

def test_grant_and_revoke(client, user, eva, echo):
    conn = _stored({"agent": eva}, user)
    url = f"/api/agents/eva/connections/{conn.pk}/grants/echo"
    assert client.put(url).status_code == 204
    assert client.put(url).status_code == 204  # idempotent
    assert ConnectionGrant.objects.filter(connection=conn, agent=echo).count() == 1
    assert client.delete(url).status_code == 204
    assert not ConnectionGrant.objects.exists()
    assert client.delete(url).status_code == 204  # already gone


def test_grant_needs_admin_of_both(user, other, ws, eva):
    WorkspaceMembership.objects.create(workspace=ws, user=other, role=WorkspaceMembership.EDITOR)
    Agent.objects.create(slug="theirs", name="Theirs", workspace=ws, owner=other)
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(user)
    grant_step_up(c)
    resp = c.put(f"/api/agents/eva/connections/{conn.pk}/grants/theirs")
    assert resp.status_code == 403
    assert not ConnectionGrant.objects.exists()


def test_grant_to_unknown_agent_and_to_self(client, user, eva):
    conn = _stored({"agent": eva}, user)
    assert client.put(f"/api/agents/eva/connections/{conn.pk}/grants/nobody").status_code == 404
    resp = client.put(f"/api/agents/eva/connections/{conn.pk}/grants/eva")
    assert resp.status_code == 409 and resp.json()["error"] == "own_connection"


def test_grant_needs_step_up_in_a_browser(user, eva, echo):
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(user)
    resp = c.put(f"/api/agents/eva/connections/{conn.pk}/grants/echo")
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"


# ---- my CLI token ------------------------------------------------------------------

def _token(user, provider="salesforce", **kw):
    return Client().post(f"/api/me/connections/{provider}/token", **_pat(user), **kw)


def test_token_route_refuses_a_browser_session(client, user):
    _stored({"user": user}, user)
    resp = client.post("/api/me/connections/salesforce/token")
    assert resp.status_code == 403 and resp.json()["error"] == "token_route_needs_pat"
    assert "00D!cached" not in resp.content.decode()


def test_token_with_a_pat_returns_the_token(user):
    _stored({"user": user}, user)
    resp = _token(user)
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "00D!cached"
    assert body["instance_url"] == "https://dimagi.my.salesforce.com"
    assert body["account_label"] == "jj@dimagi.com" and body["expires_at"]


def test_token_not_connected_is_409_with_connect_url(user):
    resp = _token(user)
    assert resp.status_code == 409
    assert resp.json()["error"] == "not_connected"
    assert resp.json()["connect_url"] == "/auth/connections/salesforce/start/"


def test_token_never_falls_back_to_an_agents_connection(user, eva):
    _stored({"agent": eva}, user)
    assert _token(user).json()["error"] == "not_connected"


def test_token_reconnect_required_is_409_and_stamps(user):
    conn = _stored({"user": user}, user, access="")
    with FakeSF(token=[_resp(400, {"error": "invalid_grant", "error_description": "expired"})]):
        resp = _token(user)
    assert resp.status_code == 409
    assert resp.json()["error"] == "reconnect_required"
    assert resp.json()["connect_url"] == "/auth/connections/salesforce/start/"
    conn.refresh_from_db()
    assert conn.refresh_failed_at is not None  # the stamp survived the 409


def test_token_ambiguous_lists_accounts(user):
    _stored({"user": user}, user)
    Connection.objects.create(provider="salesforce", account_id="00Dother/005x",
                              account_label="second@dimagi.com", user=user, connected_by=user)
    resp = _token(user)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "ambiguous"
    assert sorted(body["accounts"]) == ["jj@dimagi.com", "second@dimagi.com"]


@pytest.mark.parametrize("failure,status", [("unavailable", 503), ("rejected", 502)])
def test_token_provider_failures(user, failure, status):
    import requests
    _stored({"user": user}, user, access="")
    if failure == "unavailable":
        fake = FakeSF(token=[requests.ConnectionError("down")])
    else:
        fake = FakeSF(token=[_resp(400, {"error": "invalid_client"})])
    with fake:
        assert _token(user).status_code == status


def test_token_unknown_provider_is_404(user):
    assert _token(user, "nope").status_code == 404


def test_token_issue_is_logged_without_the_token(user, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    _stored({"user": user}, user)
    _token(user)
    lines = [r.getMessage() for r in caplog.records if "connections.token_issued" in r.getMessage()]
    assert len(lines) == 1 and "owner=jj@dimagi.com" in lines[0]
    assert "00D!cached" not in caplog.text
    assert not Event.objects.filter(kind="connections.token_issued").exists()


def test_refreshed_token_is_returned_and_rotated(user):
    conn = _stored({"user": user}, user, access="")
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!fresh", refresh_token="5Aep_new"))]):
        assert _token(user).json()["access_token"] == "00D!fresh"
    conn.refresh_from_db()
    assert conn.refresh_failed_at is None


def test_token_unconfigured_provider_is_409_even_with_a_stale_cached_token(user, settings):
    conn = _stored({"user": user}, user, access="00D!stale", expires_in=timedelta(seconds=-60))
    settings.SALESFORCE_CLIENT_ID = ""
    resp = _token(user)
    assert resp.status_code == 409 and resp.json()["error"] == "not_configured"
    assert "00D!stale" not in resp.content.decode()
    assert Connection.objects.filter(pk=conn.pk).exists()


# ---- a junk Authorization header is not a token -----------------------------------

JUNK = {"HTTP_AUTHORIZATION": "Bearer junk"}


def test_junk_bearer_with_a_cookie_is_still_a_browser_on_the_token_route(client, user):
    _stored({"user": user}, user)
    resp = client.post("/api/me/connections/salesforce/token", **JUNK)
    assert resp.status_code == 403 and resp.json()["error"] == "token_route_needs_pat"


def test_junk_bearer_with_a_cookie_still_needs_step_up(user, eva):
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(user)  # no step-up
    with FakeSF():
        resp = c.delete(f"/api/agents/eva/connections/{conn.pk}", **JUNK)
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"
    assert Connection.objects.filter(pk=conn.pk).exists()


def test_junk_bearer_on_credentials_route_still_needs_step_up(user, eva):
    c = Client()
    c.force_login(user)
    resp = c.put("/api/agents/eva/credentials", data={"values": {"x": "y"}},
                 content_type="application/json", **JUNK)
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"


def test_a_real_pat_is_a_machine(user):
    from django.test import RequestFactory

    from apps.common.views_debug import is_machine
    req = RequestFactory().get("/", **_pat(user))
    assert is_machine(req) is False  # header alone (no middleware) proves nothing
    req.auth_method = "pat"
    assert is_machine(req) is True


# ---- revoking from the grantee's side ------------------------------------------------

def test_grantee_admin_may_revoke_but_a_stranger_may_not(user, other, ws, eva):
    WorkspaceMembership.objects.create(workspace=ws, user=other, role=WorkspaceMembership.EDITOR)
    stranger = _mk("stranger")
    WorkspaceMembership.objects.create(workspace=ws, user=stranger, role=WorkspaceMembership.EDITOR)
    theirs = Agent.objects.create(slug="theirs", name="Theirs", workspace=ws, owner=other)
    conn = _stored({"agent": eva}, user)
    ConnectionGrant.objects.create(connection=conn, agent=theirs, granted_by=user)
    url = f"/api/agents/eva/connections/{conn.pk}/grants/theirs"

    s = Client()
    s.force_login(stranger)
    grant_step_up(s)
    assert s.delete(url).status_code == 403
    assert ConnectionGrant.objects.exists()

    o = Client()
    o.force_login(other)
    grant_step_up(o)
    assert o.delete(url).status_code == 204
    assert not ConnectionGrant.objects.exists()

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
from tests.fixtures.connections import REVOKE_URL, FakeSF, _resp, _stored, _token_body, home, sf_app

pytestmark = pytest.mark.django_db


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
    assert body["providers"] == [{"name": "salesforce", "title": "Salesforce", "workspace": "jj-home",
                                  "workspace_name": "jj", "configured": True},
                                 {"name": "google", "title": "Google", "workspace": "",
                                  "workspace_name": "", "configured": False}]
    assert [c["id"] for c in body["connections"]] == [mine.pk]
    c = body["connections"][0]
    assert c["provider"] == "salesforce" and c["account_label"] == "jj@dimagi.com"
    assert c["needs_reconnect"] is False and c["connected_by_email"] == "jj@dimagi.com"
    assert "token" not in str(c).lower()


def test_list_reports_unconfigured_provider(client, user):
    assert client.get("/api/connections/").json()["providers"][0]["configured"] is False   # no app anywhere
    app = sf_app(home(user))
    assert client.get("/api/connections/").json()["providers"][0]["configured"] is True
    app.client_id = ""
    app.save()
    assert client.get("/api/connections/").json()["providers"][0]["configured"] is False


def test_agent_list_reports_its_workspaces_app(client, eva):
    assert client.get("/api/agents/eva/connections").json()["providers"][0]["configured"] is False
    sf_app(eva.workspace)
    assert client.get("/api/agents/eva/connections").json()["providers"][0]["configured"] is True


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
        agent=eva, connected_by=user, app=sf_app(eva.workspace))
    ConnectionGrant.objects.create(connection=shared, agent=echo, granted_by=user)
    rows = client.get("/api/agents/echo/connections").json()["connections"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[own.pk]["granted_from"] is None
    assert by_id[shared.pk]["granted_from"] == "eva"
    # The owner sees it as its own, not as granted-from.
    eva_rows = client.get("/api/agents/eva/connections").json()["connections"]
    assert [(r["id"], r["granted_from"]) for r in eva_rows] == [(shared.pk, None)]


def test_owned_row_lists_its_grantees_until_revoked(client, user, eva, echo):
    conn = _stored({"agent": eva}, user)
    url = f"/api/agents/eva/connections/{conn.pk}/grants/echo"
    assert client.get("/api/agents/eva/connections").json()["connections"][0]["shared_with"] == []
    assert client.put(url).status_code == 204
    row = client.get("/api/agents/eva/connections").json()["connections"][0]
    assert row["shared_with"] == ["echo"]
    # The grantee's own view of it is a shared row: no list of grantees there.
    assert client.get("/api/agents/echo/connections").json()["connections"][0]["shared_with"] == []
    assert client.delete(url).status_code == 204
    assert client.get("/api/agents/eva/connections").json()["connections"][0]["shared_with"] == []


def test_agent_routes_need_admin(user, other, ws, eva):
    WorkspaceMembership.objects.create(workspace=ws, user=other, role=WorkspaceMembership.EDITOR)
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(other)
    grant_step_up(c)
    assert c.get("/api/agents/eva/connections").status_code == 403
    with FakeSF():
        assert c.delete(f"/api/agents/eva/connections/{conn.pk}").status_code == 403
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
    sf_app(home(user))
    resp = _token(user)
    assert resp.status_code == 409
    assert resp.json()["error"] == "not_connected"
    assert resp.json()["connect_url"] == "http://testserver/auth/connections/salesforce/jj-home/start/"


def test_token_never_falls_back_to_an_agents_connection(user, eva):
    _stored({"agent": eva}, user)
    assert _token(user).json()["error"] == "not_connected"


def test_token_reconnect_required_is_409_and_stamps(user):
    conn = _stored({"user": user}, user, access="")
    with FakeSF(token=[_resp(400, {"error": "invalid_grant", "error_description": "expired"})]):
        resp = _token(user)
    assert resp.status_code == 409
    assert resp.json()["error"] == "reconnect_required"
    assert resp.json()["connect_url"] == "http://testserver/auth/connections/salesforce/jj-home/start/"
    conn.refresh_from_db()
    assert conn.refresh_failed_at is not None  # the stamp survived the 409


def test_token_ambiguous_lists_accounts(user):
    first = _stored({"user": user}, user)
    Connection.objects.create(provider="salesforce", account_id="00Dother/005x",
                              account_label="second@dimagi.com", user=user, connected_by=user, app=first.app)
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


def test_token_refresh_query_forces_a_new_token(user):
    _stored({"user": user}, user)
    fake = FakeSF(token=[_resp(body=_token_body(access_token="00D!forced"))])
    with fake:
        assert _token(user).json()["access_token"] == "00D!cached"
        assert fake.calls == []
        resp = Client().post("/api/me/connections/salesforce/token?refresh=true", **_pat(user))
    assert resp.status_code == 200 and resp.json()["access_token"] == "00D!forced"
    assert len(fake.calls) == 1


def test_token_unknown_provider_is_404(user):
    assert _token(user, "nope").status_code == 404


def test_token_issue_is_logged_without_the_token(user, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    _stored({"user": user}, user)
    _token(user)
    lines = [r.getMessage() for r in caplog.records if "connections.token_issued" in r.getMessage()]
    assert len(lines) == 1 and f"owner=user:{user.pk}" in lines[0] and "to=cli" in lines[0]
    assert "00D!cached" not in caplog.text
    assert not Event.objects.filter(kind="connections.token_issued").exists()


def test_refreshed_token_is_returned_and_rotated(user):
    conn = _stored({"user": user}, user, access="")
    with FakeSF(token=[_resp(body=_token_body(access_token="00D!fresh", refresh_token="5Aep_new"))]):
        assert _token(user).json()["access_token"] == "00D!fresh"
    conn.refresh_from_db()
    assert conn.refresh_failed_at is None


def test_token_unconfigured_provider_is_409_even_with_a_stale_cached_token(user):
    conn = _stored({"user": user}, user, access="00D!stale", expires_in=timedelta(seconds=-60))
    conn.app.client_id = ""
    conn.app.save()
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


def test_cookie_plus_a_valid_pat_is_a_browser_and_needs_step_up(user, eva):
    """Pinned behaviour after the is_machine fix (I6): when a session cookie
    already signed the request in, the bearer door does not even look the PAT up
    (`auth_method` stays unset), so the request is a BROWSER — a step-up route
    asks for a passkey rather than letting the PAT skip it."""
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(user)  # no step-up
    with FakeSF():
        resp = c.delete(f"/api/agents/eva/connections/{conn.pk}", **_pat(user))
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"
    assert Connection.objects.filter(pk=conn.pk).exists()


def test_embed_cookie_plus_delegated_token_is_a_browser_and_needs_step_up(user, eva):
    """Pinned behaviour after the is_machine fix (I6), for the embed path: canopy
    embedding its own widget is same-origin, so the frame's XHRs carry the session
    cookie AND the delegated token. Identity (and `auth_method`) stay with the
    session, so the request is a browser and a step-up route still asks."""
    from apps.tokens.models import AppCredential, DelegatedToken
    from tests.site_tenant import host_workspace

    app = AppCredential.create_credential(name="canopy-web", created_by=user, workspace=host_workspace())
    raw, _ = DelegatedToken.issue(app=app, user=user, ttl_seconds=600)
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(user)  # no step-up
    with FakeSF():
        resp = c.delete(f"/api/agents/eva/connections/{conn.pk}", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"
    assert Connection.objects.filter(pk=conn.pk).exists()


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


# ---- only a plain PAT over REST reaches the CLI token route (C1) ---------------------

def test_token_route_refuses_an_oauth_connector_token(user):
    from apps.oauth_server.models import OAuthClient, OAuthGrant

    _stored({"user": user}, user)
    oc = OAuthClient.objects.create(client_id="c-1", client_name="Claude", redirect_uris=[])
    grant = OAuthGrant.objects.create(client=oc, user=user)
    raw, _ = PersonalToken.create_for_user(user=user, label="oauth", ttl=timedelta(hours=1),
                                           oauth_grant=grant)
    resp = Client().post("/api/me/connections/salesforce/token", HTTP_AUTHORIZATION=f"Bearer {raw}")
    assert resp.status_code == 403 and resp.json()["error"] == "token_route_needs_pat"
    assert "00D!cached" not in resp.content.decode()


def test_token_route_refuses_a_caller_token(user, ws):
    import uuid

    from apps.canopy_sessions.models import Session
    from apps.harness import caller_tokens
    from apps.harness import initiator as who
    from apps.harness.models import Turn

    _stored({"user": user}, user)
    session = Session.objects.create(workspace=ws, project="p", created_by=user, title="chat")
    turn = Turn.objects.create(chat_session=session, origin=Turn.ORIGIN_CANOPY_WEB_CHAT, prompt="x",
                               idempotency_key=uuid.uuid4().hex, initiator_kind=who.USER,
                               initiator_user=user, initiator_assurance=who.SESSION)
    # A scoped token reaches REST only on MCP's tool routes (403 at the door); an
    # unscoped one does not reach REST at all (401).
    for scoped, status in ((True, 403), (False, 401)):
        raw = caller_tokens.mint(turn, scoped=scoped)
        resp = Client().post("/api/me/connections/salesforce/token", HTTP_AUTHORIZATION=f"Bearer {raw}")
        assert resp.status_code == status, (scoped, resp.content)
        assert "00D!cached" not in resp.content.decode()


def test_token_route_refuses_an_mcp_call_even_with_a_pat(user):
    """The route itself, not only the tool list: a request the MCP transport
    dispatched (`via_mcp`) runs as the caller's PAT and is still refused."""
    from django.test import RequestFactory

    from apps.api.errors import ProblemError
    from apps.connections import api as conn_api
    from apps.tokens import kinds

    _stored({"user": user}, user)
    req = RequestFactory().post("/api/me/connections/salesforce/token")
    req.user, req.auth_method, req.via_mcp = user, kinds.PAT, True
    req.auth_credential = {"type": kinds.PAT, "id": 1}
    with pytest.raises(ProblemError) as exc:
        conn_api.my_connection_token(req, "salesforce")
    assert exc.value.status_code == 403


def test_token_route_is_not_an_mcp_tool():
    from apps.mcp import api_tools

    assert "my_connection_token" in api_tools.EXCLUDED
    assert "turn_connection_token" in api_tools.EXCLUDED
    assert not api_tools.is_tool_route("POST", "/api/me/connections/salesforce/token")


def test_grantee_admin_in_another_workspace_may_stop_using_it(user, other, ws, eva):
    """T4: the grantee's admin cannot read the owner agent (another workspace),
    and must still be able to revoke ("Stop using") — and only that grant."""
    theirs_ws = Workspace.objects.create(slug="theirs-ws", display_name="T", created_by=other)
    WorkspaceMembership.objects.create(workspace=theirs_ws, user=other, role=WorkspaceMembership.EDITOR)
    theirs = Agent.objects.create(slug="theirs", name="Theirs", workspace=theirs_ws, owner=other)
    shared = _stored({"agent": eva}, user)
    ConnectionGrant.objects.create(connection=shared, agent=theirs, granted_by=user)
    unshared = Connection.objects.create(
        provider="salesforce", account_id="00Dorg/005other", account_label="x", agent=eva,
        refresh_token_enc="x", connected_by=user, app=shared.app)
    o = Client()
    o.force_login(other)
    grant_step_up(o)
    assert o.get("/api/agents/eva/connections").status_code == 404  # cannot even read the owner
    assert o.delete(f"/api/agents/eva/connections/{unshared.pk}/grants/theirs").status_code == 404
    assert o.delete(f"/api/agents/eva/connections/{shared.pk}/grants/theirs").status_code == 204
    assert not ConnectionGrant.objects.exists()


def test_deleting_the_person_who_connected_keeps_the_agents_connection(client, user, eva, echo):
    """M2: an agent's Salesforce identity outlives the admin who attached it
    (and the one who shared it)."""
    admin = _mk("former-admin")
    conn = _stored({"agent": eva}, admin)
    grant = ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=admin)
    admin.delete()
    conn.refresh_from_db()
    grant.refresh_from_db()
    assert conn.connected_by_id is None and grant.granted_by_id is None
    rows = client.get("/api/agents/eva/connections").json()["connections"]
    assert [(r["id"], r["connected_by_email"], r["shared_with"]) for r in rows] == [(conn.pk, "", ["echo"])]


def test_rows_carry_their_scopes(client, user, eva):
    """M3: what the grant allows is visible, on both lists."""
    for kw in ({"user": user}, {"agent": eva}):
        c = _stored(kw, user)
        c.scopes = "api refresh_token"
        c.save()
    mine = client.get("/api/connections/").json()["connections"]
    agents = client.get("/api/agents/eva/connections").json()["connections"]
    assert mine[0]["scopes"] == ["api", "refresh_token"] == agents[0]["scopes"]


# ---- final review: usable, missing scopes, grants recorded on disconnect -------------

def test_a_connection_in_a_workspace_i_left_is_listed_as_not_usable(client, user, ws):
    """M4: the CLI says `not_connected` for it, so the list must not look healthy."""
    here = _stored({"user": user}, user, app=sf_app(ws))
    gone_ws = Workspace.objects.create(slug="gone", display_name="Gone", created_by=user)
    WorkspaceMembership.objects.create(workspace=gone_ws, user=user, role=WorkspaceMembership.EDITOR)
    left = _stored({"user": user}, user, app=sf_app(gone_ws), account_id="00Dorg/005b")
    WorkspaceMembership.objects.filter(workspace=gone_ws, user=user).delete()
    rows = {r["id"]: r for r in client.get("/api/connections/").json()["connections"]}
    assert rows[here.pk]["usable"] is True
    assert rows[left.pk]["usable"] is False and rows[left.pk]["workspace_name"] == "Gone"


def test_an_agents_connection_from_before_a_move_is_listed_as_not_usable(client, user, ws, eva, echo):
    other_ws = Workspace.objects.create(slug="x", display_name="X", created_by=user)
    WorkspaceMembership.objects.create(workspace=other_ws, user=user, role=WorkspaceMembership.EDITOR)
    old = _stored({"agent": eva}, user, app=sf_app(ws))
    ConnectionGrant.objects.create(connection=old, agent=echo, granted_by=user)
    Agent.objects.filter(pk=eva.pk).update(workspace=other_ws)
    eva_rows = client.get("/api/agents/eva/connections").json()["connections"]
    assert [(r["id"], r["workspace"], r["usable"]) for r in eva_rows] == [(old.pk, "w", False)]
    echo_rows = client.get("/api/agents/echo/connections").json()["connections"]
    assert [(r["granted_from"], r["usable"]) for r in echo_rows] == [("eva", False)]


def test_a_scope_added_to_the_app_shows_on_existing_connections(client, user, ws, eva):
    """M5: the admin ticks "Send mail"; existing grants lack it until reconnected."""
    app = sf_app(ws, provider="google", allowed_domains="")
    send, read = ("https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly")
    for kw in ({"user": user}, {"agent": eva}):
        _stored(kw, user, app=app, scopes=f"openid https://www.googleapis.com/auth/userinfo.email {read}")
    app.scopes = f"{read} {send}"
    app.save()
    want = [{"scope": send, "label": "Send mail"}]
    assert client.get("/api/connections/").json()["connections"][0]["missing_scopes"] == want
    assert client.get("/api/agents/eva/connections").json()["connections"][0]["missing_scopes"] == want
    app.scopes = read
    app.save()
    assert client.get("/api/connections/").json()["connections"][0]["missing_scopes"] == []


def test_disconnecting_a_shared_connection_records_each_grant_it_takes(client, user, ws, eva, echo):
    """M10: the grantee's workspace learns why its turns now get `not_connected`."""
    other_ws = Workspace.objects.create(slug="x", display_name="X", created_by=user)
    hal = Agent.objects.create(slug="hal", name="Hal", workspace=other_ws, owner=user)
    conn = _stored({"agent": eva}, user)
    ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=user)
    ConnectionGrant.objects.create(connection=conn, agent=hal, granted_by=user)
    with FakeSF():
        assert client.delete(f"/api/agents/eva/connections/{conn.pk}").status_code == 204
    revoked = Event.objects.filter(kind="connections.grant_revoked").order_by("id")
    assert [(e.workspace_id, e.payload["grantee"], e.payload["by"], e.payload["during"]) for e in revoked] == [
        ("w", "echo", "jj@dimagi.com", "disconnected"),
        ("w", "hal", "jj@dimagi.com", "disconnected"),
        ("x", "hal", "jj@dimagi.com", "disconnected"),      # hal lives elsewhere: its workspace hears too
    ]

"""The JSON routes of connections: my status/disconnect, an agent's.

The service layer is pinned in `test_connections_services.py`; this pins what
only the routes decide: whose rows a caller can see or delete, and who may
administer an agent's connection (with a passkey step-up from a browser).
"""
from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from apps.agents.models import Agent
from apps.connections.models import Connection, ConnectionGrant
from apps.events.models import Event
from apps.passkeys.testing import grant_step_up
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import FakeSF, _stored, home, sf_app
from tests.token_helpers import pat_headers

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


# ---- list / disconnect (mine) ---------------------------------------------------

def test_list_shows_providers_and_only_my_connections(client, user, other):
    mine = _stored({"user": user}, user)
    _stored({"user": other}, other)
    body = client.get("/api/connections/").json()
    assert body["providers"] == [{"name": "salesforce", "title": "Salesforce", "workspace": "jj-home",
                                  "workspace_name": "jj", "configured": True},
                                 {"name": "google", "title": "Google", "workspace": "",
                                  "workspace_name": "", "configured": False}]
    assert [c["id"] for c in body["connections"]] == [str(mine.pk)]
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


def test_delete_mine_removes_and_calls_no_provider(client, user):
    conn = _stored({"user": user}, user)
    cid = conn.pk
    with FakeSF() as fake:
        resp = client.delete(f"/api/connections/{cid}")
    assert resp.status_code == 200 and resp.json() == {"outcome": "deleted", "machine_revokes": False}
    assert not Connection.objects.filter(pk=cid).exists()
    assert fake.calls == []


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
    own = _stored({"agent": echo}, user, statement_version=1)
    shared = Connection.objects.create(
        provider="salesforce", account_id="00Dorg/005eva", account_label="eva@dimagi-ai.com",
        agent=eva, connected_by=user, app=sf_app(eva.workspace), statement_version=1)
    ConnectionGrant.objects.create(connection=shared, agent=echo, granted_by=user)
    rows = client.get("/api/agents/echo/connections").json()["connections"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[str(own.pk)]["granted_from"] is None
    assert by_id[str(shared.pk)]["granted_from"] == "eva"
    # The owner sees it as its own, not as granted-from.
    eva_rows = client.get("/api/agents/eva/connections").json()["connections"]
    assert [(r["id"], r["granted_from"]) for r in eva_rows] == [(str(shared.pk), None)]


def test_owned_row_lists_its_grantees_until_revoked(client, user, eva, echo):
    conn = _stored({"agent": eva}, user, statement_version=1)
    assert client.get("/api/agents/eva/connections").json()["connections"][0]["shared_with"] == []
    grant = ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=user)
    row = client.get("/api/agents/eva/connections").json()["connections"][0]
    assert row["shared_with"] == ["echo"]
    # The grantee's own view of it is a shared row: no list of grantees there.
    assert client.get("/api/agents/echo/connections").json()["connections"][0]["shared_with"] == []
    grant.delete()
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
        assert client.delete(f"/api/agents/eva/connections/{conn.pk}").status_code == 200
    assert not Connection.objects.filter(pk=conn.pk).exists()


def test_agent_disconnect_with_a_pat_needs_no_step_up(user, eva):
    conn = _stored({"agent": eva}, user)
    with FakeSF():
        resp = Client().delete(f"/api/agents/eva/connections/{conn.pk}", **pat_headers(user))
    assert resp.status_code == 200


def test_agent_disconnect_of_another_agents_connection_is_404(client, user, eva, echo):
    conn = _stored({"agent": echo}, user)
    with FakeSF():
        assert client.delete(f"/api/agents/eva/connections/{conn.pk}").status_code == 404
    assert Connection.objects.filter(pk=conn.pk).exists()


# ---- a browser or a machine ------------------------------------------------------

JUNK = {"HTTP_AUTHORIZATION": "Bearer junk"}


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
    already signed the request in, the bearer path does not even look the PAT up
    (`auth_method` stays unset), so the request is a BROWSER — a step-up route
    asks for a passkey rather than letting the PAT skip it."""
    conn = _stored({"agent": eva}, user)
    c = Client()
    c.force_login(user)  # no step-up
    with FakeSF():
        resp = c.delete(f"/api/agents/eva/connections/{conn.pk}", **pat_headers(user))
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
    req = RequestFactory().get("/", **pat_headers(user))
    assert is_machine(req) is False  # header alone (no middleware) proves nothing
    req.auth_method = "pat"
    assert is_machine(req) is True


def test_deleting_the_person_who_connected_keeps_the_agents_connection(client, user, eva, echo):
    """M2: an agent's Salesforce identity outlives the admin who attached it
    (and the one who shared it)."""
    admin = _mk("former-admin")
    conn = _stored({"agent": eva}, admin, statement_version=1)
    grant = ConnectionGrant.objects.create(connection=conn, agent=echo, granted_by=admin)
    admin.delete()
    conn.refresh_from_db()
    grant.refresh_from_db()
    assert conn.connected_by_id is None and grant.granted_by_id is None
    rows = client.get("/api/agents/eva/connections").json()["connections"]
    assert [(r["id"], r["connected_by_email"], r["shared_with"]) for r in rows] == [(str(conn.pk), "", ["echo"])]


def test_rows_carry_their_scopes(client, user, eva):
    """M3: what the grant allows is visible, on both lists."""
    for kw in ({"user": user}, {"agent": eva}):
        c = _stored(kw, user, statement_version=1)
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
    assert rows[str(here.pk)]["usable"] is True
    assert rows[str(left.pk)]["usable"] is False and rows[str(left.pk)]["workspace_name"] == "Gone"


def test_an_agents_connection_from_before_a_move_is_listed_as_not_usable(client, user, ws, eva, echo):
    other_ws = Workspace.objects.create(slug="x", display_name="X", created_by=user)
    WorkspaceMembership.objects.create(workspace=other_ws, user=user, role=WorkspaceMembership.EDITOR)
    old = _stored({"agent": eva}, user, app=sf_app(ws), statement_version=1)
    ConnectionGrant.objects.create(connection=old, agent=echo, granted_by=user)
    Agent.objects.filter(pk=eva.pk).update(workspace=other_ws)
    eva_rows = client.get("/api/agents/eva/connections").json()["connections"]
    assert [(r["id"], r["workspace"], r["usable"]) for r in eva_rows] == [(str(old.pk), "w", False)]
    echo_rows = client.get("/api/agents/echo/connections").json()["connections"]
    assert [(r["granted_from"], r["usable"]) for r in echo_rows] == [("eva", False)]


def test_a_scope_added_to_the_app_shows_on_existing_connections(client, user, ws, eva):
    """M5: the admin ticks "Send mail"; existing grants lack it until reconnected."""
    app = sf_app(ws, provider="google", allowed_domains="")
    send, read = ("https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly")
    for kw in ({"user": user}, {"agent": eva}):
        _stored(kw, user, app=app, statement_version=1,
                scopes=f"openid https://www.googleapis.com/auth/userinfo.email {read}")
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
        assert client.delete(f"/api/agents/eva/connections/{conn.pk}").status_code == 200
    revoked = Event.objects.filter(kind="connections.grant_revoked").order_by("id")
    assert [(e.workspace_id, e.payload["grantee"], e.payload["by"], e.payload["during"]) for e in revoked] == [
        ("w", "echo", "jj@dimagi.com", "disconnected"),
        ("w", "hal", "jj@dimagi.com", "disconnected"),
        ("x", "hal", "jj@dimagi.com", "disconnected"),      # hal lives elsewhere: its workspace hears too
    ]

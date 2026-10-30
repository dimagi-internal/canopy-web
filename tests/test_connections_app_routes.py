"""The per-workspace routes of connections, over HTTP: a workspace's app admin API
(`/api/workspaces/{workspace}/connection-apps`), the per-workspace browser legs
(`/auth/connections/<provider>/<workspace>/start|callback/`), and how listings and
the CLI token route answer once a person can be in several workspaces.

The service layer is pinned in `test_connections_apps.py`; this pins what only
the routes decide: who may read or write an app (workspace admins; a browser
needs a fresh step-up for a write), that the secret never comes back, which
workspace a start or a callback is for, and the 409 bodies the CLI branches on.
"""
from __future__ import annotations

import json
import logging
from urllib.parse import parse_qs, urlparse

import pytest
from django.contrib.auth import get_user_model
from django.forms.models import model_to_dict
from django.test import Client
from django.urls import reverse

from apps.agents.models import Agent
from apps.connections.models import Connection, ConnectionApp
from apps.events.models import Event, SettingChange
from apps.passkeys.testing import grant_step_up
from apps.tokens.models import PersonalToken
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connections import (
    CLIENT_SECRET,
    REVOKE_URL,
    TOKEN_URL,
    FakeSF,
    _resp,
    _stored,
    _token_body,
    sf_app,
)

pytestmark = pytest.mark.django_db

M = WorkspaceMembership
APPS = "/api/workspaces/{}/connection-apps"


def _mk(username):
    return get_user_model().objects.create_user(username=username, email=f"{username}@dimagi.com")


def _ws(slug, name, **members):
    w = Workspace.objects.create(slug=slug, display_name=name, created_by=next(iter(members.values()))[0])
    for role, users in members.items():
        for u in users:
            M.objects.create(workspace=w, user=u, role=getattr(M, role.upper()))
    return w


@pytest.fixture
def admin():
    return _mk("ad")


@pytest.fixture
def editor():
    return _mk("ed")


@pytest.fixture
def ws(admin, editor):
    return _ws("w", "Dimagi", admin=[admin], editor=[editor])


def _browser(user, *, step_up=True):
    c = Client()
    c.force_login(user)
    if step_up:
        grant_step_up(c)
    return c


def _pat(user) -> dict:
    raw, _ = PersonalToken.create_for_user(user=user, label="cli")
    return {"HTTP_AUTHORIZATION": f"Bearer {raw}"}


def _put(client, slug="w", provider="salesforce", **body):
    return client.put(f"{APPS.format(slug)}/{provider}", body, content_type="application/json")


def _row(body, provider="salesforce"):
    return next(a for a in body["apps"] if a["provider"] == provider)


# ---- the app admin API ------------------------------------------------------------

def test_list_shows_every_provider_set_up_or_not(admin, ws):
    body = _browser(admin).get(APPS.format("w")).json()
    row = _row(body)
    assert row == {
        "provider": "salesforce", "title": "Salesforce", "configured": False, "client_id": "",
        "has_secret": False, "options": {}, "scopes": [], "scope_choices": [],
        "option_fields": [
            {"name": "login_url", "label": row["option_fields"][0]["label"], "kind": "url", "required": True,
             "default": "https://login.salesforce.com"},
            {"name": "access_token_seconds", "label": row["option_fields"][1]["label"], "kind": "int",
             "required": False, "default": 1800},
        ],
        "callback_url": "http://testserver/auth/connections/salesforce/w/callback/",
        "connection_count": 0, "updated_at": None, "configured_by_email": "",
    }


def test_put_creates_then_lists_without_the_secret(admin, ws):
    c = _browser(admin)
    resp = _put(c, client_id="3MVG-new", client_secret="s3cret-value",
                options={"login_url": "https://dimagi.my.salesforce.com"})
    assert resp.status_code == 200, resp.content
    out = resp.json()
    assert out["configured"] is True and out["has_secret"] is True and out["client_id"] == "3MVG-new"
    assert out["options"] == {"login_url": "https://dimagi.my.salesforce.com", "access_token_seconds": 1800}
    assert out["configured_by_email"] == "ad@dimagi.com" and out["updated_at"]
    assert "s3cret-value" not in resp.content.decode()
    listed = c.get(APPS.format("w"))
    assert _row(listed.json()) == out
    assert "s3cret-value" not in listed.content.decode()
    assert ConnectionApp.objects.get().client_secret == "s3cret-value"


def test_connection_count_counts_the_apps_connections(admin, ws):
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=admin)
    _stored({"agent": agent}, admin)
    _stored({"user": admin}, admin, app=sf_app(ws))
    assert _row(_browser(admin).get(APPS.format("w")).json())["connection_count"] == 2


def test_editor_is_403_and_non_member_404_on_every_app_route(admin, editor, ws):
    sf_app(ws)
    stranger = _mk("x")
    _ws("theirs", "Theirs", admin=[stranger])
    for user, status in ((editor, 403), (stranger, 404)):
        c = _browser(user)
        assert c.get(APPS.format("w")).status_code == status
        assert _put(c, client_id="other", client_secret="zzz").status_code == status
        with FakeSF() as fake:
            assert c.delete(f"{APPS.format('w')}/salesforce").status_code == status
        assert fake.calls == []
    app = ConnectionApp.objects.get()
    assert app.client_id == "3MVG-client" and app.client_secret == CLIENT_SECRET


def test_a_browser_write_needs_a_fresh_step_up(admin, ws):
    c = _browser(admin, step_up=False)
    resp = _put(c, client_id="3MVG-new", client_secret="s3cret-value",
                options={"login_url": "https://login.salesforce.com"})
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"
    assert not ConnectionApp.objects.exists()
    sf_app(ws)
    with FakeSF():
        resp = c.delete(f"{APPS.format('w')}/salesforce")
    assert resp.status_code == 403 and resp.json().get("error") == "step_up_required"
    assert ConnectionApp.objects.exists()
    assert c.get(APPS.format("w")).status_code == 200                 # reading needs none


def test_a_pat_write_needs_no_step_up(admin, ws):
    resp = Client().put(f"{APPS.format('w')}/salesforce",
                        {"client_id": "3MVG-new", "client_secret": "s3cret-value",
                         "options": {"login_url": "https://login.salesforce.com"}},
                        content_type="application/json", **_pat(admin))
    assert resp.status_code == 200
    assert Client().delete(f"{APPS.format('w')}/salesforce", **_pat(admin)).status_code == 204


def test_unknown_provider_is_404(admin, ws):
    c = _browser(admin)
    assert _put(c, provider="nope", client_id="x", client_secret="y").status_code == 404
    assert c.delete(f"{APPS.format('w')}/nope").status_code == 404


@pytest.mark.parametrize("body", [
    {"client_id": "", "client_secret": "s"},
    {"client_id": "a", "client_secret": ""},                         # first save needs a secret
    {"client_id": "a", "client_secret": "s", "options": {"login_url": "https://evil.example"}},
    {"client_id": "a", "client_secret": "s", "options": {"login_url": "https://login.salesforce.com",
                                                         "nope": 1}},
    {"client_id": "a", "client_secret": "s", "options": {"login_url": "https://login.salesforce.com"},
     "scopes": ["full"]},
])
def test_invalid_settings_are_400_invalid_app(admin, ws, body):
    resp = _put(_browser(admin), **body)
    assert resp.status_code == 400 and resp.json()["error"] == "invalid_app"
    assert resp.json()["detail"]
    assert not ConnectionApp.objects.exists()


def test_rotating_the_secret_carries_existing_connections(admin, ws):
    """Review Focus 2: a new secret with the same client id; connections refresh
    with it. A blank secret keeps the stored one."""
    c = _browser(admin)
    app = sf_app(ws)
    conn = _stored({"user": admin}, admin, app=app, access="")
    resp = _put(c, client_id=app.client_id, client_secret="rotated-secret")
    assert resp.status_code == 200 and resp.json()["has_secret"] is True
    assert _put(c, client_id=app.client_id).status_code == 200     # blank: keeps "rotated-secret"
    with FakeSF() as fake:
        got = Client().post("/api/me/connections/salesforce/token", **_pat(admin))
    assert got.status_code == 200, got.content
    (_, url, data), = fake.token_calls()
    assert url == TOKEN_URL and data["client_secret"] == "rotated-secret" and data["client_id"] == app.client_id
    assert Connection.objects.filter(pk=conn.pk).exists()


def test_the_client_id_is_locked_while_connections_exist(admin, ws):
    """Review Focus 3: changing the client id is 409 `app_locked` while
    connections exist; removing the app (which disconnects them) frees it."""
    c = _browser(admin)
    app = sf_app(ws)
    conn = _stored({"user": admin}, admin, app=app)
    resp = _put(c, client_id="3MVG-other", client_secret="other-secret")
    assert resp.status_code == 409 and resp.json()["error"] == "app_locked"
    app.refresh_from_db()
    assert app.client_id == "3MVG-client" and app.client_secret == CLIENT_SECRET

    with FakeSF() as fake:
        assert c.delete(f"{APPS.format('w')}/salesforce").status_code == 204
    assert [call[1] for call in fake.calls] == [REVOKE_URL]
    assert not Connection.objects.filter(pk=conn.pk).exists() and not ConnectionApp.objects.exists()
    resp = _put(c, client_id="3MVG-other", client_secret="other-secret",
                options={"login_url": "https://login.salesforce.com"})
    assert resp.status_code == 200 and resp.json()["client_id"] == "3MVG-other"


def test_removing_an_app_that_is_not_set_up_is_harmless(admin, ws):
    assert _browser(admin).delete(f"{APPS.format('w')}/salesforce").status_code == 204


def test_put_replaces_scopes_and_merges_options(admin, ws, monkeypatch):
    from apps.connections.providers.salesforce import SalesforceProvider

    monkeypatch.setattr(SalesforceProvider, "scope_choices", {"chatter_api": "Chatter", "web": "Web"})
    c = _browser(admin)
    sf_app(ws, access_token_seconds=900)
    out = _put(c, client_id="3MVG-client", scopes=["web", "chatter_api"]).json()
    assert out["scopes"] == ["chatter_api", "web"]
    assert out["scope_choices"] == [{"scope": "chatter_api", "label": "Chatter"}, {"scope": "web", "label": "Web"}]
    assert out["options"]["access_token_seconds"] == 900                 # merged: omitted, kept
    assert _put(c, client_id="3MVG-client", scopes=["web"]).json()["scopes"] == ["web"]
    assert _put(c, client_id="3MVG-client").json()["scopes"] == []     # replaced in full


def test_app_writes_are_workspace_events_and_setting_changes(admin, ws):
    c = _browser(admin)
    _put(c, client_id="3MVG-new", client_secret="s3cret-value",
         options={"login_url": "https://login.salesforce.com"})
    c.delete(f"{APPS.format('w')}/salesforce")
    kinds = list(Event.objects.filter(workspace=ws, source="connections").order_by("id").values_list("kind", flat=True))
    assert kinds == ["connections.app_configured", "connections.app_removed"]
    assert SettingChange.objects.filter(workspace=ws).count() == 2


# ---- the browser legs, per workspace ------------------------------------------------

def _start(client, slug, **params):
    url = reverse("connections_start", args=["salesforce", slug])
    return client.post(url, params) if "agent" in params else client.get(url, params)


def _state(resp):
    return parse_qs(urlparse(resp["Location"]).query)["state"][0]


def _landing(resp):
    u = urlparse(resp["Location"])
    return {k: v[0] for k, v in parse_qs(u.query).items()}


def test_a_persons_start_needs_membership_of_the_workspace(admin, editor, ws):
    sf_app(ws)
    stranger = _mk("x")
    _ws("theirs", "Theirs", admin=[stranger])
    sf_app(Workspace.objects.get(slug="theirs"))
    c = _browser(editor)
    assert _start(c, "w").status_code == 302
    assert _start(c, "theirs").status_code == 404                     # set up, but not mine
    assert _start(c, "nope").status_code == 404


def test_a_persons_start_in_a_workspace_without_an_app_is_not_configured(editor, ws):
    resp = _start(_browser(editor), "w", return_to="/settings")
    assert resp.status_code == 302
    assert _landing(resp) == {"connection": "salesforce", "status": "error", "reason": "not_configured"}


def test_an_agents_start_must_name_its_own_workspace(admin, ws):
    """An admin of the agent, starting it under another workspace (one they
    administer too, set up), is a 404: the agent connects with its own app only."""
    sf_app(ws)
    other = _ws("other", "Other", admin=[admin])
    sf_app(other, client_id="other-client")
    Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=admin)
    c = _browser(admin)
    assert _start(c, "other", agent="eva").status_code == 404
    resp = _start(c, "w", agent="eva")
    assert resp.status_code == 302 and parse_qs(urlparse(resp["Location"]).query)["client_id"] == ["3MVG-client"]


def test_an_agents_start_needs_its_admin(admin, editor, ws):
    sf_app(ws)
    Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=admin)
    assert _start(_browser(editor), "w", agent="eva").status_code == 403


def test_a_callback_for_another_workspace_is_bad_state(admin, ws):
    """Review Focus 1: a state parked for workspace B's app, delivered to
    workspace A's callback, is refused and nothing is exchanged — both ways round."""
    sf_app(ws)
    b = _ws("b", "B", admin=[admin])
    sf_app(b, client_id="b-client")
    c = _browser(admin)
    for parked_in, landed_in in (("b", "w"), ("w", "b")):
        state = _state(_start(c, parked_in))
        with FakeSF() as fake:
            resp = c.get(reverse("connections_callback", args=["salesforce", landed_in]),
                         {"code": "c0de", "state": state})
        assert _landing(resp) == {"connection": "salesforce", "status": "error", "reason": "bad_state"}
        assert fake.calls == []
    assert not Connection.objects.exists()
    # The right workspace's callback still completes it.
    state = _state(_start(c, "b"))
    with FakeSF() as fake:
        resp = c.get(reverse("connections_callback", args=["salesforce", "b"]), {"code": "c0de", "state": state})
    assert _landing(resp)["status"] == "connected"
    assert fake.token_calls()[0][2]["redirect_uri"] == "http://testserver/auth/connections/salesforce/b/callback/"
    assert Connection.objects.get().app.workspace_id == "b"


# ---- listings ----------------------------------------------------------------------

def test_my_list_has_a_row_per_workspace_that_set_the_provider_up(admin, ws):
    b = _ws("b", "Bravo", editor=[admin])
    _ws("c", "Charlie", editor=[admin])                               # nothing set up there
    sf_app(ws)
    sf_app(b)
    _stored({"user": admin}, admin, app=sf_app(b))
    body = _browser(admin).get("/api/connections/").json()
    assert body["providers"] == [
        {"name": "salesforce", "title": "Salesforce", "workspace": "b", "workspace_name": "Bravo", "configured": True},
        {"name": "salesforce", "title": "Salesforce", "workspace": "w", "workspace_name": "Dimagi",
         "configured": True},
        {"name": "google", "title": "Google", "workspace": "", "workspace_name": "", "configured": False},
    ]
    (row,) = body["connections"]
    assert row["workspace"] == "b" and row["workspace_name"] == "Bravo"


def test_an_agents_list_names_its_workspace(admin, ws):
    agent = Agent.objects.create(slug="eva", name="Eva", workspace=ws, owner=admin)
    _stored({"agent": agent}, admin)
    body = _browser(admin).get("/api/agents/eva/connections").json()
    assert body["providers"] == [{"name": "salesforce", "title": "Salesforce", "workspace": "w",
                                  "workspace_name": "Dimagi", "configured": True},
                                 {"name": "google", "title": "Google", "workspace": "w",
                                  "workspace_name": "Dimagi", "configured": False}]
    assert body["connections"][0]["workspace"] == "w"


# ---- my CLI token, across workspaces ------------------------------------------------

def _token(user, query=""):
    return Client().post(f"/api/me/connections/salesforce/token{query}", **_pat(user))


def test_a_person_who_left_the_workspace_gets_no_token(admin, editor, ws):
    """Review Focus 4."""
    _stored({"user": editor}, editor, app=sf_app(ws))
    assert _token(editor).status_code == 200
    M.objects.filter(workspace=ws, user=editor).delete()
    resp = _token(editor)
    assert resp.status_code == 409 and resp.json()["error"] == "not_connected"
    assert "00D!cached" not in resp.content.decode()


def test_ambiguous_across_workspaces_lists_them_and_workspace_picks_one(admin, ws):
    b = _ws("b", "B", editor=[admin])
    _stored({"user": admin}, admin, app=sf_app(ws))
    _stored({"user": admin}, admin, app=sf_app(b, client_id="b-client"), access="00D!b")
    resp = _token(admin)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"] == "ambiguous" and body["workspaces"] == ["b", "w"]
    assert body["accounts"] == ["jj@dimagi.com", "jj@dimagi.com"]
    assert body["connect_url"] == "http://testserver/settings/connections"
    got = _token(admin, "?workspace=b")
    assert got.status_code == 200 and got.json()["access_token"] == "00D!b"
    assert _token(admin, "?workspace=w").json()["access_token"] == "00D!cached"
    assert _token(admin, "?workspace=nope").json()["error"] == "not_connected"


def test_not_connected_points_at_the_one_set_up_workspace_or_lists_them(admin, ws):
    sf_app(ws)
    assert _token(admin).json()["connect_url"] == "http://testserver/auth/connections/salesforce/w/start/"
    sf_app(_ws("b", "B", editor=[admin]))
    body = _token(admin).json()
    assert body["error"] == "not_connected" and body["workspaces"] == ["b", "w"]
    assert body["connect_url"] == "http://testserver/settings/connections"
    narrowed = _token(admin, "?workspace=b").json()
    assert narrowed["connect_url"] == "http://testserver/auth/connections/salesforce/b/start/"
    assert "workspaces" not in narrowed


def test_reconnect_points_at_the_connections_workspace(admin, ws):
    b = _ws("b", "B", editor=[admin])
    sf_app(ws)
    _stored({"user": admin}, admin, app=sf_app(b), access="")
    with FakeSF(token=[_resp(400, {"error": "invalid_grant"})]):
        body = _token(admin).json()
    assert body["error"] == "reconnect_required"
    assert body["connect_url"] == "http://testserver/auth/connections/salesforce/b/start/"


# ---- the secret never comes back ----------------------------------------------------

def test_an_apps_secret_is_never_in_a_response_log_event_or_setting_change(admin, ws, caplog):
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="apps")
    secrets = ["first-s3cret", "rotated-s3cret", "refused-s3cret"]
    c = _browser(admin)
    bodies = []
    bodies.append(_put(c, client_id="3MVG-a", client_secret="first-s3cret",
                       options={"login_url": "https://login.salesforce.com"}))
    bodies.append(c.get(APPS.format("w")))
    app = ConnectionApp.objects.get()
    _stored({"user": admin}, admin, app=app, access="")
    bodies.append(_put(c, client_id="3MVG-a", client_secret="rotated-s3cret"))
    bodies.append(_put(c, client_id="3MVG-b", client_secret="refused-s3cret"))           # 409 app_locked
    bodies.append(_put(c, client_id="3MVG-a", client_secret="refused-s3cret", scopes=["nope"]))  # 400
    with FakeSF(token=[_resp(400, {"error": "invalid_client"})]):
        bodies.append(_token(admin))                                                       # 502
    with FakeSF(token=[_resp(body=_token_body())]):
        bodies.append(_token(admin, "?refresh=true"))
    bodies.append(c.get("/api/connections/"))
    with FakeSF():
        bodies.append(c.delete(f"{APPS.format('w')}/salesforce"))
    assert [r.status_code for r in bodies] == [200, 200, 200, 409, 400, 502, 200, 200, 204]

    tracebacks = [logging.Formatter().formatException(r.exc_info) for r in caplog.records if r.exc_info]
    blob = "\n".join(
        [r.content.decode() for r in bodies]
        + [r.getMessage() for r in caplog.records] + tracebacks
        + [json.dumps(e.payload) + e.summary for e in Event.objects.all()]
        + [json.dumps(model_to_dict(s), default=str) for s in SettingChange.objects.all()]
    )
    leaked = [s for s in secrets if s in blob]
    assert not leaked, f"an app's secret leaked: {leaked}"
    assert SettingChange.objects.filter(workspace=ws).exists()


def test_only_the_write_carrying_the_secret_is_kept_off_mcp():
    from apps.mcp import api_tools

    assert "configure_connection_app" in api_tools.EXCLUDED
    assert not api_tools.is_tool_route("PUT", "/api/workspaces/w/connection-apps/salesforce")
    assert api_tools.is_tool_route("GET", "/api/workspaces/w/connection-apps")
    assert api_tools.is_tool_route("DELETE", "/api/workspaces/w/connection-apps/salesforce")

"""Google Drive calls from screens: the allowlist (`calls.GOOGLE_RULES`), the query names a Google call
may carry, the URL it goes to, what a view may declare, and the route end to end with the token stubbed.

A security boundary like the Salesforce one (`test_connection_calls_allowlist`): a path or query that
slipped out would send the viewer's Google token somewhere unintended, or turn a metadata read into
a download. A Drive call runs only on the viewer's own hour of access for screens
(`temporary.for_screens`), never on a stored connection.
"""
from __future__ import annotations

import json
from datetime import timedelta
from unittest import mock

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.common.encryption import encrypt_secret
from apps.connections import call_service, calls, declarations, server_key, server_tokens, temporary
from apps.connections.calls import NotAllowed
from apps.connections.models import Connection, TemporaryAccess
from apps.connections.providers.google import GoogleProvider
from apps.dynamic_views import services as view_services
from apps.events.models import Event
from apps.workspaces.models import Workspace, WorkspaceMembership
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import _stored, sf_app

FILE = "1AbCdEfGhIjKlMnOpQrStUvWxYz012345"  # 33: a folder or file id
SHARED_DRIVE = "0AbCdEfGhIjKlMnOpQr"  # 19: a shared drive id
ROOT = calls.DRIVE_API_ROOT

ALLOWED = [
    ("GET", "files", "read", "files"),
    ("GET", f"files/{FILE}", "read", "files"),
    ("GET", f"files/{SHARED_DRIVE}", "read", "files"),
    ("GET", f"files/{FILE}/listLabels", "read", "labels"),
    ("POST", f"files/{FILE}/modifyLabels", "write", "labels"),
]


@pytest.mark.parametrize("method,path,kind,name", ALLOWED)
def test_each_drive_rule_matches_a_real_example_and_its_kind(method, path, kind, name):
    found = calls.match("google", method, path)
    assert (found.rule.kind, found.rule.name, found.rule.method) == (kind, name, method)
    assert found.path == path


def test_drive_root_is_fixed():
    assert ROOT == "https://www.googleapis.com/drive/v3"


@pytest.mark.parametrize("path", [
    "../files", "%2e%2e/files", "files%2F" + FILE, "files%2f" + FILE, f"files/{FILE}%2FlistLabels",
    "files.json", f"files/{FILE}/../../gmail/v1/users/me/messages", "//evil.com/files", "/files", "files/",
    "https://evil.com/files", "files?alt=media", "files%3Falt=media", "files#x", "files\\x", "files%00",
    "fi%256ces", "files@evil.com", "files:443", "",
])
def test_refuses_traversal_and_anything_that_could_move_the_host(path):
    with pytest.raises(NotAllowed):
        calls.match("google", "GET", path)
    with pytest.raises(NotAllowed):
        calls.build_url("google", "", path, None, method="GET")


@pytest.mark.parametrize("path", [
    "files/generateIds",  # Drive's own words under files/ are shorter than any id
    "files/trash",
    f"files/{FILE}/copy",
    f"files/{FILE}/permissions",
    f"files/{FILE}/export",
    f"files/{FILE}/watch",
    f"files/{FILE}/revisions",
    f"files/{FILE}/comments",
    f"files/{FILE}/listLabels/x",
    "files/short",
    "files/" + "a" * 65,
    "drives",
    "about",
    "changes",
    "users/me/messages",
    "gmail/v1/users/me/messages",
    "calendar/v3/calendars/primary/events",
])
def test_refuses_everything_outside_the_drive_list(path):
    for method in ("GET", "POST", "PATCH", "DELETE"):
        with pytest.raises(NotAllowed):
            calls.match("google", method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", "files"),  # create
    ("PATCH", f"files/{FILE}"),  # rename, move
    ("DELETE", f"files/{FILE}"),
    ("POST", f"files/{FILE}"),
    ("POST", f"files/{FILE}/listLabels"),
    ("GET", f"files/{FILE}/modifyLabels"),
    ("PATCH", f"files/{FILE}/modifyLabels"),
    ("PUT", f"files/{FILE}"),
])
def test_refuses_the_wrong_method(method, path):
    with pytest.raises(NotAllowed):
        calls.match("google", method, path)


def test_salesforce_paths_are_not_google_paths_and_back():
    with pytest.raises(NotAllowed):
        calls.match("google", "GET", "query")
    with pytest.raises(NotAllowed):
        calls.match("salesforce", "GET", "files")


# ---- the query -------------------------------------------------------------------------------

def test_drive_q_is_accepted_as_text_and_encoded_into_the_query_only():
    q = f"'{FILE}' in parents and trashed = false"
    query = {"q": q, "pageSize": "1000", "pageToken": "tok/en+=", "fields": "nextPageToken, files(id, name)",
             "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "allDrives"}
    assert calls.check_query(query, "google") == query
    url = calls.build_url("google", "", "files", query, method="GET")
    head, _, tail = url.partition("?")
    assert head == f"{ROOT}/files"
    assert "q=%27" + FILE + "%27+in+parents+and+trashed+%3D+false" in tail
    assert "pageToken=tok%2Fen%2B%3D" in tail


@pytest.mark.parametrize("key", ["alt", "uploadType", "access_token", "oauth_token", "key", "callback", "$.xgafv",
                                 "_HttpMethod", "Q", "q ", "prettyPrint", "quotaUser"])
def test_google_query_refuses_names_outside_the_list(key):
    with pytest.raises(NotAllowed):
        calls.check_query({key: "media"}, "google")
    with pytest.raises(NotAllowed):
        calls.build_url("google", "", f"files/{FILE}", {key: "media"}, method="GET")


def test_salesforce_query_rules_are_unchanged():
    assert calls.check_query({"alt": "x", "q": "SELECT Id FROM Lead"}) == {"alt": "x", "q": "SELECT Id FROM Lead"}
    with pytest.raises(NotAllowed):
        calls.check_query({"_HttpMethod": "DELETE"}, "salesforce")


# ---- the URL ---------------------------------------------------------------------------------

def test_build_url_is_the_fixed_drive_root():
    assert calls.build_url("google", "", f"files/{FILE}/listLabels", None, method="GET") == \
        f"{ROOT}/files/{FILE}/listLabels"
    assert calls.build_url("google", "", f"files/{FILE}/modifyLabels", {}, method="POST") == \
        f"{ROOT}/files/{FILE}/modifyLabels"


@pytest.mark.parametrize("instance", ["https://evil.com", "https://www.googleapis.com", "x"])
def test_build_url_refuses_a_google_connection_naming_a_host(instance):
    with pytest.raises(NotAllowed):
        calls.build_url("google", instance, "files", None, method="GET")


def test_google_is_viewer_only():
    assert "google" in calls.VIEWER_ONLY and "salesforce" not in calls.VIEWER_ONLY


# ---- the provider ----------------------------------------------------------------------------

def test_drive_metadata_is_a_scope_an_app_may_ask_for():
    assert "https://www.googleapis.com/auth/drive.metadata" in GoogleProvider.scope_choices
    # No switch: a screen's Drive call never uses a sealed connection, only the viewer's hour.
    assert (GoogleProvider.server_use_default, GoogleProvider.server_calls) == (False, False)
    assert "google" in calls.TEMPORARY_ONLY


# ---- declaring -------------------------------------------------------------------------------

GDECL = {"provider": "google", "as": "viewer",
         "reads": [{"method": "GET", "path": "files"}, {"method": "GET", "path": "files/*"},
                   {"method": "GET", "path": "files/*/listLabels"}],
         "writes": [{"method": "POST", "path": "files/*/modifyLabels"}]}


def test_a_drive_declaration_is_valid():
    assert declarations.validate([GDECL]) == [GDECL]


def test_a_drive_declaration_beside_a_salesforce_one():
    sf = {"provider": "salesforce", "as": "viewer", "reads": [{"method": "GET", "path": "query"}], "writes": []}
    assert declarations.validate([sf, GDECL]) == [sf, GDECL]


@pytest.mark.parametrize("entry,says", [
    ({**GDECL, "as": "shared", "agent": "eva"}, "viewer's own account only"),
    ({"provider": "google", "reads": [{"method": "POST", "path": "files/*/modifyLabels"}]}, "a write, not a read"),
    ({"provider": "google", "writes": [{"method": "GET", "path": "files"}]}, "a read, not a write"),
    ({"provider": "google", "writes": [{"method": "POST", "path": "*/*/modifyLabels"}]}, ""),
    ({"provider": "google", "reads": [{"method": "GET", "path": "files/*/permissions"}]}, ""),
    ({"provider": "google", "reads": [{"method": "GET", "path": "users/me/messages"}]}, ""),
])
def test_a_bad_drive_line_is_refused_at_save(entry, says):
    with pytest.raises(ValueError) as exc:
        declarations.validate([entry])
    assert says in str(exc.value)


def test_allows_a_drive_call_the_declaration_names():
    declared = declarations.validate([GDECL])
    write = calls.match("google", "POST", f"files/{FILE}/modifyLabels")
    assert declarations.allows(declared, "google", "viewer", None, write, "POST")
    assert not declarations.allows(declared, "salesforce", "viewer", None, write, "POST")
    read_only = declarations.validate([{**GDECL, "writes": []}])
    assert not declarations.allows(read_only, "google", "viewer", None, write, "POST")


@pytest.mark.django_db
def test_a_view_saves_the_drive_declaration(key):
    world = TokensWorld()
    c = Client()
    c.force_login(world.person)
    r = c.post("/api/dynamic-views/", json.dumps({"workspace": "w", "title": "Labels", "code": CODE,
                                                  "connections": [GDECL]}), content_type=J)
    assert r.status_code == 201, r.content
    assert r.json()["connections"] == [GDECL]
    r = c.post("/api/dynamic-views/", json.dumps({"workspace": "w", "title": "Labels", "code": CODE,
                                                  "connections": [{**GDECL, "as": "shared", "agent": "eva"}]}),
               content_type=J)
    assert r.status_code == 422, r.content


# ---- the route -------------------------------------------------------------------------------

J = "application/json"
URL = "/api/connections/google/request"
CODE = "function View({ canopy }) { return <div/>; }"
TOKEN = "ya29.access-token"


@pytest.fixture(autouse=True)
def _clean():
    server_tokens._forget_all()
    cache.clear()
    yield
    server_tokens._forget_all()
    cache.clear()


@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    yield k
    server_key.use_software_key_for_tests(None)


def _hour(world, **over):
    fields = {"app": world.gapp, "user": world.person, "agent": None, "provider": "google", "account_id": "1098",
              "account_label": "jj@dimagi.com", "token": encrypt_secret(TOKEN),
              "expires_at": timezone.now() + timedelta(minutes=50), **over}
    return TemporaryAccess.objects.create(**fields)


@pytest.fixture
def g(key, db):
    """jj's hour of Google for screens in workspace w, and a view of theirs declaring the Drive calls."""
    world = TokensWorld()
    world.gapp = sf_app(world.ws, provider="google", client_id="123.apps.googleusercontent.com")
    world.hour = _hour(world)
    world.view = view_services.create(workspace_id="w", project=None, user=world.person,
                                      data={"title": "Labels", "code": CODE, "connections": [GDECL]})
    world.client = Client()
    world.client.force_login(world.person)
    return world


def _post(world, **over):
    body = {"method": "GET", "path": "files", "surface": {"kind": "view", "id": world.view.pk,
                                                          "version": world.view.version}, **over}
    return world.client.post(URL, json.dumps(body), content_type=J)


def test_a_folder_listing_goes_to_drive_with_the_viewers_hour(g):
    q = f"'{FILE}' in parents and trashed = false"
    with Salesforce(Answer(body={"files": [{"id": FILE, "name": "a"}]})) as drive:
        r = _post(g, query={"q": q, "fields": "files(id,name)", "supportsAllDrives": "true"})
    assert r.status_code == 200, r.content
    assert r.json() == {"status": 200, "body": {"files": [{"id": FILE, "name": "a"}]}}
    call = drive.calls[0]
    assert call["method"] == "GET" and call["url"].startswith(f"{ROOT}/files?q=%27{FILE}%27+in+parents")
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert call["allow_redirects"] is False
    assert TOKEN not in r.content.decode()


def test_a_stored_google_connection_is_never_used(g):
    """Even a persistent connection canopy-web could open: Drive pages run only on the viewer's hour."""
    TemporaryAccess.objects.all().delete()
    _stored({"user": g.person}, g.person, app=g.gapp, instance_url="", account_id="1098",
            account_label="jj@dimagi.com", server_use=True, state=Connection.ACTIVE)
    with mock.patch.object(server_tokens, "access") as access, Salesforce() as drive:
        r = _post(g)
    assert r.status_code == 409 and r.json()["error"] == "not_connected", r.content
    access.assert_not_called()
    assert drive.calls == []


def test_with_no_hour_the_page_is_given_the_link_that_grants_one(g):
    TemporaryAccess.objects.all().delete()
    r = _post(g)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    assert "for the next hour" in r.json()["detail"]
    assert r.json()["connect_url"].endswith("/auth/connections/google/w/start/?temporary=1&for_screens=1")


@pytest.mark.parametrize("over", [{"expires_at": "past"}, {"agent": "eva"}, {"user": "ada"}])
def test_an_hour_that_ran_out_or_is_not_for_my_screens_is_not_used(g, over):
    TemporaryAccess.objects.all().delete()
    fields = {"expires_at": timezone.now() - timedelta(seconds=1)} if over.get("expires_at") else (
        {"agent": g.eva} if "agent" in over else {"user": g.ada})
    _hour(g, **fields)
    with Salesforce() as drive:
        r = _post(g)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    assert drive.calls == []


def test_another_workspaces_hour_is_not_used(g):
    other = Workspace.objects.create(slug="o", display_name="O", created_by=g.person)
    WorkspaceMembership.objects.create(workspace=other, user=g.person, role=WorkspaceMembership.EDITOR)
    TemporaryAccess.objects.filter(pk=g.hour.pk).update(app=sf_app(other, provider="google", client_id="o.apps"))
    with Salesforce() as drive:
        r = _post(g)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    assert drive.calls == []


def test_a_401_ends_it_and_gives_the_link_again(g):
    with Salesforce(Answer(status=401, body={"error": {"code": 401}})) as drive:
        r = _post(g)
    assert r.json()["error"] == "session_ended" and "for_screens=1" in r.json()["connect_url"]
    assert len(drive.calls) == 1, "an hour's token is never refreshed or retried"


def test_a_label_change_needs_a_click_first(g):
    with Salesforce() as drive:
        r = _post(g, method="POST", path=f"files/{FILE}/modifyLabels", body={"labelModifications": []})
    assert r.status_code == 409 and r.json()["error"] == "needs_operation"
    assert drive.calls == []


def test_a_label_change_with_an_operation_is_sent_and_recorded(g):
    surface = {"kind": "view", "id": g.view.pk, "version": g.view.version}
    opened = g.client.post("/api/connections/operations", json.dumps({"surface": surface}), content_type=J)
    assert opened.status_code == 200, opened.content
    assert opened.json()["writes"] == [{"provider": "google", "method": "POST", "path": "files/*/modifyLabels"}]
    mods = {"labelModifications": [{"labelId": "abc", "removeLabel": True}]}
    with Salesforce(Answer(body={"kind": "drive#modifyLabelsResponse", "modifiedLabels": []})) as drive:
        r = _post(g, method="POST", path=f"files/{FILE}/modifyLabels", body=mods,
                  operation_id=opened.json()["operation_id"])
    assert r.status_code == 200, r.content
    call = drive.calls[0]
    assert (call["method"], call["url"], call["json"]) == ("POST", f"{ROOT}/files/{FILE}/modifyLabels", mods)
    event = Event.objects.get(kind="connections.connection_call")
    assert event.workspace_id == "w" and event.payload["path"] == f"files/{FILE}/modifyLabels"
    assert event.payload["method"] == "POST" and event.payload["as"] == "viewer"
    assert TOKEN not in json.dumps(event.payload)


@pytest.mark.parametrize("over", [
    {"query": {"alt": "media"}},
    {"path": f"files/{FILE}/permissions"},
    {"path": "../gmail/v1/users/me/messages"},
    {"method": "DELETE", "path": f"files/{FILE}"},
])
def test_refused_before_any_token_is_read(g, over):
    with mock.patch.object(temporary, "token_of") as token, Salesforce() as drive:
        r = _post(g, **over)
    assert r.status_code == 403 and r.json()["error"] == "not_allowed", r.content
    token.assert_not_called()
    assert drive.calls == []


def test_an_undeclared_drive_call_is_refused(g):
    view_services.update(g.view, data={"connections": [{**GDECL, "writes": []}]}, user=g.person)
    g.view.refresh_from_db()
    with mock.patch.object(call_service, "_spend_write", return_value=None), Salesforce() as drive:
        r = _post(g, method="POST", path=f"files/{FILE}/modifyLabels", body={}, operation_id="op")
    assert r.status_code == 403 and r.json()["error"] == "not_declared"
    assert drive.calls == []


def test_an_agents_google_is_never_used_from_a_screen(g):
    with Salesforce() as drive:
        r = _post(g, **{"as": "shared", "agent": "eva"})
    assert r.status_code == 403 and r.json()["error"] == "not_allowed"
    assert drive.calls == []

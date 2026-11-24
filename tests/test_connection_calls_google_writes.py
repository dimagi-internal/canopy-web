"""Google writes from screens: what the Sales app's screens change in the viewer's own Google account
(`calls.GOOGLE_RULES`): a Gmail draft (never a send), Sheets values written and appended and a
spreadsheet's batchUpdate, a Doc's batchUpdate, a new Drive file. The rules, the `{x}:verb` and A1
range placeholders, the URL each goes to, what a view may declare, and the route end to end: a write
needs a click (a lease), is sent once, and is an Event.
"""
from __future__ import annotations

import json
from datetime import timedelta

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import Client
from django.utils import timezone

from apps.common.encryption import encrypt_secret
from apps.connections import calls, declarations, server_key, server_tokens
from apps.connections.calls import NotAllowed
from apps.connections.models import Connection, TemporaryAccess
from apps.dynamic_views import services as view_services
from apps.events.models import Event
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import sf_app

SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"
DOC = "1ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-dcba"
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DOCS = "https://docs.googleapis.com/v1/documents"
DRIVE = calls.DRIVE_API_ROOT

WRITES = [
    ("gmail/drafts", "gmail", f"{GMAIL}/drafts"),
    (f"sheets/{SHEET}/values:batchUpdate", "sheets", f"{SHEETS}/{SHEET}/values:batchUpdate"),
    (f"sheets/{SHEET}/values/Pathways!A:K:append", "sheets", f"{SHEETS}/{SHEET}/values/Pathways%21A%3AK:append"),
    (f"sheets/{SHEET}/values/'Pathway Types'!A2:D:append", "sheets",
     f"{SHEETS}/{SHEET}/values/%27Pathway%20Types%27%21A2%3AD:append"),
    (f"sheets/{SHEET}:batchUpdate", "sheets", f"{SHEETS}/{SHEET}:batchUpdate"),
    (f"docs/{DOC}:batchUpdate", "docs", f"{DOCS}/{DOC}:batchUpdate"),
    ("files", "files", f"{DRIVE}/files"),
]


@pytest.mark.parametrize("path,name,url", WRITES)
def test_each_write_is_a_post_and_goes_to_its_apis_fixed_root(path, name, url):
    found = calls.match("google", "POST", path)
    assert (found.rule.kind, found.rule.name) == ("write", name)
    assert calls.build_url("google", "", path, None, method="POST") == url


@pytest.mark.parametrize("path,_n,_u", WRITES)
def test_every_write_is_post_only(path, _n, _u):
    for method in ("PATCH", "DELETE"):
        with pytest.raises(NotAllowed):
            calls.match("google", method, path)


@pytest.mark.parametrize("path", [
    # Gmail: sending, by any road
    "gmail/messages/send", "gmail/drafts/send", "gmail/drafts/abc", "gmail/messages/import", "gmail/messages/insert",
    # Sheets: a range without its verb, another verb, a range with a slash, dot or query in it
    f"sheets/{SHEET}/values/A1:K", f"sheets/{SHEET}/values/A1:clear", f"sheets/{SHEET}/values/A1:batchUpdate",
    f"sheets/{SHEET}/values:batchClear", f"sheets/{SHEET}:copyTo", f"sheets/{SHEET}/values/a.b:append",
    f"sheets/{SHEET}/values/a?x:append", f"sheets/{SHEET}/values/:append",
    # Docs and Drive: anything beyond a batchUpdate and a new file's metadata
    f"docs/{DOC}:copy", f"files/{SHEET}/copy", f"files/{SHEET}/permissions", "files/generateIds",
    # the verb must follow the placeholder exactly
    f"sheets/{SHEET}:batchUpdateX", f"docs/{DOC}:batchupdate", "docs/x:batchUpdate",
])
def test_refuses_writes_outside_the_list(path):
    for method in ("POST", "PATCH", "DELETE"):
        with pytest.raises(NotAllowed):
            calls.match("google", method, path)


def test_a_range_can_never_move_the_call():
    for bad in ("../x:append", "a%2Fb:append", "a/b:append", "x@evil.com:append", "a#b:append"):
        with pytest.raises(NotAllowed):
            calls.build_url("google", "", f"sheets/{SHEET}/values/{bad}", None, method="POST")


def test_write_query_names():
    q = {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}
    url = calls.build_url("google", "", f"sheets/{SHEET}/values/A:B:append", q, method="POST")
    assert url.endswith(":append?valueInputOption=USER_ENTERED&insertDataOption=INSERT_ROWS")
    with pytest.raises(NotAllowed):
        calls.build_url("google", "", "gmail/drafts", {"uploadType": "media"}, method="POST")


# ---- declaring -------------------------------------------------------------------------------

DECL = {"provider": "google", "as": "viewer", "reads": [],
        "writes": [{"method": "POST", "path": "gmail/drafts"},
                   {"method": "POST", "path": "sheets/*/values:batchUpdate"},
                   {"method": "POST", "path": "sheets/*/values/*:append"},
                   {"method": "POST", "path": "sheets/*:batchUpdate"},
                   {"method": "POST", "path": "docs/*:batchUpdate"},
                   {"method": "POST", "path": "files"}]}


def test_the_sales_apps_writes_are_a_valid_declaration():
    assert declarations.validate([DECL]) == [DECL]


@pytest.mark.parametrize("line", [
    {"method": "POST", "path": "*/*:batchUpdate"},  # a star for the API's name
    {"method": "POST", "path": "gmail/*"},
    {"method": "POST", "path": "sheets/*/values/*:clear"},
    {"method": "POST", "path": "sheets/*/*:append"},
])
def test_a_bad_write_line_is_refused_at_save(line):
    with pytest.raises(ValueError):
        declarations.validate([{"provider": "google", "writes": [line]}])


def test_star_verb_covers_only_its_own_verb():
    declared = declarations.validate([DECL])
    append = calls.match("google", "POST", f"sheets/{SHEET}/values/Pathways!A:K:append")
    assert declarations.allows(declared, "google", "viewer", None, append, "POST")
    doc = calls.match("google", "POST", f"docs/{DOC}:batchUpdate")
    assert declarations.allows(declared, "google", "viewer", None, doc, "POST")
    only_docs = declarations.validate([{**DECL, "writes": [{"method": "POST", "path": "docs/*:batchUpdate"}]}])
    sheet = calls.match("google", "POST", f"sheets/{SHEET}:batchUpdate")
    assert not declarations.allows(only_docs, "google", "viewer", None, sheet, "POST")
    assert not declarations.allows(only_docs, "google", "viewer", None, append, "POST")


# ---- the route -------------------------------------------------------------------------------

J = "application/json"
TOKEN = "ya29.access-token"
CODE = "function View({ canopy }) { return <div/>; }"


@pytest.fixture(autouse=True)
def _clean():
    server_tokens._forget_all()
    cache.clear()
    yield
    server_tokens._forget_all()
    cache.clear()


@pytest.fixture
def g(db):
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    world = TokensWorld()
    world.gapp = sf_app(world.ws, provider="google", client_id="123.apps.googleusercontent.com")
    TemporaryAccess.objects.create(app=world.gapp, user=world.person, agent=None, provider="google",
                                   account_id="1098", account_label="jj@example.com", token=encrypt_secret(TOKEN),
                                   expires_at=timezone.now() + timedelta(minutes=50))
    world.view = view_services.create(workspace_id="w", project=None, user=world.person,
                                      data={"title": "Sales", "code": CODE, "connections": [DECL]})
    world.client = Client()
    world.client.force_login(world.person)
    world.surface = {"kind": "view", "id": world.view.pk, "version": world.view.version}
    yield world
    server_key.use_software_key_for_tests(None)


def _post(world, **body):
    return world.client.post("/api/connections/google/request",
                             json.dumps({"surface": world.surface, "method": "POST", **body}), content_type=J)


def test_a_draft_needs_a_click(g):
    with Salesforce() as google:
        r = _post(g, path="gmail/drafts", body={"message": {"raw": "eA"}})
    assert r.status_code == 409 and r.json()["error"] == "needs_operation"
    assert google.calls == []


def test_a_draft_and_an_append_with_a_click_are_sent_once_and_recorded(g):
    op = g.client.post("/api/connections/operations", json.dumps({"surface": g.surface}), content_type=J).json()
    with Salesforce(Answer(body={"id": "r-1", "message": {"id": "m1", "threadId": "t1"}})) as google:
        r = _post(g, path="gmail/drafts", body={"message": {"raw": "eA", "threadId": "t1"}},
                  operation_id=op["operation_id"])
    assert r.status_code == 200, r.content
    assert len(google.calls) == 1
    sent = google.calls[0]
    assert (sent["url"], sent["json"]) == (f"{GMAIL}/drafts", {"message": {"raw": "eA", "threadId": "t1"}})
    with Salesforce(Answer(body={"updates": {"updatedRows": 1}})) as google:
        r = _post(g, path=f"sheets/{SHEET}/values/Pathways!A:K:append", body={"values": [["a"]]},
                  query={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                  operation_id=op["operation_id"])
    assert r.status_code == 200, r.content
    assert google.calls[0]["url"].startswith(f"{SHEETS}/{SHEET}/values/Pathways%21A%3AK:append?valueInputOption=")
    paths = sorted(e.payload["path"] for e in Event.objects.filter(kind="connections.connection_call"))
    assert paths == ["gmail/drafts", f"sheets/{SHEET}/values/Pathways!A:K:append"]
    assert TOKEN not in json.dumps([e.payload for e in Event.objects.all()])


def test_no_send_whatever_is_declared(g):
    with pytest.raises(ValueError):
        declarations.validate([{"provider": "google", "writes": [{"method": "POST", "path": "gmail/messages/send"}]}])
    op = g.client.post("/api/connections/operations", json.dumps({"surface": g.surface}), content_type=J).json()
    with Salesforce() as google:
        r = _post(g, path="gmail/messages/send", body={"raw": "eA"}, operation_id=op["operation_id"])
    assert r.status_code == 403 and r.json()["error"] == "not_allowed"
    assert google.calls == []


def test_every_scope_at_once_fits_a_connection():
    from apps.connections.providers.google import GoogleProvider  # noqa: PLC0415
    every = " ".join((*GoogleProvider.scopes, *GoogleProvider.scope_choices))
    assert len(every) <= Connection._meta.get_field("scopes").max_length

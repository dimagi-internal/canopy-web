"""Gmail, Calendar, Sheets and Docs reads from screens: the rules beside Drive's in `calls.GOOGLE_RULES`,
the query names each API takes, the URL each goes to, what a view may declare, and the route end to
end with the token stubbed (as `test_connection_calls_google` does for Drive).

The same boundary as Drive's: a path or query that slipped out would send the viewer's Google token
somewhere unintended (another mailbox, another calendar, another host) or turn a read into a write.
These are reads only; nothing here sends mail, makes a draft or changes an event, cell or document.
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
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import _stored, sf_app

MSG = "18c2f5a1b2c3d4e5"  # a Gmail message or thread id: lowercase hex
SHEET = "1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789_-abcd"  # 44: a spreadsheet id
DOC = "1ZyXwVuTsRqPoNmLkJiHgFeDcBa9876543210_-dcba"

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
CALENDAR = "https://www.googleapis.com/calendar/v3"
SHEETS = "https://sheets.googleapis.com/v4/spreadsheets"
DOCS = "https://docs.googleapis.com/v1/documents"

ALLOWED = [
    ("GET", "gmail/profile", "gmail", f"{GMAIL}/profile"),
    ("GET", "gmail/messages", "gmail", f"{GMAIL}/messages"),
    ("GET", f"gmail/messages/{MSG}", "gmail", f"{GMAIL}/messages/{MSG}"),
    ("GET", f"gmail/threads/{MSG}", "gmail", f"{GMAIL}/threads/{MSG}"),
    ("GET", "calendar/calendars/primary", "calendar", f"{CALENDAR}/calendars/primary"),
    ("GET", "calendar/calendars/primary/events", "calendar", f"{CALENDAR}/calendars/primary/events"),
    ("GET", f"sheets/{SHEET}", "sheets", f"{SHEETS}/{SHEET}"),
    ("GET", f"sheets/{SHEET}/values:batchGet", "sheets", f"{SHEETS}/{SHEET}/values:batchGet"),
    ("GET", f"docs/{DOC}", "docs", f"{DOCS}/{DOC}"),
]


@pytest.mark.parametrize("method,path,name,url", ALLOWED)
def test_each_rule_is_a_read_and_goes_to_its_apis_fixed_root(method, path, name, url):
    found = calls.match("google", method, path)
    assert (found.rule.kind, found.rule.name, found.rule.method) == ("read", name, method)
    assert found.path == path
    assert calls.build_url("google", "", path, None, method=method) == url


def test_the_api_roots_are_fixed():
    assert calls.GOOGLE_API_ROOTS == {"gmail": GMAIL, "calendar": CALENDAR, "sheets": SHEETS, "docs": DOCS}
    assert calls.google_api("files") == calls.google_api("files/x") == "drive"
    assert calls.google_api("gmail/messages") == "gmail"


@pytest.mark.parametrize("path", [
    # Gmail: anything that sends, drafts, changes, or names another mailbox
    "gmail/messages/send", "gmail/drafts", "gmail/messages/import", "gmail/messages/batchDelete",
    f"gmail/messages/{MSG}/trash", f"gmail/messages/{MSG}/modify", f"gmail/messages/{MSG}/attachments",
    "gmail/settings", "gmail/labels", "gmail/history", "gmail/threads", f"gmail/messages/{MSG.upper()}",
    "gmail/v1/users/me/messages", "gmail/users/other@example.com/messages", "gmail",
    # Calendar: another calendar, the calendar list, settings, ACLs, one event's instances
    "calendar/calendars/other", "calendar/users/me/calendarList", "calendar/calendars/primary/acl",
    "calendar/calendars/primary/events/abc", "calendar/calendars/primary/events/quickAdd",
    "calendar/v3/calendars/primary",
    # Sheets / Docs: writes and anything beyond the two reads
    f"sheets/{SHEET}/values", f"sheets/{SHEET}/values/Sheet1", f"sheets/{SHEET}:batchUpdate",
    f"sheets/{SHEET}/values:append", f"sheets/{SHEET}/values:batchUpdate", f"sheets/{SHEET}/values:batchClear",
    f"sheets/{SHEET}/sheets", "sheets", f"docs/{DOC}:batchUpdate", "docs", f"docs/{DOC}/x",
    # a custom method that isn't a rule's literal
    "files:batchGet", f"files/{SHEET}:copy",
])
def test_refuses_everything_outside_the_list(path):
    for method in ("GET", "POST", "PATCH", "DELETE"):
        with pytest.raises(NotAllowed):
            calls.match("google", method, path)


@pytest.mark.parametrize("method,path", [(m, p) for _, p, _, _ in ALLOWED for m in ("POST", "PATCH", "DELETE")])
def test_every_new_rule_is_get_only(method, path):
    with pytest.raises(NotAllowed):
        calls.match("google", method, path)


@pytest.mark.parametrize("path", [
    "gmail/../files", "gmail/%2e%2e/messages", "gmail%2Fmessages", "gmail/messages%2F" + MSG,
    f"sheets/{SHEET}/values:batchGet:x", f"sheets/{SHEET}/values::batchGet", f"sheets/{SHEET}/values:batch-Get",
    f"sheets/{SHEET}/values%3AbatchGet%2F..", "//gmail.googleapis.com/gmail/v1/users/me/messages",
    "gmail/messages:443", "gmail/messages@evil.com", "https://evil.com/gmail/messages", f"docs/{DOC}?alt=media",
])
def test_refuses_traversal_and_anything_that_could_move_the_host(path):
    with pytest.raises(NotAllowed):
        calls.match("google", "GET", path)
    with pytest.raises(NotAllowed):
        calls.build_url("google", "", path, None, method="GET")


def test_salesforce_paths_take_no_custom_method():
    with pytest.raises(NotAllowed):
        calls.match("salesforce", "GET", "query:batchGet")


@pytest.mark.parametrize("instance", ["https://evil.com", "https://gmail.googleapis.com", "x"])
def test_build_url_refuses_a_google_connection_naming_a_host(instance):
    with pytest.raises(NotAllowed):
        calls.build_url("google", instance, "gmail/messages", None, method="GET")


# ---- the query -------------------------------------------------------------------------------

def test_gmail_search_is_text_encoded_into_the_query_only():
    query = {"q": "to:a@example.com OR from:a@example.com after:2026/11/01", "maxResults": "5",
             "pageToken": "tok/en+="}
    url = calls.build_url("google", "", "gmail/messages", query, method="GET")
    head, _, tail = url.partition("?")
    assert head == f"{GMAIL}/messages"
    assert "q=to%3Aa%40example.com+OR+from%3Aa%40example.com+after%3A2026%2F11%2F01" in tail
    assert "pageToken=tok%2Fen%2B%3D" in tail


def test_repeated_names_are_each_sent():
    url = calls.build_url("google", "", f"gmail/messages/{MSG}",
                          {"format": "metadata", "metadataHeaders": ["Subject", "Message-ID"]}, method="GET")
    assert url.endswith("?format=metadata&metadataHeaders=Subject&metadataHeaders=Message-ID")
    ranges = {"ranges": ["Pathways!A1:Z", "'Pathway Types'!A:C"]}
    url = calls.build_url("google", "", f"sheets/{SHEET}/values:batchGet", ranges, method="GET")
    assert url == (f"{SHEETS}/{SHEET}/values:batchGet?ranges=Pathways%21A1%3AZ"
                   "&ranges=%27Pathway+Types%27%21A%3AC")


@pytest.mark.parametrize("path,query", [
    ("calendar/calendars/primary/events", {"q": "a@example.com", "timeMin": "2026-11-24T00:00:00Z",
                                           "timeMax": "2027-05-23T00:00:00Z", "singleEvents": "true",
                                           "orderBy": "startTime", "maxResults": "250", "pageToken": "x"}),
    (f"sheets/{SHEET}", {"fields": "properties.title,sheets.properties"}),
    (f"sheets/{SHEET}/values:batchGet", {"ranges": "A1:B2", "majorDimension": "ROWS",
                                         "valueRenderOption": "FORMATTED_VALUE"}),
    (f"docs/{DOC}", {"fields": "title,body.content"}),
])
def test_each_api_takes_its_own_names(path, query):
    assert calls.check_query(query, "google", path) == query
    calls.build_url("google", "", path, query, method="GET")


@pytest.mark.parametrize("path,key", [
    ("gmail/messages", "alt"), ("gmail/messages", "uploadType"), ("gmail/messages", "key"),
    ("gmail/messages", "access_token"), ("gmail/messages", "pageSize"), ("gmail/messages", "includeGridData"),
    ("calendar/calendars/primary/events", "alt"), ("calendar/calendars/primary/events", "sendUpdates"),
    (f"sheets/{SHEET}", "includeGridData"), (f"sheets/{SHEET}/values:batchGet", "alt"),
    (f"sheets/{SHEET}/values:batchGet", "valueInputOption"), (f"docs/{DOC}", "alt"),
    (f"docs/{DOC}", "q"), ("files", "ranges"), ("files", "format"),
])
def test_a_name_another_api_takes_is_refused(path, key):
    with pytest.raises(NotAllowed):
        calls.check_query({key: "x"}, "google", path)
    with pytest.raises(NotAllowed):
        calls.build_url("google", "", path, {key: "x"}, method="GET")


def test_drive_names_are_unchanged_without_a_path():
    assert calls.check_query({"q": "x", "pageSize": "10"}, "google") == {"q": "x", "pageSize": "10"}
    with pytest.raises(NotAllowed):
        calls.check_query({"format": "full"}, "google")


# ---- the provider ----------------------------------------------------------------------------

def test_an_app_may_ask_for_each_read_scope():
    # Sheets and Docs reads ride `drive.readonly`, which both APIs accept.
    for scope in ("gmail.readonly", "calendar.readonly", "drive.readonly"):
        assert f"https://www.googleapis.com/auth/{scope}" in GoogleProvider.scope_choices
    assert (GoogleProvider.server_use_default, GoogleProvider.server_calls) == (False, False)


# ---- declaring -------------------------------------------------------------------------------

SALES = {"provider": "google", "as": "viewer",
         "reads": [{"method": "GET", "path": "gmail/profile"}, {"method": "GET", "path": "gmail/messages"},
                   {"method": "GET", "path": "gmail/messages/*"}, {"method": "GET", "path": "gmail/threads/*"},
                   {"method": "GET", "path": "calendar/calendars/primary"},
                   {"method": "GET", "path": "calendar/calendars/primary/events"},
                   {"method": "GET", "path": "sheets/*"}, {"method": "GET", "path": "sheets/*/values:batchGet"},
                   {"method": "GET", "path": "docs/*"}],
         "writes": []}


def test_the_sales_apps_declaration_is_valid():
    assert declarations.validate([SALES]) == [SALES]


@pytest.mark.parametrize("entry,says", [
    ({**SALES, "as": "shared", "agent": "eva"}, "viewer's own account only"),
    ({"provider": "google", "writes": [{"method": "GET", "path": "gmail/messages"}]}, "a read, not a write"),
    ({"provider": "google", "reads": [{"method": "POST", "path": "gmail/drafts"}]}, ""),
    ({"provider": "google", "reads": [{"method": "GET", "path": "gmail/*"}]}, ""),
    ({"provider": "google", "reads": [{"method": "GET", "path": "calendar/calendars/*/events"}]}, ""),
    ({"provider": "google", "reads": [{"method": "GET", "path": "sheets/*/values/*"}]}, ""),
])
def test_a_bad_line_is_refused_at_save(entry, says):
    with pytest.raises(ValueError) as exc:
        declarations.validate([entry])
    assert says in str(exc.value)


def test_allows_only_what_is_declared():
    declared = declarations.validate([{**SALES, "reads": [{"method": "GET", "path": "gmail/messages/*"}]}])
    one = calls.match("google", "GET", f"gmail/messages/{MSG}")
    assert declarations.allows(declared, "google", "viewer", None, one, "GET")
    thread = calls.match("google", "GET", f"gmail/threads/{MSG}")
    assert not declarations.allows(declared, "google", "viewer", None, thread, "GET")


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


@pytest.fixture
def g(key, db):
    """jj's hour of Google for screens in workspace w, and a view of theirs declaring the Sales reads."""
    world = TokensWorld()
    world.gapp = sf_app(world.ws, provider="google", client_id="123.apps.googleusercontent.com")
    world.hour = TemporaryAccess.objects.create(
        app=world.gapp, user=world.person, agent=None, provider="google", account_id="1098",
        account_label="jj@example.com", token=encrypt_secret(TOKEN), expires_at=timezone.now() + timedelta(minutes=50))
    world.view = view_services.create(workspace_id="w", project=None, user=world.person,
                                      data={"title": "Sales", "code": CODE, "connections": [SALES]})
    world.client = Client()
    world.client.force_login(world.person)
    return world


def _post(world, **over):
    body = {"method": "GET", "path": "gmail/messages",
            "surface": {"kind": "view", "id": world.view.pk, "version": world.view.version}, **over}
    return world.client.post(URL, json.dumps(body), content_type=J)


def test_a_gmail_search_goes_to_the_viewers_mailbox_with_their_hour(g):
    with Salesforce(Answer(body={"messages": [{"id": MSG, "threadId": MSG}]})) as google:
        r = _post(g, query={"q": "from:a@example.com", "maxResults": "5"})
    assert r.status_code == 200, r.content
    assert r.json() == {"status": 200, "body": {"messages": [{"id": MSG, "threadId": MSG}]}}
    call = google.calls[0]
    assert call["method"] == "GET" and call["url"] == f"{GMAIL}/messages?q=from%3Aa%40example.com&maxResults=5"
    assert call["headers"]["Authorization"] == f"Bearer {TOKEN}" and call["allow_redirects"] is False
    assert TOKEN not in r.content.decode()


@pytest.mark.parametrize("path,query,url", [
    ("calendar/calendars/primary/events", {"singleEvents": "true"},
     f"{CALENDAR}/calendars/primary/events?singleEvents=true"),
    (f"sheets/{SHEET}/values:batchGet", {"ranges": "Pathways!A:Z"},
     f"{SHEETS}/{SHEET}/values:batchGet?ranges=Pathways%21A%3AZ"),
    (f"docs/{DOC}", {"fields": "title,body.content"}, f"{DOCS}/{DOC}?fields=title%2Cbody.content"),
])
def test_calendar_sheets_and_docs_reads_reach_their_api(g, path, query, url):
    with Salesforce(Answer(body={"ok": True})) as google:
        r = _post(g, path=path, query=query)
    assert r.status_code == 200, r.content
    assert google.calls[0]["url"] == url


def test_with_no_hour_the_page_is_given_the_link_that_grants_one(g):
    TemporaryAccess.objects.all().delete()
    with Salesforce() as google:
        r = _post(g)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    assert r.json()["connect_url"].endswith("/auth/connections/google/w/start/?temporary=1&for_screens=1")
    assert google.calls == []


def test_an_undeclared_read_is_refused(g):
    narrowed = [{**SALES, "reads": [{"method": "GET", "path": "gmail/profile"}]}]
    view_services.update(g.view, data={"connections": narrowed}, user=g.person)
    g.view.refresh_from_db()
    with Salesforce() as google:
        r = _post(g)
    assert r.status_code == 403 and r.json()["error"] == "not_declared"
    assert google.calls == []


@pytest.mark.parametrize("over", [
    {"query": {"alt": "media"}},
    {"path": "gmail/messages/send", "method": "POST", "body": {"raw": "x"}},
    {"path": "gmail/drafts", "method": "POST", "body": {}},
    {"path": "calendar/calendars/someone@example.com/events"},
    {"path": f"sheets/{SHEET}/values:append", "method": "POST", "body": {}},
])
def test_refused_before_any_token_is_read(g, over):
    with mock.patch.object(temporary, "token_of") as token, Salesforce() as google:
        r = _post(g, **over)
    assert r.status_code == 403 and r.json()["error"] == "not_allowed", r.content
    token.assert_not_called()
    assert google.calls == []


# ---- the second consumer: a flag, not a rewrite -------------------------------------------------

def test_a_stored_connection_is_unused_while_server_calls_is_off(g):
    TemporaryAccess.objects.all().delete()
    _stored({"user": g.person}, g.person, app=g.gapp, instance_url="", account_id="1098",
            account_label="jj@example.com", server_use=True, state=Connection.ACTIVE)
    with mock.patch.object(server_tokens, "access") as access, Salesforce() as google:
        r = _post(g)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    access.assert_not_called()
    assert google.calls == []


def test_with_server_calls_on_a_screen_falls_back_to_the_stored_connection(g):
    """Turning `GoogleProvider.server_calls` on is all it takes for canopy-web to use a person's stored
    Google connection (with its switch on) when they have no hour live: same rules, same route."""
    TemporaryAccess.objects.all().delete()
    conn = _stored({"user": g.person}, g.person, app=g.gapp, instance_url="", account_id="1098",
                   account_label="jj@example.com", server_use=True, state=Connection.ACTIVE)
    stored = server_tokens.Access("ya29.stored", "", "d1")
    with mock.patch.object(GoogleProvider, "server_calls", True), \
            mock.patch.object(call_service.services, "connection_for_person", return_value=conn), \
            mock.patch.object(server_tokens, "access", return_value=stored) as access, \
            Salesforce(Answer(body={"messages": []})) as google:
        r = _post(g)
    assert r.status_code == 200, r.content
    access.assert_called_once()
    assert google.calls[0]["headers"]["Authorization"] == "Bearer ya29.stored"
    assert google.calls[0]["url"] == f"{GMAIL}/messages"


def test_with_server_calls_on_the_hour_still_comes_first_and_the_link_is_a_stored_connect(g):
    with mock.patch.object(GoogleProvider, "server_calls", True), \
            mock.patch.object(server_tokens, "access") as access, \
            Salesforce(Answer(body={"messages": []})) as google:
        r = _post(g)
    assert r.status_code == 200 and google.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    access.assert_not_called()
    TemporaryAccess.objects.all().delete()
    with mock.patch.object(GoogleProvider, "server_calls", True):
        r = _post(g)
    assert r.status_code == 409 and r.json()["error"] == "not_connected"
    assert r.json()["connect_url"].endswith("/auth/connections/google/w/start/")

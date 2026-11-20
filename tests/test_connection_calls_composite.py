"""A query's next page, `composite/batch` and sObject Collections through a screen's call.

Each composite call is taken apart (`calls.expand`) and every call inside it is checked exactly as
if it were made alone: the allowlist, `GUARDED_OBJECTS`, `_HttpMethod`, the screen's declaration,
and one place on the lease for each write. Salesforce is stubbed at the `requests` boundary, as in
`test_connection_calls_route`.
"""
from __future__ import annotations

import json
import logging

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from django.core.cache import cache
from django.test import Client

from apps.connections import call_service, calls, declarations, server_key, server_tokens
from apps.connections.calls import NotAllowed
from apps.connections.models import ConnectionOperation
from apps.dynamic_views import services as view_services
from apps.events.models import Event
from tests.fixtures.connection_calls import Answer, Salesforce, TokensWorld
from tests.fixtures.connections import FakeSF, sealed_to_owner

J = "application/json"
CALL = "/api/connections/salesforce/request"
OPEN = "/api/connections/operations"
CODE = "function View({ canopy }) { return <div/>; }"
INSTANCE = "https://dimagi.my.salesforce.com"
BASE = f"{INSTANCE}/services/data/v65.0"
LEAD = "00Q5g00000AbCdEFGH"
LEAD2 = "00Q5g00000AbCdEFGJ"
TASK = "00T5g00000AbCdEFGH"
USER = "0055g00000AbCdEFGH"
CUSTOM = "a0B5g00000AbCdEFGH"
LOCATOR = "01gD0000002HU6KIAW-2000"
DECL = {"provider": "salesforce", "as": "viewer",
        "reads": [{"method": "GET", "path": "query"}, {"method": "GET", "path": "sobjects/Lead/*"}],
        "writes": [{"method": "PATCH", "path": "sobjects/Lead/*"}, {"method": "POST", "path": "sobjects/Task"},
                   {"method": "DELETE", "path": "sobjects/Task/*"}]}


def sub(method: str, path: str, **more) -> dict:
    return {"method": method, "url": f"/services/data/v65.0/{path}", **more}


def batch(*subs, **more) -> dict:
    return {"batchRequests": list(subs), **more}


def records(type_: str, n: int, **fields) -> dict:
    return {"records": [{"attributes": {"type": type_}, "Subject": f"r{i}", **fields} for i in range(n)]}


def expand(method: str, path: str, query=None, body=None) -> calls.Composite:
    found = calls.match("salesforce", method, path)
    return calls.expand("salesforce", found, calls.check_query(query), body)


# ---- a query's next page --------------------------------------------------------------------

def test_a_next_page_is_a_read_the_query_covers():
    found = calls.match("salesforce", "GET", f"query/{LOCATOR}")
    assert found.rule.kind == "read" and found.path == f"query/{LOCATOR}" and found.rule.declared_as == "query"
    assert declarations.allows([DECL], "salesforce", "viewer", None, found, "GET")
    without_query = {**DECL, "reads": [{"method": "GET", "path": "sobjects/Lead/*"}]}
    assert not declarations.allows([without_query], "salesforce", "viewer", None, found, "GET")


@pytest.mark.parametrize("locator", [
    "abc", "01gD0000002HU6KIAW", "01gD0000002HU6KIAW-", "01gD0000002HU6KIAW-x", "01gD0000002HU6KIA-2000",
    "01gD0000002HU6KIAW-2000-1", "-2000", "01gD0000002HU6KIAW-1234567890",
])
def test_a_locator_is_a_query_id_a_dash_and_a_row(locator):
    with pytest.raises(NotAllowed):
        calls.match("salesforce", "GET", f"query/{locator}")


@pytest.mark.parametrize("method,path", [
    ("GET", f"query/{LOCATOR}/more"), ("POST", f"query/{LOCATOR}"), ("DELETE", f"query/{LOCATOR}"),
    ("GET", f"queryAll/{LOCATOR}"), ("GET", "query/..%2f..%2flimits"), ("GET", f"query/{LOCATOR}%3Fx=1"),
])
def test_only_a_get_of_one_page(method, path):
    with pytest.raises(NotAllowed):
        calls.match("salesforce", method, path)


def test_a_page_is_never_declared_on_its_own_nor_a_composite_call():
    for line in ({"method": "GET", "path": "query/*"}, {"method": "GET", "path": f"query/{LOCATOR}"}):
        with pytest.raises(ValueError):
            declarations.validate([{"provider": "salesforce", "reads": [line]}])
    for method, path in (("POST", "composite/batch"), ("POST", "composite/sobjects"),
                         ("PATCH", "composite/sobjects"), ("DELETE", "composite/sobjects")):
        with pytest.raises(ValueError, match="composite"):
            declarations.validate([{"provider": "salesforce", "writes": [{"method": method, "path": path}]}])
    with pytest.raises(ValueError, match="composite"):
        declarations.validate([{"provider": "salesforce",
                                "reads": [{"method": "GET", "path": "composite/sobjects/Lead"}]}])


# ---- composite/batch, taken apart -----------------------------------------------------------

def test_a_batch_is_rebuilt_from_what_was_matched():
    got = expand("POST", "composite/batch", body=batch(
        sub("GET", "query?q=SELECT+Id+FROM+Lead"),
        {"method": "patch", "url": f"v65.0/sobjects/Lead/{LEAD}", "richInput": {"Status": "Open"}},
        haltOnError=True))
    assert got.kind == "batch" and got.writes == 1 and got.query == {}
    assert [(p.rule.method, p.path) for p in got.parts] == [("GET", "query"), ("PATCH", f"sobjects/Lead/{LEAD}")]
    assert got.body == {"haltOnError": True, "batchRequests": [
        {"method": "GET", "url": "v65.0/query?q=SELECT+Id+FROM+Lead"},
        {"method": "PATCH", "url": f"v65.0/sobjects/Lead/{LEAD}", "richInput": {"Status": "Open"}}]}


def test_a_batch_may_page_a_query():
    got = expand("POST", "composite/batch", body=batch(sub("GET", f"query/{LOCATOR}")))
    assert got.parts[0].rule.declared_as == "query" and got.writes == 0


@pytest.mark.parametrize("url", [
    "https://evil.example.com/services/data/v65.0/query?q=x",
    "//evil.example.com/services/data/v65.0/query",
    "/services/data/v64.0/query?q=x",
    "v64.0/query",
    "/services/apexrest/Thing",
    "/services/data/v65.0/../v64.0/query",
    "/services/data/v65.0/sobjects%2fLead",
    "/services/data/v65.0/query#frag",
    "/services/data/v65.0/query?q=a b",
    "/services/data/v65.0/tooling/query?q=x",
    "/services/data/v65.0/query?_HttpMethod=DELETE",
    "/services/data/v65.0/query?_httpmethod=DELETE",
    "query",
    " /services/data/v65.0/query",
])
def test_a_subrequest_url_is_this_connections_own_api_and_nothing_else(url):
    with pytest.raises(NotAllowed):
        expand("POST", "composite/batch", body=batch({"method": "GET", "url": url}))


@pytest.mark.parametrize("inner", [
    sub("POST", "composite/batch", richInput={"batchRequests": []}),
    sub("POST", "composite/sobjects", richInput={"records": []}),
    sub("GET", f"composite/sobjects/Lead?ids={LEAD}&fields=Id"),
])
def test_nothing_nests(inner):
    with pytest.raises(NotAllowed, match="composite"):
        expand("POST", "composite/batch", body=batch(inner))


@pytest.mark.parametrize("body", [
    None, [], {}, {"batchRequests": []}, batch(*[sub("GET", "limits")] * 26),
    batch(sub("GET", "limits"), haltOnError="yes"), {"batchRequests": [sub("GET", "limits")], "extra": 1},
    batch({"method": "GET"}), batch({"url": "v65.0/limits"}), batch("v65.0/limits"),
    batch(sub("GET", "limits", binaryPartName="x")), batch(sub("GET", "limits", richInput={})),
    batch(sub("DELETE", f"sobjects/Task/{TASK}", richInput={})), batch(sub("PUT", f"sobjects/Lead/{LEAD}")),
    batch(sub("POST", "apexrest/Thing")),
])
def test_a_batch_must_be_plain(body):
    with pytest.raises(NotAllowed):
        expand("POST", "composite/batch", body=body)


def test_a_batch_takes_no_query():
    with pytest.raises(NotAllowed):
        expand("POST", "composite/batch", query={"x": "1"}, body=batch(sub("GET", "limits")))


@pytest.mark.parametrize("obj", ["User", "user", "PermissionSetAssignment", "ApexClass"])
def test_a_guarded_object_is_not_writable_in_a_batch(obj):
    with pytest.raises(NotAllowed, match="people, permissions or code"):
        expand("POST", "composite/batch", body=batch(sub("PATCH", f"sobjects/{obj}/{USER}", richInput={"x": 1})))


def test_twenty_five_is_the_most():
    assert len(expand("POST", "composite/batch", body=batch(*[sub("GET", "limits")] * 25)).parts) == 25


# ---- sObject Collections, taken apart -------------------------------------------------------

def test_a_collection_create_is_one_write_per_record():
    body = {"allOrNone": True, **records("Task", 3)}
    got = expand("POST", "composite/sobjects", body=body)
    assert got.kind == "collection" and got.writes == 3 and got.body == body
    assert {(p.rule.method, p.path) for p in got.parts} == {("POST", "sobjects/Task")}


def test_a_collection_update_names_each_record():
    body = {"records": [{"attributes": {"type": "Lead"}, "Id": LEAD, "Status": "x"},
                        {"attributes": {"type": "Lead"}, "id": LEAD2, "Status": "y"}]}
    got = expand("PATCH", "composite/sobjects", body=body)
    assert [p.path for p in got.parts] == [f"sobjects/Lead/{LEAD}", f"sobjects/Lead/{LEAD2}"]


@pytest.mark.parametrize("record", [
    {"attributes": {"type": "Lead"}, "Status": "x"},                                  # no Id
    {"attributes": {"type": "Lead"}, "Id": LEAD, "id": LEAD},                         # two
    {"attributes": {"type": "Lead"}, "Id": "not-an-id"},
    {"attributes": {"type": "Lead"}, "Id": TASK},                                     # a Task, called a Lead
    {"attributes": {"type": "Thing__c"}, "Id": CUSTOM},                               # can't tell what it is
    {"attributes": {"type": "Lead"}, "Id": USER},
    {"attributes": {"type": "User"}, "Id": USER},
    {"attributes": {"type": "Lead", "url": "/x"}, "Id": LEAD},
    {"Id": LEAD},
    "a record",
])
def test_a_collection_update_is_refused_when_its_record_cannot_be_checked(record):
    with pytest.raises(NotAllowed):
        expand("PATCH", "composite/sobjects", body={"records": [record]})


@pytest.mark.parametrize("obj", ["User", "Profile", "GroupMember", "apextrigger"])
def test_a_guarded_object_is_not_writable_in_a_collection(obj):
    with pytest.raises(NotAllowed, match="people, permissions or code"):
        expand("POST", "composite/sobjects", body=records(obj, 1))


@pytest.mark.parametrize("body", [
    None, {}, {"records": []}, records("Task", 201), {**records("Task", 1), "allOrNone": "true"},
    {**records("Task", 1), "extra": 1}, {"records": [{"attributes": {"type": "1Bad"}}]},
    {"records": [{"attributes": {"type": "Task/../User"}}]},
])
def test_a_collection_must_be_plain(body):
    with pytest.raises(NotAllowed):
        expand("POST", "composite/sobjects", body=body)


def test_two_hundred_records_is_the_most():
    assert expand("POST", "composite/sobjects", body=records("Task", 200)).writes == 200


def test_a_collection_delete_tells_each_records_object_from_its_id():
    got = expand("DELETE", "composite/sobjects", query={"ids": f"{TASK},{LEAD}", "allOrNone": "true"})
    assert [p.path for p in got.parts] == [f"sobjects/Task/{TASK}", f"sobjects/Lead/{LEAD}"]
    assert got.query == {"ids": f"{TASK},{LEAD}", "allOrNone": "true"} and got.writes == 2


@pytest.mark.parametrize("query", [
    {"ids": CUSTOM}, {"ids": USER}, {"ids": f"{TASK},"}, {"ids": ""}, {}, {"ids": [TASK, LEAD]},
    {"ids": TASK, "allOrNone": "yes"}, {"ids": TASK, "fields": "Id"},
    {"ids": ",".join([TASK] * 201)},
])
def test_a_collection_delete_is_refused_when_its_ids_cannot_be_checked(query):
    with pytest.raises(NotAllowed):
        expand("DELETE", "composite/sobjects", query=query)


def test_a_collection_read_is_one_read_per_id():
    got = expand("GET", "composite/sobjects/Lead", query={"ids": f"{LEAD},{LEAD2}", "fields": "Id,Name"})
    assert got.writes == 0 and [p.path for p in got.parts] == [f"sobjects/Lead/{LEAD}", f"sobjects/Lead/{LEAD2}"]
    with pytest.raises(NotAllowed):
        expand("GET", "composite/sobjects/Lead", query={"ids": LEAD})


# ---- through the route ----------------------------------------------------------------------

@pytest.fixture
def key():
    k = ec.generate_private_key(ec.SECP256R1())
    server_key.use_software_key_for_tests(k)
    server_tokens._forget_all()
    cache.clear()
    yield k
    server_key.use_software_key_for_tests(None)
    server_tokens._forget_all()
    cache.clear()


@pytest.fixture
def w(key, db):
    world = TokensWorld()
    world.conn = world.shared(sealed_to_owner({"user": world.person}, world.person, app=world.app, server_use=True),
                              canopy_web=server_key.thumbprint())
    world.view = view_services.create(workspace_id="w", project=None, user=world.person,
                                      data={"title": "Leads", "code": CODE, "connections": [DECL]})
    world.client = Client()
    world.client.force_login(world.person)
    return world


def call(world, method, path, *, query=None, body=None, operation_id=None):
    payload = {"method": method, "path": path, "surface": {"kind": "view", "id": world.view.pk,
                                                            "version": world.view.version}}
    if query is not None:
        payload["query"] = query
    if body is not None:
        payload["body"] = body
    if operation_id:
        payload["operation_id"] = operation_id
    return world.client.post(CALL, json.dumps(payload), content_type=J)


def lease(world) -> str:
    r = world.client.post(OPEN, json.dumps({"surface": {"kind": "view", "id": world.view.pk,
                                                         "version": world.view.version}}), content_type=J)
    assert r.status_code == 200, r.content
    return r.json()["operation_id"]


def error_of(r) -> str:
    return r.json().get("error")


def test_the_next_page_goes_to_salesforce_with_no_lease(w):
    page = {"totalSize": 4000, "done": False, "nextRecordsUrl": "/services/data/v65.0/query/01gD0000002HU6KIAW-4000"}
    with FakeSF(), Salesforce(Answer(body=page, headers={"Sforce-Limit-Info": "api-usage=18/15000"})) as sf:
        r = call(w, "GET", f"query/{LOCATOR}")
    assert r.status_code == 200, r.content
    assert r.json() == {"status": 200, "body": page, "limits": "api-usage=18/15000"}
    assert sf.calls[0]["url"] == f"{BASE}/query/{LOCATOR}" and sf.calls[0]["method"] == "GET"


def test_a_batch_of_reads_needs_no_click_and_is_one_log_line(w, caplog):
    caplog.set_level(logging.INFO, logger="apps.connections.services")
    answer = {"hasErrors": False, "results": [{"statusCode": 200, "result": {}}] * 2}
    with FakeSF(), Salesforce(Answer(body=answer)) as sf:
        r = call(w, "POST", "composite/batch", body=batch(sub("GET", "query?q=SELECT+Id+FROM+Lead"),
                                                          sub("GET", f"query/{LOCATOR}")))
    assert r.status_code == 200, r.content
    assert r.json()["body"] == answer
    [sent] = sf.calls
    assert sent["method"] == "POST" and sent["url"] == f"{BASE}/composite/batch"
    assert sent["json"] == {"batchRequests": [{"method": "GET", "url": "v65.0/query?q=SELECT+Id+FROM+Lead"},
                                              {"method": "GET", "url": f"v65.0/query/{LOCATOR}"}]}
    assert not Event.objects.filter(kind="connections.connection_call").exists()
    [line] = [rec.getMessage() for rec in caplog.records if "connections.connection_read" in rec.getMessage()]
    assert "path=composite/batch" in line and "parts=2" in line and "reads=2" in line


def test_a_batch_with_a_write_needs_a_click(w):
    body = batch(sub("PATCH", f"sobjects/Lead/{LEAD}", richInput={"Status": "Open"}))
    with FakeSF(), Salesforce() as sf:
        r = call(w, "POST", "composite/batch", body=body)
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == []


def test_a_batchs_writes_each_spend_a_place_and_are_each_an_event(w):
    op = lease(w)
    body = batch(sub("PATCH", f"sobjects/Lead/{LEAD}", richInput={"Status": "Open"}),
                 sub("GET", "query?q=SELECT+Id+FROM+Lead"),
                 sub("PATCH", f"sobjects/Lead/{LEAD2}", richInput={"Status": "Open"}), haltOnError=True)
    answer = {"hasErrors": True, "results": [{"statusCode": 204, "result": None}, {"statusCode": 200, "result": {}},
                                             {"statusCode": 400, "result": [{"errorCode": "X"}]}]}
    with FakeSF(), Salesforce(Answer(body=answer)) as sf:
        r = call(w, "POST", "composite/batch", body=body, operation_id=op)
    assert r.status_code == 200, r.content
    assert sf.calls[0]["json"]["haltOnError"] is True
    assert ConnectionOperation.objects.get(pk=op).writes_used == 2
    events = sorted((e.payload for e in Event.objects.filter(kind="connections.connection_call")),
                    key=lambda p: p["part"])
    assert [(p["method"], p["path"], p["status"], p["part"], p["parts"], p["composite"]) for p in events] == [
        ("PATCH", f"sobjects/Lead/{LEAD}", 204, 1, 3, "batch"),
        ("PATCH", f"sobjects/Lead/{LEAD2}", 400, 3, 3, "batch")]
    assert all(p["operation_id"] == op and p["outcome"] == "answered" for p in events)


def test_an_undeclared_call_in_a_batch_stops_all_of_it(w):
    op = lease(w)
    body = batch(sub("PATCH", f"sobjects/Lead/{LEAD}", richInput={}),
                 sub("PATCH", "sobjects/Account/0015g00000AbCdEFGH", richInput={}))
    with FakeSF(), Salesforce() as sf:
        r = call(w, "POST", "composite/batch", body=body, operation_id=op)
    assert r.status_code == 403 and error_of(r) == "not_declared"
    assert "Call 2" in r.json()["detail"]
    assert sf.calls == [] and ConnectionOperation.objects.get(pk=op).writes_used == 0


@pytest.mark.parametrize("inner", [
    {"method": "GET", "url": "https://evil.example.com/services/data/v65.0/query?q=x"},
    sub("POST", "composite/batch", richInput=batch(sub("GET", "limits"))),
    sub("PATCH", f"sobjects/User/{USER}", richInput={"IsActive": False}),
    sub("GET", "query?_HttpMethod=PATCH"),
])
def test_a_refused_call_in_a_batch_sends_nothing(w, inner):
    op = lease(w)
    with FakeSF(), Salesforce() as sf:
        r = call(w, "POST", "composite/batch", body=batch(inner), operation_id=op)
    assert r.status_code == 403 and error_of(r) == "not_allowed", r.content
    assert sf.calls == []


def test_a_collection_create_spends_a_place_per_record_and_is_an_event_each(w):
    op = lease(w)
    body = {"allOrNone": False, **records("Task", 3)}
    answer = [{"id": "00T000000000001AAA", "success": True, "errors": []},
              {"success": False, "errors": [{"statusCode": "REQUIRED_FIELD_MISSING"}]},
              {"id": "00T000000000003AAA", "success": True, "errors": []}]
    with FakeSF(), Salesforce(Answer(body=answer)) as sf:
        r = call(w, "POST", "composite/sobjects", body=body, operation_id=op)
    assert r.status_code == 200, r.content
    assert sf.calls[0]["url"] == f"{BASE}/composite/sobjects" and sf.calls[0]["json"] == body
    assert ConnectionOperation.objects.get(pk=op).writes_used == 3
    events = sorted((e.payload for e in Event.objects.filter(kind="connections.connection_call")),
                    key=lambda p: p["part"])
    assert [(p["method"], p["path"], p["ok"]) for p in events] == [
        ("POST", "sobjects/Task", True), ("POST", "sobjects/Task", False), ("POST", "sobjects/Task", True)]
    assert all(p["composite"] == "collection" and p["status"] == 200 for p in events)


def test_a_collection_delete_sends_its_ids(w):
    op = lease(w)
    with FakeSF(), Salesforce(Answer(body=[{"id": TASK, "success": True}])) as sf:
        r = call(w, "DELETE", "composite/sobjects", query={"ids": TASK, "allOrNone": "true"}, operation_id=op)
    assert r.status_code == 200, r.content
    assert sf.calls[0]["method"] == "DELETE"
    assert sf.calls[0]["url"] == f"{BASE}/composite/sobjects?ids={TASK}&allOrNone=true"
    [ev] = Event.objects.filter(kind="connections.connection_call")
    assert ev.payload["path"] == f"sobjects/Task/{TASK}" and ev.payload["method"] == "DELETE"


def test_a_collection_of_an_undeclared_object_is_refused(w):
    op = lease(w)
    with FakeSF(), Salesforce() as sf:
        r = call(w, "POST", "composite/sobjects", body=records("Contact", 2), operation_id=op)
    assert r.status_code == 403 and error_of(r) == "not_declared"
    assert sf.calls == []


def test_a_guarded_object_in_a_collection_is_refused(w):
    op = lease(w)
    with FakeSF(), Salesforce() as sf:
        r = call(w, "POST", "composite/sobjects", body=records("PermissionSetAssignment", 1), operation_id=op)
    assert r.status_code == 403 and error_of(r) == "not_allowed"
    assert sf.calls == []


def test_too_many_records_is_refused(w):
    op = lease(w)
    with FakeSF(), Salesforce() as sf:
        r = call(w, "POST", "composite/sobjects", body=records("Task", 201), operation_id=op)
    assert r.status_code == 403 and error_of(r) == "not_allowed" and "200" in r.json()["detail"]
    assert sf.calls == []


def test_a_collection_without_a_lease_is_refused(w):
    with FakeSF(), Salesforce() as sf:
        r = call(w, "POST", "composite/sobjects", body=records("Task", 2))
    assert r.status_code == 409 and error_of(r) == "needs_operation"
    assert sf.calls == []


def test_a_collection_past_the_leases_limit_is_refused_whole(w):
    op = lease(w)
    ConnectionOperation.objects.filter(pk=op).update(writes_used=call_service.OPERATION_MAX_WRITES - 2)
    with FakeSF(), Salesforce(Answer(body=[{"success": True}] * 2)) as sf:
        refused = call(w, "POST", "composite/sobjects", body=records("Task", 3), operation_id=op)
        assert refused.status_code == 409 and error_of(refused) == "needs_operation"
        assert "3" in refused.json()["detail"]
        assert sf.calls == []
        assert ConnectionOperation.objects.get(pk=op).writes_used == call_service.OPERATION_MAX_WRITES - 2
        fits = call(w, "POST", "composite/sobjects", body=records("Task", 2), operation_id=op)
    assert fits.status_code == 200, fits.content
    assert ConnectionOperation.objects.get(pk=op).writes_used == call_service.OPERATION_MAX_WRITES


# ---- Salesforce's limits --------------------------------------------------------------------

def test_a_429_is_provider_rate_limited_with_salesforces_wait(w):
    with FakeSF(), Salesforce(Answer(status=429, body=[{"errorCode": "X"}], headers={"Retry-After": "30"})):
        r = call(w, "GET", "query", query={"q": "SELECT Id FROM Lead"})
    assert r.status_code == 429 and error_of(r) == "provider_rate_limited"
    assert r["Retry-After"] == "30" and r.json()["retry_after"] == 30
    assert r.json()["limits"] == "api-usage=1/15000"


def test_the_orgs_api_limit_is_provider_rate_limited(w):
    used_up = [{"errorCode": "REQUEST_LIMIT_EXCEEDED", "message": "TotalRequests Limit exceeded."}]
    with FakeSF(), Salesforce(Answer(status=403, body=used_up)):
        r = call(w, "GET", "query", query={"q": "SELECT Id FROM Lead"})
    assert r.status_code == 429 and error_of(r) == "provider_rate_limited"
    assert "limit" in r.json()["detail"] and "Retry-After" not in r


def test_a_503_is_provider_rate_limited_and_its_write_still_recorded(w):
    op = lease(w)
    with FakeSF(), Salesforce(Answer(status=503, raw=b"")):
        r = call(w, "PATCH", f"sobjects/Lead/{LEAD}", body={"Status": "Open"}, operation_id=op)
    assert r.status_code == 429 and error_of(r) == "provider_rate_limited"
    [ev] = Event.objects.filter(kind="connections.connection_call")
    assert ev.payload["status"] == 503 and ev.payload["outcome"] == "rate_limited"


def test_a_limit_header_that_is_not_plain_is_not_passed_on(w):
    with FakeSF(), Salesforce(Answer(body={}, headers={"Sforce-Limit-Info": "api-usage=1/15000\r\nX: y"})):
        r = call(w, "GET", "query", query={"q": "x"})
    assert r.status_code == 200 and "limits" not in r.json()

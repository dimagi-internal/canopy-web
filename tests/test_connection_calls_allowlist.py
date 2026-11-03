"""The allowlist for connection calls: which Salesforce paths a screen may reach.

A security boundary: a path that slips out would send a real token somewhere
unintended, so the refusals here are tested against every escape we know of.
"""
from __future__ import annotations

import pytest

from apps.connections import calls
from apps.connections.calls import NotAllowed

LEAD = "00Q5g00000AbCdEFGH"
INSTANCE = "https://dimagi.my.salesforce.com"

# (method, path, kind, rule name): a real example of each allowed rule.
ALLOWED = [
    ("GET", "query", "read", "query"),
    ("GET", "search", "read", "search"),
    ("GET", "sobjects/Lead/describe", "read", "describe"),
    ("GET", "sobjects/Lead", "read", "sobject"),
    ("GET", f"sobjects/Lead/{LEAD}", "read", "sobject"),
    ("POST", "sobjects/Event", "write", "sobject"),
    ("POST", f"sobjects/Lead/{LEAD}", "write", "sobject"),
    ("PATCH", f"sobjects/Lead/{LEAD}", "write", "sobject"),
    ("DELETE", f"sobjects/Task/{LEAD}", "write", "sobject"),
    ("GET", "analytics/reports", "read", "reports"),
    ("GET", f"analytics/reports/{LEAD}/describe", "read", "reports"),
    ("POST", f"analytics/reports/{LEAD}", "write", "reports"),
    ("GET", f"analytics/dashboards/{LEAD}", "read", "dashboards"),
    ("POST", "actions/custom/flow/Create_Lead_From_Contact", "write", "flow"),
    ("POST", "apexrest/convert-lead", "write", "apexrest"),
    ("GET", "chatter/users/me", "read", "me"),
    ("GET", "limits", "read", "limits"),
    ("GET", "ui-api/object-info/Lead", "read", "ui-api"),
    ("GET", f"ui-api/object-info/Lead/picklist-values/{LEAD}/Status", "read", "ui-api"),
]


@pytest.mark.parametrize("method,path,kind,name", ALLOWED)
def test_each_rule_matches_a_real_example_and_its_kind(method, path, kind, name):
    rule = calls.match("salesforce", method, path)
    assert (rule.kind, rule.name) == (kind, name)
    assert rule.method == method


def test_method_is_case_insensitive():
    assert calls.match("salesforce", "get", "query").name == "query"


@pytest.mark.parametrize("path", [
    "../",
    "../query",
    "%2e%2e/",
    "%2e%2e/query",
    "%2E%2E",
    "%2E%2E/query",
    "..%2f",
    "..%2fquery",
    "//evil.com/x",
    "/query",
    "query/",
    "https://evil.com",
    "https://evil.com/query",
    "sobjects/Lead/../../x",
    f"sobjects/Lead/{LEAD}/../../../x",
    "sobjects/./Lead",
    ".",
    "query?x=y",
    "query%3Fx=y",
    "query#frag",
    "query%23frag",
    "sobjects\\Lead",
    "sobjects%5CLead",
    "query\x00",
    "query%00",
    "query\n",
    "query%0d%0aHost:evil",
    "que%2572y",           # double-encoded: decoded once it still holds a '%'
    "%252e%252e/query",
    "",
])
def test_refuses_traversal(path):
    with pytest.raises(NotAllowed):
        calls.match("salesforce", "GET", path)


def test_refusal_is_a_sentence_without_jargon_leakage():
    with pytest.raises(NotAllowed) as exc:
        calls.match("salesforce", "GET", "../x")
    text = str(exc.value)
    assert text.endswith(".") and len(text.split()) >= 4


def test_refuses_composite():
    for method in ("GET", "POST", "PATCH", "DELETE"):
        for path in ("composite", "composite/batch", "composite/tree/Account", "composite/sobjects"):
            with pytest.raises(NotAllowed):
                calls.match("salesforce", method, path)


@pytest.mark.parametrize("method,path", [
    ("POST", "query"),
    ("DELETE", "query"),
    ("POST", "search"),
    ("POST", "sobjects/Lead/describe"),
    ("DELETE", "sobjects/Lead"),
    ("PATCH", "sobjects/Lead"),
    ("POST", "analytics/dashboards/01Z"),
    ("GET", "actions/custom/flow/Create_Lead_From_Contact"),
    ("GET", "apexrest/convert-lead"),
    ("POST", "chatter/users/me"),
    ("POST", "limits"),
    ("POST", "ui-api/object-info/Lead"),
    ("PUT", f"sobjects/Lead/{LEAD}"),
    ("TRACE", "query"),
    ("", "query"),
])
def test_refuses_wrong_method(method, path):
    with pytest.raises(NotAllowed):
        calls.match("salesforce", method, path)


@pytest.mark.parametrize("path", [
    "sobjects",
    "sobjects/Lead/notanid",
    "sobjects/Lead/001/extra",
    "sobjects/Lead/ExtId__c/abc",
    "actions/custom/flow",
    "actions/custom/apex/Thing",
    "actions/standard/emailSimple",
    "apexrest",
    "apexrest/a/b",
    "tooling/query",
    "connect/anything",
    "sobjects/Lead/describe/x",
    "query/more",
    "limits/recordCount",
    "chatter/users/me/x",
    "chatter/users/005",
])
def test_refuses_shapes_outside_the_list(path):
    for method in ("GET", "POST", "PATCH", "DELETE"):
        # Every method, so a path that is allowed for none stays refused for all.
        with pytest.raises(NotAllowed):
            calls.match("salesforce", method, path)


def test_google_not_supported():
    with pytest.raises(NotAllowed) as exc:
        calls.match("google", "GET", "users/me/messages")
    assert isinstance(exc.value, calls.NotSupported)
    assert "Google" in str(exc.value)


def test_unknown_provider_not_supported():
    with pytest.raises(calls.NotSupported):
        calls.match("nope", "GET", "query")


def test_build_url_keeps_host():
    url = calls.build_url("salesforce", INSTANCE, "query", {"q": "SELECT Id FROM Lead"})
    assert url == f"{INSTANCE}/services/data/{calls.SF_API_VERSION}/query?q=SELECT+Id+FROM+Lead"
    assert calls.build_url("salesforce", INSTANCE, "limits", None) == \
        f"{INSTANCE}/services/data/{calls.SF_API_VERSION}/limits"
    assert calls.build_url("salesforce", INSTANCE, "apexrest/convert-lead", {}) == \
        f"{INSTANCE}/services/apexrest/convert-lead"


def test_api_version_is_the_orgs():
    assert calls.SF_API_VERSION == "v65.0"


@pytest.mark.parametrize("path", [
    "..", "../evil.com", "%2e%2e/%2e%2e/x", "//evil.com", "@evil.com", "evil.com/../x",
    "https://evil.com", "query@evil.com", "query:80", "query?host=evil.com",
])
def test_build_url_cannot_be_moved_by_the_path(path):
    with pytest.raises(NotAllowed):
        calls.build_url("salesforce", INSTANCE, path, None)


def test_build_url_query_cannot_change_the_host():
    url = calls.build_url("salesforce", INSTANCE, "query", {"q": "x#@evil.com/", "a b": ["1", "2&c=3"]})
    assert url.startswith(f"{INSTANCE}/services/data/{calls.SF_API_VERSION}/query?")
    assert "#" not in url and "evil.com/" not in url.split("?", 1)[0]
    assert "a+b=1&a+b=2%26c%3D3" in url


@pytest.mark.parametrize("instance", [
    "http://dimagi.my.salesforce.com",
    "https://evil.com",
    "https://dimagi.my.salesforce.com.evil.com",
    "https://dimagi.my.salesforce.com@evil.com",
    "https://dimagi.my.salesforce.com:8443",
    "https://dimagi.my.salesforce.com/path",
    "https://login.salesforce.com",
    "",
])
def test_build_url_refuses_a_host_that_is_not_a_my_domain(instance):
    with pytest.raises(NotAllowed):
        calls.build_url("salesforce", instance, "query", None)


def test_build_url_google_not_supported():
    with pytest.raises(calls.NotSupported):
        calls.build_url("google", "", "users/me/messages", None)

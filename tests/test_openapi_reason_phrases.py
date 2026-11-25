"""The OpenAPI document (and so `generated.ts`) does not depend on the Python that dumps it.

Ninja describes a response by the stdlib reason phrase, and Python 3.13 renamed
four of them (422 "Unprocessable Entity" -> "Unprocessable Content", 413, 414,
416). `apps/api/openapi_doc.py` pins the RFC 9110 phrases, so a schema dumped on
any interpreter reads the same and CI's stale-`generated.ts` check cannot flip
on the interpreter alone.
"""
from __future__ import annotations

from http.client import responses as stdlib_phrases

from apps.api.api import api
from apps.api.errors import _RFC9110_PHRASES, reason_phrase, status_title
from apps.api.openapi_doc import _pinned_descriptions

# What Python 3.12 calls them; none may reach the document.
PRE_313 = {413: "Request Entity Too Large", 414: "Request-URI Too Long",
           416: "Requested Range Not Satisfiable", 422: "Unprocessable Entity"}


def test_the_pinned_phrases_are_rfc9110():
    assert reason_phrase(422) == "Unprocessable Content"
    assert status_title(413) == "Content Too Large"
    assert reason_phrase(404) == "Not Found"
    assert reason_phrase(799) is None and status_title(799) == "Error"


def test_a_pre_313_default_description_is_rewritten():
    # Simulate Ninja on Python 3.12: its default description is the old phrase.
    responses = {code: {"description": old} for code, old in PRE_313.items()}
    responses[200] = {"description": "OK"}
    responses[409] = {"description": "A route's own words"}
    real = dict(stdlib_phrases)
    stdlib_phrases.update(PRE_313)
    try:
        _pinned_descriptions(responses)
    finally:
        stdlib_phrases.clear()
        stdlib_phrases.update(real)
    assert {c: r["description"] for c, r in responses.items()} == {
        **_RFC9110_PHRASES, 200: "OK", 409: "A route's own words"}


def test_the_document_carries_no_pre_313_phrase():
    schema = api.get_openapi_schema()
    found = [
        (path, method, status)
        for path, item in schema["paths"].items()
        for method, op in item.items() if isinstance(op, dict)
        for status, resp in op.get("responses", {}).items()
        if resp.get("description") in PRE_313.values()
    ]
    assert not found, found

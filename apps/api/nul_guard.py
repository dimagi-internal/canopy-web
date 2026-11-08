"""Refuse a NUL character anywhere in a JSON request body, before any view runs.

PostgreSQL stores no `\\x00` in `text`, `varchar` or `jsonb`. A free-text field
says so in the OpenAPI document (`openapi_doc`), but free-form JSON — a `dict`
payload, `list[dict]` evidence, an agent task's `input` — can carry one at any
depth, in a value or a key, and no JSON Schema keyword reaches inside it. Left to
the database, such a request was refused late (a `DataError`, answered 400 by
`api._on_out_of_range`) or, where nothing stored it, accepted. This answers it
once, here, with the same 400 "Value cannot be stored" problem, for every JSON
body under `/api/`.

Only JSON bodies are read: multipart uploads, form posts and raw files pass
through untouched. A body with no `\\u0000` escape — almost every request — is
not parsed at all (JSON cannot hold a bare NUL byte; that is malformed JSON and
the API's own parser answers it).
"""
from __future__ import annotations

import json

from django.core.exceptions import RequestDataTooBig
from django.http import HttpRequest, HttpResponse

from .errors import TYPE_VALIDATION, UNSTORABLE_DETAIL, UNSTORABLE_TITLE, problem_response

_NUL = "\x00"
_ESCAPED_NUL = b"\\u0000"
_BODY_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def holds_nul(value) -> bool:
    """Whether a decoded JSON value holds a NUL in any string — value or object key — at any depth."""
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if _NUL in node:
                return True
        elif isinstance(node, dict):
            for key, item in node.items():
                if _NUL in key:
                    return True
                stack.append(item)
        elif isinstance(node, list):
            stack.extend(node)
    return False


def _is_json(content_type: str) -> bool:
    media = content_type.split(";", 1)[0].strip().lower()
    return media == "application/json" or (media.startswith("application/") and media.endswith("+json"))


class RefuseNulInJsonMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if (request.method in _BODY_METHODS and request.path_info.startswith("/api/")
                and _is_json(request.META.get("CONTENT_TYPE", ""))):
            try:
                body = request.body
            except RequestDataTooBig:
                body = b""  # the view answers it (413)
            if _ESCAPED_NUL in body and _decoded_holds_nul(body):
                return problem_response(400, type=TYPE_VALIDATION, title=UNSTORABLE_TITLE,
                                        detail=UNSTORABLE_DETAIL, instance=request.path)
        return self.get_response(request)


def _decoded_holds_nul(body: bytes) -> bool:
    try:
        data = json.loads(body)
    except (ValueError, RecursionError):
        return False  # malformed: the API's parser answers that
    return holds_nul(data)

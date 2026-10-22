"""RFC 7807 problem+json error model + helpers."""
from __future__ import annotations

import json
from http import HTTPStatus
from typing import Any

from django.http import HttpResponse
from ninja.errors import HttpError
from pydantic import BaseModel, Field


class Problem(BaseModel):
    """RFC 7807 application/problem+json body.

    `type` is a stable URI identifying the error class.
    `title` is human-readable, stable per `type`.
    `status` mirrors the HTTP status.
    `detail` is the per-occurrence message.
    `instance` is the request path (optional).
    """

    type: str = Field(default="about:blank")
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    extras: dict[str, Any] | None = None


class ProblemError(HttpError):
    """Raise this anywhere in a v2 handler to short-circuit with a problem+json response."""

    def __init__(
        self,
        status: int,
        title: str,
        *,
        type_: str = "about:blank",
        detail: str | None = None,
        extras: dict[str, Any] | None = None,
        members: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status, title)
        self.problem_type = type_
        self.problem_title = title
        self.problem_detail = detail
        self.problem_extras = extras
        # RFC 7807 §3.2 extension members: TOP-LEVEL keys a client branches on
        # without digging into `extras` (`{"error": "step_up_required"}`).
        self.problem_members = members
        # Response headers the refusal needs (`WWW-Authenticate` on a 401).
        self.problem_headers = headers


def status_title(status: int) -> str:
    """The stable ``title`` for a problem of ``type: about:blank`` — RFC 7807
    §4.2 says it SHOULD be the status's reason phrase, the same for every
    occurrence. The per-occurrence message belongs in ``detail``."""
    try:
        return HTTPStatus(status).phrase
    except ValueError:
        return "Error"


def problem_response(
    status: int,
    *,
    type: str = "about:blank",  # noqa: A002 - the RFC 7807 member name
    title: str | None = None,
    detail: str | None = None,
    **members: Any,
) -> HttpResponse:
    """A problem+json response built outside a Ninja route (middleware, bare
    views). ``members`` are RFC 7807 extension members, at the top level beside
    the standard ones, which they never replace. Serialized with ``json.dumps``,
    so any text (a slug from the URL) is escaped."""
    body: dict[str, Any] = {**members, "type": type,
                            "title": title or status_title(status), "status": status}
    if detail is not None:
        body["detail"] = detail
    return HttpResponse(json.dumps(body), status=status, content_type="application/problem+json")


# Common type URIs — extend as needed.
TYPE_VALIDATION = "https://canopy-web.dimagi.com/problems/validation"
TYPE_AUTH = "https://canopy-web.dimagi.com/problems/auth"
TYPE_FORBIDDEN = "https://canopy-web.dimagi.com/problems/forbidden"
TYPE_STEP_UP_REQUIRED = "https://canopy-web.dimagi.com/problems/step-up-required"
TYPE_NOT_FOUND = "https://canopy-web.dimagi.com/problems/not-found"
TYPE_CONFLICT = "https://canopy-web.dimagi.com/problems/conflict"
TYPE_RATE_LIMIT = "https://canopy-web.dimagi.com/problems/rate-limit"
TYPE_UPSTREAM = "https://canopy-web.dimagi.com/problems/upstream"
TYPE_INTERNAL = "https://canopy-web.dimagi.com/problems/internal"
TYPE_PAYLOAD_TOO_LARGE = "https://canopy-web.dimagi.com/problems/payload-too-large"
TYPE_DRIVE_NOT_CONFIGURED = "https://canopy-web.dimagi.com/problems/drive-not-configured"
TYPE_DRIVE_UPLOAD_FAILED = "https://canopy-web.dimagi.com/problems/drive-upload-failed"

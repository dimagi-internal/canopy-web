"""RFC 7807 problem+json error model + helpers."""
from __future__ import annotations

from typing import Any

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
    ) -> None:
        super().__init__(status, title)
        self.problem_type = type_
        self.problem_title = title
        self.problem_detail = detail
        self.problem_extras = extras
        # RFC 7807 §3.2 extension members: TOP-LEVEL keys a client branches on
        # without digging into `extras` (`{"error": "step_up_required"}`).
        self.problem_members = members


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

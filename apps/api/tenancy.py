"""Tenant (workspace) resolution for the /api surface.

Scoped routers are mounted ONCE, flat (``/api/agents/...``) — that keeps the
OpenAPI schema single + clean. The canonical tenant URL ``/api/w/{ws}/agents/...``
is served by this middleware, which:

  * verifies the caller is a member of ``{ws}`` (non-member → 404, no leak),
  * stashes ``request.workspace_slug = ws``,
  * strips the ``/w/{ws}`` segment so the request reroutes to the flat mount.

Legacy flat ``/api/agents/...`` calls (existing PAT / plugin callers) fall
through untouched: ``workspace_slug`` stays ``None`` and the handler applies its
pre-tenancy default-workspace logic (non-breaking).

Handlers read ``getattr(request, "workspace_slug", None)``: truthy pins the
tenant; ``None`` means flat/compat.
"""
from __future__ import annotations

import re

from django.http import HttpRequest, HttpResponse

from apps.workspaces import services as wsvc

_WS_RE = re.compile(r"^/api/w/(?P<ws>[^/]+)(?P<rest>/.*)$")


def _problem_404(detail: str) -> HttpResponse:
    body = (
        b'{"type":"about:blank","title":"Not found","status":404,"detail":"'
        + detail.encode()
        + b'"}'
    )
    return HttpResponse(body, status=404, content_type="application/problem+json")


class WorkspaceResolveMiddleware:
    """Gate + strip the ``/api/w/{ws}/`` prefix, then reroute to the flat mount.

    Runs after auth middleware (so ``request.user`` is resolved for session +
    PAT) and before URL resolution, so the path rewrite reroutes cleanly.
    Anonymous callers are left for LoginRequiredMiddleware (401)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        request.workspace_slug = None  # type: ignore[attr-defined]
        m = _WS_RE.match(request.path_info)
        if m:
            user = getattr(request, "user", None)
            if user is not None and user.is_authenticated:
                ws = m.group("ws")
                if not wsvc.is_member(user, ws):
                    return _problem_404(f"workspace '{ws}' not found")
                request.workspace_slug = ws  # type: ignore[attr-defined]
                flat = "/api" + m.group("rest")  # strip /w/{ws} → reroute to flat mount
                request.path = flat
                request.path_info = flat
        return self.get_response(request)

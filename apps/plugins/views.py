"""The two marketplace URLs — bare Django views, like the walkthrough stream.

They are not Ninja routes because their addresses are fixed by what Claude Code
fetches (`/w/<ws>/marketplace.json`, a `.zip` URL), they return raw JSON / raw
bytes rather than an API envelope, and they must not become MCP tools.

Auth is self-enforced here, and that is why `LoginRequiredMiddleware` admits
these paths: its fallback for a signed-out request outside `/api/` is a 302 to
Google sign-in, which Claude Code would follow cross-origin and fail on with no
useful message. Here a request with no canopy identity gets a 401 that says
which header to send, and a non-member gets the same 404 as a workspace that
does not exist — existence never leaks.
"""
from __future__ import annotations

from django.http import Http404, HttpResponse, JsonResponse
from django.views.decorators.http import require_GET

from apps.workspaces import permissions as perms
from apps.workspaces.models import Workspace

from . import services

_UNAUTHENTICATED = (
    "Send your canopy token: `Authorization: Bearer <token>`. The canopy plugin's "
    "headers helper does this — see docs/plugin-marketplace.md."
)


def _unauthorized() -> JsonResponse:
    resp = JsonResponse({"detail": _UNAUTHENTICATED}, status=401)
    resp["WWW-Authenticate"] = 'Bearer realm="canopy"'
    return resp


def _workspace(request, ws: str) -> Workspace:
    workspace = Workspace.objects.filter(slug=ws).first()
    if workspace is None or not perms.can(request.user, workspace, perms.READ):
        raise Http404("no such workspace")
    return workspace


@require_GET
def marketplace_json(request, ws: str):
    if not request.user.is_authenticated:
        return _unauthorized()
    workspace = _workspace(request, ws)
    resp = JsonResponse(services.marketplace(workspace), json_dumps_params={"indent": 2})
    # Per-person (membership-filtered) and changes on every merge.
    resp["Cache-Control"] = "private, no-cache"
    return resp


@require_GET
def plugin_archive(request, ws: str, agent: str, version: str):
    if not request.user.is_authenticated:
        return _unauthorized()
    workspace = _workspace(request, ws)
    archive = services.archive_for(workspace, agent, version)
    if archive is None:
        raise Http404("no such plugin version")
    resp = HttpResponse(bytes(archive.content), content_type="application/zip")
    resp["Content-Disposition"] = f'attachment; filename="{archive.plugin_name}-{archive.short_sha}.zip"'
    # One URL is one commit's bytes forever.
    resp["Cache-Control"] = "private, max-age=31536000, immutable"
    resp["ETag"] = f'"{archive.sha256}"'
    resp["X-Content-Type-Options"] = "nosniff"
    return resp

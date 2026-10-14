"""The agent catalog — a bare Django view, like the walkthrough stream.

Not a Ninja route: its address is fixed by what Claude Code fetches, it returns
raw JSON rather than an API envelope, and it must not become an MCP tool.

Auth is self-enforced here, which is why `LoginRequiredMiddleware` admits these
paths: its fallback for a signed-out request outside `/api/` is a 302 to Google
sign-in, which Claude Code would follow cross-origin and fail on. Here a request
with no canopy identity gets a 401 that says which header to send.

The retired per-workspace addresses (`/w/<ws>/marketplace.json` and the
`/w/<ws>/plugins/<agent>/<sha>.zip` archives) answer 410 with the new address,
so a stale registration fails loudly with the fix rather than as a bare 404.
"""
from __future__ import annotations

from django.http import JsonResponse
from django.views.decorators.http import require_GET

from . import services

_UNAUTHENTICATED = (
    "Send your canopy token: `Authorization: Bearer <token>`. The canopy plugin's "
    "headers helper does this — see docs/architecture/plugin-marketplace.md."
)


def _unauthorized() -> JsonResponse:
    resp = JsonResponse({"detail": _UNAUTHENTICATED}, status=401)
    resp["WWW-Authenticate"] = 'Bearer realm="canopy"'
    return resp


@require_GET
def marketplace_json(request):
    if not request.user.is_authenticated:
        return _unauthorized()
    resp = JsonResponse(services.catalog(request.user), json_dumps_params={"indent": 2})
    # Per-person (membership-filtered).
    resp["Cache-Control"] = "private, no-cache"
    return resp


def moved(request, **_):
    """410 for the retired per-workspace marketplace and archive URLs."""
    return JsonResponse({
        "detail": (f"Agent plugins moved to one catalog: register {services.catalog_url()} "
                   "(re-run /canopy:setup)."),
        "marketplace": services.MARKETPLACE_NAME,
        "url": services.catalog_url(),
    }, status=410)

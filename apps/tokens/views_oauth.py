"""`/oauth/client.json` and `/oauth/jwks.json` — canopy's public client identity.

Bare Django views rather than Ninja routes because the URL IS the identifier:
`client_id` is `{CANOPY_PUBLIC_BASE_URL}/oauth/client.json`, and a host
allowlists that exact string. A route under `/api/` would put canopy's identity
behind an API version and an OpenAPI tag it has nothing to do with.

Public by nature (a host must fetch both before it can trust anything) and
listed in `LoginRequiredMiddleware`'s allowlist. Neither holds anything that can
sign: the document names a URL, and the JWKS carries public halves only.
"""
from __future__ import annotations

from django.http import HttpRequest, JsonResponse
from django.views.decorators.http import require_GET

from . import client_identity

#: Short enough that a rotation propagates, long enough that a host is not
#: fetching per redemption. The contract caps a host's own cache at one hour.
_CACHE = "public, max-age=300"


def _unconfigured() -> JsonResponse:
    # 503, not 404: the route exists, this deployment is just not a client of
    # anyone yet — an operator reading a host's failure should see that.
    resp = JsonResponse(
        {"type": "about:blank", "title": "canopy is not configured as an OAuth client",
         "status": 503},
        status=503, content_type="application/problem+json")
    resp["Cache-Control"] = "no-store"
    return resp


@require_GET
def client_metadata(request: HttpRequest) -> JsonResponse:
    if not client_identity.configured():
        return _unconfigured()
    resp = JsonResponse(client_identity.client_metadata())
    resp["Cache-Control"] = _CACHE
    return resp


@require_GET
def jwks(request: HttpRequest) -> JsonResponse:
    if not client_identity.configured():
        return _unconfigured()
    resp = JsonResponse(client_identity.published_jwks())
    resp["Cache-Control"] = _CACHE
    return resp

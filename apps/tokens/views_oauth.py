"""`/oauth/client.json` and `/oauth/jwks.json` — canopy's public client identity.

Also, below, canopy-web's HOST surface for its own MCP: `/oauth/token`,
`/oauth/host/jwks.json` and the RFC 8414 / RFC 9728 documents.

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
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

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


# --- canopy-web as a HOST of its own MCP (apps/tokens/self_host.py) ------------
#
# The authorization server for the jwt-bearer grant ONLY, for ONE client —
# canopy itself. Public for the same reason as the two views above: the party
# redeeming must reach them before it holds anything. canopy's own redemption
# reaches the same functions in-process (`self_host.loopback_*`); these views
# are what anyone else — a conformance run, an operator's curl — sees.


def _host_unconfigured() -> JsonResponse:
    resp = JsonResponse(
        {"type": "about:blank", "title": "canopy-web is not configured as a host of its own MCP",
         "status": 503},
        status=503, content_type="application/problem+json")
    resp["Cache-Control"] = "no-store"
    return resp


def _host_document(build) -> JsonResponse:
    from . import self_host

    if not self_host.configured():
        return _host_unconfigured()
    resp = JsonResponse(build())
    resp["Cache-Control"] = _CACHE
    return resp


@require_GET
def host_jwks(request: HttpRequest) -> JsonResponse:
    """The HOST signing key's public half — what the `canopy-web` Connected
    site's JWKS URL names, and what verifies the ID-JAGs canopy signs for its
    own pages. Distinct from `/oauth/jwks.json` (the CLIENT key)."""
    from . import self_host

    return _host_document(lambda: self_host.config().jwks())


@require_GET
def authorization_server_metadata(request: HttpRequest, rest: str = "") -> JsonResponse:
    """RFC 8414 for canopy-web's issuer, at the RFC location (a deployment at
    the root of its host) and with any path suffix."""
    from . import self_host

    return _host_document(self_host.as_metadata)


@require_GET
def protected_resource_metadata(request: HttpRequest, rest: str = "") -> JsonResponse:
    """RFC 9728 for `/api/mcp/`."""
    from . import self_host

    return _host_document(self_host.pr_metadata)


@csrf_exempt
@require_POST
def token(request: HttpRequest) -> JsonResponse:
    """The jwt-bearer grant (RFC 7523 + private_key_jwt + DPoP). Nothing else is
    served here: canopy-web runs no other OAuth grant. Never logs the form."""
    from canopy_sdk import contract

    from . import self_host

    if request.POST.get("grant_type") != contract.JWT_BEARER_GRANT:
        status = 400
        body = {"error": "unsupported_grant_type",
                "error_description": "Only the jwt-bearer grant is served here."}
        headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    else:
        status, body, headers = self_host.handle_token_request(
            request.POST.dict(), request.headers.get(contract.DPOP_HEADER))
    resp = JsonResponse(body, status=status)
    for key, value in headers.items():
        resp[key] = value
    return resp

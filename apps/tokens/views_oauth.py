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
from django.views.decorators.http import require_GET, require_http_methods, require_POST

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


@require_http_methods(["GET", "OPTIONS"])
def authorization_server_metadata(request: HttpRequest, rest: str = "") -> JsonResponse:
    """RFC 8414 for canopy-web's issuer, at the RFC location (a deployment at
    the root of its host) and with any path suffix.

    One issuer, two kinds of login: a PERSON signing an MCP client in
    (authorization_code + PKCE, `mcp_oauth.py`) — always served — and canopy's
    own jwt-bearer grant for embedded pages (`self_host.py`), folded in when
    this deployment is configured as a host."""
    from . import mcp_oauth, self_host
    from .views_mcp_oauth import cors, issuer, preflight

    if request.method == "OPTIONS":
        return preflight()
    base = self_host.as_metadata() if self_host.configured() else {}
    resp = JsonResponse(mcp_oauth.merge_metadata(base, mcp_oauth.as_metadata_fields(issuer(request))))
    resp["Cache-Control"] = _CACHE
    return cors(resp)


@require_http_methods(["GET", "OPTIONS"])
def protected_resource_metadata(request: HttpRequest, rest: str = "") -> JsonResponse:
    """RFC 9728 for `/api/mcp/` — where an MCP client learns whom to sign in with."""
    from . import mcp_oauth, self_host
    from .views_mcp_oauth import cors, issuer, mcp_resource, preflight

    if request.method == "OPTIONS":
        return preflight()
    base = self_host.pr_metadata() if self_host.configured() else {}
    doc = mcp_oauth.merge_metadata(base, {
        "resource": mcp_resource(request),
        "authorization_servers": [issuer(request)],
        "bearer_methods_supported": ["header"],
        "scopes_supported": [mcp_oauth.USER_SCOPE],
        "resource_name": "canopy-web",
    })
    resp = JsonResponse(doc)
    resp["Cache-Control"] = _CACHE
    return cors(resp)


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def token(request: HttpRequest) -> JsonResponse:
    """The token endpoint for both logins: a person's MCP client
    (`authorization_code`, `refresh_token` — `mcp_oauth.py`) and canopy's own
    jwt-bearer grant (RFC 7523 + private_key_jwt + DPoP). Never logs the form."""
    from canopy_sdk import contract

    from . import mcp_oauth, self_host
    from .views_mcp_oauth import cors, preflight

    if request.method == "OPTIONS":
        return preflight()
    headers = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    grant_type = request.POST.get("grant_type")
    if grant_type in (mcp_oauth.AUTHORIZATION_CODE, mcp_oauth.REFRESH_TOKEN):
        try:
            status, body = 200, mcp_oauth.token_request(request.POST)
        except mcp_oauth.OAuthError as e:
            status, body = e.status, e.body()
    elif grant_type == contract.JWT_BEARER_GRANT:
        status, body, headers = self_host.handle_token_request(
            request.POST.dict(), request.headers.get(contract.DPOP_HEADER))
    else:
        status = 400
        body = {"error": "unsupported_grant_type",
                "error_description": f"grant_type {grant_type!r} is not served here."}
    resp = JsonResponse(body, status=status)
    for key, value in headers.items():
        resp[key] = value
    return cors(resp)


@csrf_exempt
@require_POST
def probe(request: HttpRequest) -> JsonResponse:
    """canopy's live probe of its own host half: a real ID-JAG for the dedicated
    probe user (`CANOPY_HOST_PROBE_USERNAME`), for canopy's own client only,
    authenticated exactly like the token endpoint. 404 while there is no probe
    user. Never logs the form."""
    from canopy_sdk import contract

    from . import self_host

    status, body, headers = self_host.handle_probe_request(
        request.POST.dict(), request.headers.get(contract.DPOP_HEADER))
    resp = JsonResponse(body, status=status)
    for key, value in headers.items():
        resp[key] = value
    return resp

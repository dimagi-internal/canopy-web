"""The browser + client legs of signing in to canopy's MCP (`mcp_oauth.py`).

`/oauth/register` is public (a client registers before anyone signs in, and
registering grants nothing). `/oauth/authorize` is NOT public: an anonymous
visitor is sent to the Google sign-in by `LoginRequiredMiddleware` and comes
back here, so the consent page always knows who is approving. The token
endpoint lives in `views_oauth.token`, which serves the jwt-bearer grant too.
"""
from __future__ import annotations

import json

from django.core.cache import cache
from django.http import HttpRequest, HttpResponse, HttpResponseRedirect, JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from apps.common.script_prefix import self_full_path
from apps.common.views_debug import is_machine

from . import mcp_oauth

#: Registrations per client IP per hour. Registration grants nothing, but it
#: writes a row, so an unauthenticated loop must not be able to fill a table.
REGISTER_LIMIT = 30


def issuer(request: HttpRequest) -> str:
    """The issuer: the deployment's public base, or — where none is configured
    (dev, tests) — this request's own origin and script prefix."""
    from . import client_identity

    base = client_identity.public_base()
    if base:
        return base
    return request.build_absolute_uri(request.META.get("SCRIPT_NAME", "") or "/").rstrip("/")


def mcp_resource(request: HttpRequest) -> str:
    return f"{issuer(request)}/api/mcp/"


def cors(response: HttpResponse) -> HttpResponse:
    """The discovery documents, registration and token endpoints are called
    from browser-based MCP clients (the MCP Inspector) too. None of them reads
    a cookie, so any origin may call them."""
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response["Access-Control-Allow-Headers"] = "Authorization, Content-Type, MCP-Protocol-Version"
    return response


def preflight() -> HttpResponse:
    return cors(HttpResponse(status=204))


def _client_ip(request: HttpRequest) -> str:
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    return forwarded.split(",")[0].strip() or request.META.get("REMOTE_ADDR", "")


@csrf_exempt
@require_http_methods(["POST", "OPTIONS"])
def register(request: HttpRequest) -> HttpResponse:
    """RFC 7591 dynamic client registration, for public (PKCE) clients."""
    if request.method == "OPTIONS":
        return preflight()
    key = f"mcp-oauth:register:{_client_ip(request)}"
    cache.add(key, 0, timeout=3600)
    if cache.incr(key) > REGISTER_LIMIT:
        return cors(JsonResponse({"error": "slow_down",
                                  "error_description": "Too many registrations; try later."},
                                 status=429))
    try:
        metadata = json.loads(request.body or b"{}")
        client = mcp_oauth.register_client(metadata)
    except json.JSONDecodeError:
        return cors(JsonResponse({"error": "invalid_client_metadata",
                                  "error_description": "The body must be JSON."}, status=400))
    except mcp_oauth.OAuthError as e:
        return cors(JsonResponse(e.body(), status=e.status))
    return cors(JsonResponse(mcp_oauth.client_registration_response(client), status=201))


@require_http_methods(["GET", "POST"])
def authorize(request: HttpRequest) -> HttpResponse:
    """The consent page. GET shows it; POST is the person's answer."""
    if not request.user.is_authenticated:
        # `LoginRequiredMiddleware` does this too; the consent page must not
        # depend on it (it is switchable), since an approval with no person
        # behind it would mint a token for nobody — or crash trying.
        from urllib.parse import urlencode

        from django.conf import settings

        return HttpResponseRedirect(
            f"{settings.LOGIN_URL}?{urlencode({'next': self_full_path(request)})}")
    params = request.POST if request.method == "POST" else request.GET
    try:
        req = mcp_oauth.parse_authorize(params, resource=mcp_resource(request))
    except mcp_oauth.InvalidClientOrRedirect as e:
        # Never redirect to a URI we could not verify — that is an open redirect.
        return render(request, "tokens/mcp_authorize_error.html", {"reason": str(e)}, status=400)
    except mcp_oauth.AuthorizeError as e:
        return _Redirect(e.redirect())

    if is_machine(request):
        # A session minted from a token is not a person at a browser; it must
        # not approve a new client on anyone's behalf.
        return render(request, "tokens/mcp_authorize_error.html",
                      {"reason": "Sign in to canopy in your browser to approve this."}, status=403)

    if request.method == "GET":
        return render(request, "tokens/mcp_authorize.html", {
            "client_name": req.client.display_name,
            "redirect_host": _redirect_host(req.redirect_uri),
            "form_action": self_full_path(request).split("?", 1)[0],
            "params": {k: params.get(k, "") for k in (
                "client_id", "redirect_uri", "response_type", "code_challenge",
                "code_challenge_method", "state", "scope", "resource")},
        })

    if request.POST.get("decision") != "approve":
        return _Redirect(req.redirect(error="access_denied",
                                                 error_description="The person declined."))
    code = mcp_oauth.issue_code(req, request.user)
    return _Redirect(req.redirect(code=code))


def _redirect_host(uri: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(uri)
    return parts.netloc or f"{parts.scheme}:"


class _Redirect(HttpResponseRedirect):
    """A redirect that may target an app's private-use scheme (`cursor://…`).
    Django's refuses anything but http/https/ftp; the URI was already checked
    against the client's registration and `redirect_uri_allowed`."""

    def __init__(self, redirect_to, *args, **kwargs):
        from urllib.parse import urlsplit

        scheme = urlsplit(redirect_to).scheme
        self.allowed_schemes = ["http", "https", scheme] if scheme else ["http", "https"]
        super().__init__(redirect_to, *args, **kwargs)

"""Views a Django host mounts."""
from __future__ import annotations

import logging

from django.contrib.auth.decorators import login_required
from django.http import Http404, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .. import contract
from ..host.client_keys import ClientKeyResolver
from ..host.config import HostNotConfigured
from ..host.grant import GrantHandler, GrantRefused
from ..host.metadata import authorization_server_metadata, protected_resource_metadata
from ..host.signing import MintFailed, arrival_payload, mint_contact_token
from . import conf
from .stores import DjangoJtiStore, DjangoTokenStore

log = logging.getLogger("canopy_sdk.django")


def _json(body: dict, status: int = 200, headers: dict | None = None) -> JsonResponse:
    response = JsonResponse(body, status=status)
    for key, value in (headers or {}).items():
        response[key] = value
    return response


def grant_handler() -> GrantHandler:
    from django.core.cache import cache

    return GrantHandler(
        conf.get_host_config(),
        jti_store=DjangoJtiStore(),
        token_store=DjangoTokenStore(),
        client_keys=ClientKeyResolver(cache=cache),
        subject_active=conf.subject_active(),
    )


def redeem(request) -> JsonResponse:
    """The jwt-bearer grant for this request. Never logs the form."""
    try:
        handler = grant_handler()
    except HostNotConfigured:
        refused = GrantRefused("unsupported_grant_type", "This server does not accept the jwt-bearer grant.")
        return _json(refused.body(), refused.status, refused.headers())
    try:
        result = handler.handle(request.POST, request.headers.get(contract.DPOP_HEADER))
    except GrantRefused as refused:
        return _json(refused.body(), refused.status, refused.headers())
    return _json(result.body(), 200, result.headers())


@csrf_exempt
@require_POST
def token_endpoint(request):
    """A token endpoint that serves ONLY the jwt-bearer grant."""
    if request.POST.get("grant_type") != contract.JWT_BEARER_GRANT:
        refused = GrantRefused("unsupported_grant_type", "Only the jwt-bearer grant is served here.")
        return _json(refused.body(), refused.status, refused.headers())
    return redeem(request)


def jwt_bearer_view(fallback):
    """Put the jwt-bearer grant in front of an existing token view.

    For a host that already runs django-oauth-toolkit::

        from oauth2_provider.views import TokenView
        path("o/token/", jwt_bearer_view(TokenView.as_view()))

    The jwt-bearer grant is answered here; every other grant reaches
    ``fallback`` unchanged. Issued tokens live in this app's own table, never
    the toolkit's — which authenticates the host's REST API with any live row
    in its table.
    """

    @csrf_exempt
    def view(request, *args, **kwargs):
        if request.method == "POST" and request.POST.get("grant_type") == contract.JWT_BEARER_GRANT:
            return redeem(request)
        return fallback(request, *args, **kwargs)

    return view


@require_GET
def jwks(request):
    """The host's public key(s), for canopy to verify assertions and ID-JAGs.
    Unauthenticated on purpose: a public key is public."""
    try:
        return _json(conf.get_host_config().jwks())
    except HostNotConfigured:
        return _json({"keys": []}, 503)
    except Exception:  # noqa: BLE001 - a malformed key is a deployment fault
        log.exception("CANOPY_HOST['SIGNING_KEY'] could not be read")
        return _json({"keys": []}, 503)


def _grant_config_or_404():
    """The host config while the grant is on; 404 otherwise — a server does not
    advertise a way in it does not offer, and "not configured" is never a 500."""
    try:
        config = conf.get_host_config()
    except HostNotConfigured:
        raise Http404("the jwt-bearer grant is not configured here") from None
    except Exception:  # noqa: BLE001 - a malformed key is a deployment fault
        log.exception("CANOPY_HOST['SIGNING_KEY'] could not be read")
        raise Http404("the jwt-bearer grant is not configured here") from None
    if not config.grant_enabled:
        raise Http404("the jwt-bearer grant is not configured here")
    return config


@require_GET
def authorization_server_metadata_view(request):
    """RFC 8414, for a host that serves no metadata document of its own."""
    return _json(authorization_server_metadata(_grant_config_or_404()))


@require_GET
def protected_resource_metadata_view(request):
    """RFC 9728, for a host that serves no metadata document of its own."""
    return _json(protected_resource_metadata(_grant_config_or_404()))


@login_required
@require_POST
def panel_token(request):
    """Mint a canopy token for the person whose session this is.

    The subject is ``request.user`` and can be nothing else: the request body is
    never read. The one thing read is ``?page=``, which names the page the panel
    is on — in signed mode the page token it was rendered with (this host's own
    signature over the route and this user), in key mode a page key or path the
    browser names. Either way it can only SELECT among the pages registered in
    ``PAGE_SCOPES``; unknown, forged, expired or another user's means no grant,
    and the mint goes ahead without one.
    """
    if not conf.is_configured():
        return _json({"error": "the canopy panel is not configured here"}, 503)
    try:
        config = conf.get_host_config()
        scopes = conf.page_scopes(request.GET.get("page"), request.user)
    except Exception:  # noqa: BLE001 - HostNotConfigured, a malformed key or registry
        log.exception("CANOPY_HOST could not be read for the panel")
        return _json({"error": "the canopy panel is not configured here"}, 503)
    try:
        payload = arrival_payload(config, conf.subject_for_user(request.user),
                                  scopes=scopes, agent_slug=conf.agent_slug(),
                                  **conf.user_claims(request.user))
        vouched = mint_contact_token(config, payload)
    except MintFailed as exc:
        # canopy's own words name the cause; logged, not returned — the visitor
        # cannot act on them.
        log.warning("canopy declined to mint a token for user %s: %s", request.user.pk, exc)
        return _json({"error": "could not reach the agent service"}, 502)
    # Only what a browser needs: the token, its expiry, and `kind` (a user or a
    # contact), which canopy-client routes on.
    return _json({"token": vouched["token"], "expires_at": vouched.get("expires_at", ""),
                  "kind": vouched.get("kind", "contact")})

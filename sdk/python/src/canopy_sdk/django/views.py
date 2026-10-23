"""Views a Django host mounts."""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.urls import NoReverseMatch, reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from .. import contract
from ..host.client_keys import ClientKeyResolver
from ..host.config import HostNotConfigured
from ..host.grant import GrantHandler, GrantRefused
from ..host.metadata import authorization_server_metadata, protected_resource_metadata
from ..host.probe import ProbeDisabled, ProbeHandler
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


def probe_handler() -> ProbeHandler:
    from django.core.cache import cache

    return ProbeHandler(
        conf.get_host_config(),
        jti_store=DjangoJtiStore(),
        client_keys=ClientKeyResolver(cache=cache),
        subject_active=conf.subject_active(),
    )


@csrf_exempt
@require_POST
def probe_endpoint(request):
    """canopy's live probe: a real ID-JAG for this host's configured probe
    principal (``CANOPY_HOST["PROBE"]``), for canopy's client ONLY. 404 while no
    probe is configured. Never logs the form."""
    try:
        result = probe_handler().handle(request.POST, request.headers.get(contract.DPOP_HEADER))
    except (ProbeDisabled, HostNotConfigured):
        return _json({"error": "not_found", "error_description": "No probe is configured here."}, 404,
                     {"Cache-Control": "no-store"})
    except GrantRefused as refused:
        return _json(refused.body(), refused.status, refused.headers())
    except Exception:  # noqa: BLE001 - a malformed key is a deployment fault, not a 500 with a trace
        log.exception("the canopy probe endpoint could not answer")
        return _json({"error": "server_error", "error_description": "The probe could not run."}, 500,
                     {"Cache-Control": "no-store"})
    return _json(result.body(), 200, result.headers())


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


def _origin_of(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def site_facts(request, config) -> dict:
    """``site.json``'s ``site`` block, from what this host already knows:

    * ``origins`` — ``CANOPY_HOST["ORIGINS"]``, else the origin of ``ISSUER``
      (the public address the host signs as), else the origin this request
      reached (the one canopy fetched the document from);
    * ``jwks_url`` — ``CANOPY_HOST["JWKS_URL"]``, else the SDK's ``jwks.json``
      route on that origin;
    * ``issuer`` / ``mcp_resource`` — ``ISSUER`` / ``RESOURCE`` (blank while the
      grant is off).
    """
    overrides = conf.site_facts_overrides()
    base = _origin_of(config.issuer) or _origin_of(request.build_absolute_uri("/"))
    jwks_url = overrides["jwks_url"]
    if not jwks_url and base:
        try:
            jwks_url = base + reverse("canopy_host:jwks")
        except NoReverseMatch:
            # Mounted outside the `canopy_host` namespace: beside this document.
            jwks_url = base + request.path.rsplit("/", 1)[0] + "/jwks.json"
    return {"origins": overrides["origins"] or ([base] if base else []), "jwks_url": jwks_url,
            "issuer": config.issuer if config.grant_enabled else "",
            "mcp_resource": config.resource if config.grant_enabled else ""}


@require_GET
def site_document(request):
    """``site.json``: what this site asks of canopy's agents, and its own facts
    (origins, keys, issuer, MCP server), signed with the host's key
    (``canopy_sdk.site``). canopy reads it beside ``jwks.json``, or at
    ``/.well-known/canopy-site.json`` when a host mounts this view there too.
    Unauthenticated, like the JWKS: it names pages, agents and public URLs,
    never a person."""
    from ..site import MEDIA_TYPE, SiteDocumentError, sign_site_document
    from ..site import site_document as build

    try:
        config = conf.get_host_config()
        if not config.app_name:
            raise HostNotConfigured("CANOPY_HOST['APP_NAME'] is not set")
        needs = conf.site_needs()
        token = sign_site_document(config.signing_key, build(
            config.app_name, pages=conf.site_pages(needs.get("titles")),
            sensitive_data=needs.get("sensitive_data"), uses=needs.get("uses"),
            site=site_facts(request, config)))
    except HostNotConfigured:
        return _json({"error": "not_configured"}, 503)
    except SiteDocumentError as exc:
        log.error("CANOPY_HOST's site document is not valid: %s", exc)
        return _json({"error": exc.code, "error_description": exc.message}, 503)
    except Exception:  # noqa: BLE001 - a malformed key or a failing hook is a deployment fault
        log.exception("canopy/site.json could not be built")
        return _json({"error": "server_error"}, 503)
    response = HttpResponse(token, content_type=MEDIA_TYPE)
    response["Cache-Control"] = "max-age=60"
    return response


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

    A page that chose its agent (and requirements) has them read from the same
    place as its scopes, and signed into the assertion: the token is BOUND to
    that agent. Any other mint gets ``AGENT_SLUG``, unbound, and the site's
    requirements only.
    """
    if not conf.is_configured():
        return _json({"error": "the canopy panel is not configured here"}, 503)
    try:
        config = conf.get_host_config()
        page = conf.page_for(request.GET.get("page"), request.user)
    except Exception:  # noqa: BLE001 - HostNotConfigured, a malformed key or registry
        log.exception("CANOPY_HOST could not be read for the panel")
        return _json({"error": "the canopy panel is not configured here"}, 503)
    chosen = page.agent if page is not None else ""
    try:
        payload = arrival_payload(config, conf.subject_for_user(request.user),
                                  scopes=page.scopes if page is not None else (),
                                  agent_slug=chosen or conf.agent_slug(), bind_agent=bool(chosen),
                                  runner_requirements=(page.runner_requirements
                                                       if page is not None else ()),
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

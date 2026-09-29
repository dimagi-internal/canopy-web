"""canopy-web as a HOST: agents on canopy's own pages call canopy's own MCP as the visitor.

Host grant contract v1 (`docs/architecture/host-grant-contract.md`) has two
sides, and canopy-web already plays the consumer: it redeems a host's ID-JAG
(`host_grants.py`) and calls the host's MCP through its gateway
(`host_gateway.py`). Nothing in the contract assumes the host is not canopy, so
canopy-web plays the HOST as well, for exactly one client — itself — and one
resource — its own `/api/mcp/`. The pieces are the SDK's (`canopy_sdk.host`,
`canopy_sdk.django` stores), configured here:

* **An authorization server for the jwt-bearer grant only.** `GrantHandler` at
  `{base}/oauth/token`, accepting ONLY canopy's own client id, authenticated by
  `private_key_jwt` against canopy's own published client keys, DPoP-bound.
  RFC 8414 / RFC 9728 documents from `authorization_server_metadata` /
  `protected_resource_metadata`; the signing key's public half at
  `{base}/oauth/host/jwks.json`.
* **The resource side.** `ResourceVerifier` behind the SDK's `DPoPGate` in front
  of `/api/mcp/` (`apps/mcp/delegation.py`). A delegated token is an ADDITIONAL
  credential there: PATs and caller tokens are untouched. It runs tools AS the
  visitor (their own ACL, the same service functions a PAT reaches) and only the
  tools its scopes map to (`SCOPE_TOOLS`).
* **The issuer side.** When canopy's own widget mints its token
  (`POST /api/embed/token`), and the page it is on is registered in
  `PAGE_SCOPES`, canopy signs an ID-JAG for that page's scopes and redeems it
  through the NORMAL consumer path (`host_grants.redeem`): the ID-JAG is checked
  against the Connected site's registered keys, the token endpoint is
  discovered from metadata, the grant is POSTed with a real client assertion
  and DPoP proof, and the host half verifies all of it — no step skipped.

**In-process, not over the network.** The documents and the token endpoint a
request to canopy's OWN URLs would reach are answered by `loopback_get` /
`loopback_post`, which `outbound.py` and `jwks.py` consult before going out. The
same functions serve the public views, so this is not a second implementation
of anything — it is the same handler without the round trip through the load
balancer. (Which matters on labs: canopy is served under `/canopy`, so the
RFC 8414 location for its issuer, `/.well-known/oauth-authorization-server/canopy`,
sits at the ROOT of a host the ALB routes to connect-labs.) The gateway's MCP
call is still real HTTP to `{base}/api/mcp/`, through the real gate.

**Pages, and why the browser names one.** canopy's pages are a single-page app:
the server never renders a route, so it cannot observe which one is on screen,
and a server-signed page token (the SDK's SIGNED mode, `PageTokens`) would only
sign whatever the browser asked for. So canopy uses the SDK's KEY mode
(`canopy_sdk.host.PageRegistry`): the widget names a page KEY, and everything
that matters is decided here: an unregistered key gets
no grant; a registered one gets the scopes this registry says, never scopes the
browser sent; every scope is read-only; the tools run as the visitor, so they
reach no more than the visitor's own ACL already does; and the gateway narrows
each call again to the page's declared `backing_tool`. A browser that names the
wrong page picks among read-only views of its own data, nothing more.

**Probed like any other host.** `/oauth/probe` is the SDK's `ProbeHandler` for
canopy's own client only: a real ID-JAG for the dedicated `canopy-probe` user
(`CANOPY_HOST_PROBE_USERNAME`; no membership, so `list_insights` reads nothing),
which `live_probe.py` redeems and uses exactly as it does for a connected site.

**Off until configured.** Needs `CANOPY_HOST_SIGNING_KEY` (Ed25519 or P-256 PEM;
"PLACEHOLDER" reads as unset; dev and tests generate one per process) AND
canopy's client keys (`client_identity.configured()`), and a grant is issued only
for the Connected site whose `host_issuer` / `host_mcp_resource` name THIS
deployment and that shows its panel on canopy's own pages.
"""
from __future__ import annotations

import logging
import threading

from canopy_sdk import contract, fetch
from canopy_sdk.host import (
    ClientKeyResolver, GrantHandler, GrantRefused, HostConfig, HostNotConfigured, PageRegistry,
    ProbeDisabled, ProbeHandler, ProbeIdentity, ResourceVerifier, authorization_server_metadata,
    issue_id_jag, protected_resource_metadata,
)
from django.conf import settings

from . import client_identity

log = logging.getLogger(__name__)

#: What each scope unlocks at canopy's own MCP — THE only place a delegated
#: token's reach is decided. Read-only tools only: a scope that could write is a
#: decision for a later version, taken on purpose.
SCOPE_TOOLS: dict[str, tuple[str, ...]] = {
    "insights:read": ("list_insights",),
    "items:read": ("list_items",),
    "skills:read": ("skill_history", "skill_revision_diff"),
}

#: Page key → the scopes a visitor on that page grants. Keys are what the
#: widget sends (`frontend/src/widget/grantPage.ts` derives them from the
#: route); every page here declares its selection with `usePageState` and a
#: `backing_tool` among its scope's tools, because the gateway unlocks only the
#: page's backing tool — a page with none would unlock nothing.
PAGE_SCOPES: dict[str, tuple[str, ...]] = {
    "insights": ("insights:read",),               # /insights — backing_tool list_insights
    "agent.inbox": ("items:read",),               # /w/:ws/agents/:slug/inbox — list_items
    "agent.skill_history": ("skills:read",),      # /w/:ws/agents/:slug/skills/history — skill_history
}

#: The SDK's key-mode registry over the two maps above. Built at import, so a
#: page naming a scope canopy does not offer — or a scope that is not read-only
#: — fails at startup rather than as a grant that silently carries nothing.
PAGES = PageRegistry(PAGE_SCOPES, scope_tools=SCOPE_TOOLS)

_lock = threading.Lock()
_ephemeral: dict[str, object] = {}


# --- identity ---------------------------------------------------------------------


def issuer() -> str:
    """This deployment's issuer: its public base URL."""
    return client_identity.public_base()


def resource() -> str:
    """canopy's own MCP server, as its RFC 9728 `resource`."""
    return f"{issuer()}/api/mcp/"


def token_endpoint() -> str:
    return f"{issuer()}/oauth/token"


def probe_endpoint() -> str:
    """canopy's live probe of its OWN host half (`canopy_sdk.host.ProbeHandler`)."""
    return f"{issuer()}/oauth/probe"


#: canopy-web's own probe: the one read-only call the live probe makes as the
#: dedicated probe user, and a tool outside that scope the MCP must refuse.
PROBE_SCOPE = "insights:read"
PROBE_TOOL = "list_insights"
PROBE_DENIED_TOOL = "list_items"
PROBE_PAGE = "insights"


def probe_user():
    """The dedicated probe user (`CANOPY_HOST_PROBE_USERNAME`, created by
    `tokens/0026_probe_user`), or None — which turns the probe off. It must be
    active, and it holds no membership, so `list_insights` runs as a real user
    and returns nothing: the call is meaningful (the whole chain ran, as that
    user, within its scope) without the probe reading anyone's data."""
    from django.contrib.auth import get_user_model

    username = (getattr(settings, "CANOPY_HOST_PROBE_USERNAME", "") or "").strip()
    if not username:
        return None
    return get_user_model().objects.filter(username=username, is_active=True).first()


def probe_identity() -> ProbeIdentity | None:
    user = probe_user()
    if user is None:
        return None
    return ProbeIdentity(endpoint=probe_endpoint(), subject=str(user.pk), scope=PROBE_SCOPE,
                         tool=PROBE_TOOL, arguments={"limit": 1}, denied_tool=PROBE_DENIED_TOOL,
                         page=PROBE_PAGE)


def jwks_url() -> str:
    """Where the HOST signing key's public half is published — what the
    `canopy-web` Connected site's `jwks_url` names. Distinct from
    `/oauth/jwks.json`, which is canopy's CLIENT key."""
    return f"{issuer()}/oauth/host/jwks.json"


def _signing_key():
    from cryptography.hazmat.primitives import serialization

    pem = (getattr(settings, "CANOPY_HOST_SIGNING_KEY", "") or "").strip()
    if pem and pem != "PLACEHOLDER":
        return serialization.load_pem_private_key(pem.encode(), password=None)
    if not getattr(settings, "CANOPY_OAUTH_EPHEMERAL_KEYS", False):
        return None
    with _lock:
        if "host" not in _ephemeral:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            _ephemeral["host"] = Ed25519PrivateKey.generate()
        return _ephemeral["host"]


def configured() -> bool:
    """Whether canopy-web can act as a host at all: its host signing key AND its
    client keys (the only client it grants to is itself)."""
    try:
        return _signing_key() is not None and client_identity.configured()
    except Exception:  # noqa: BLE001 - a malformed key is "not configured", loudly in config()
        log.warning("CANOPY_HOST_SIGNING_KEY could not be read", exc_info=True)
        return False


def config(*, with_probe: bool = False) -> HostConfig:
    """The SDK's `HostConfig` for canopy-web-as-host. Raises `HostNotConfigured`.

    `with_probe` looks the probe user up (a database read), so only the probe
    endpoint and the metadata ask for it: the DPoP gate builds its verifier
    from here in ASYNC context, where a query is not allowed."""
    key = _signing_key()
    if key is None:
        raise HostNotConfigured("CANOPY_HOST_SIGNING_KEY is not set")
    if not client_identity.configured():
        raise HostNotConfigured("canopy has no OAuth client keys, and it is the only client")
    return HostConfig(
        signing_key=key,
        canopy_base_url=issuer(),
        app_name="canopy-web",
        issuer=issuer(),
        resource=resource(),
        token_endpoint=token_endpoint(),
        canopy_client_id=client_identity.client_id(),
        scope_tools=SCOPE_TOOLS,
        probe=probe_identity() if with_probe else None,
    )


def is_self_site(app) -> bool:
    """Whether this Connected site's grant settings name THIS deployment."""
    return bool(app is not None and app.issues_host_grants()
                and contract.normalize_url(app.host_issuer) == contract.normalize_url(issuer())
                and contract.normalize_url(app.host_mcp_resource) == contract.normalize_url(resource()))


# --- the host half ------------------------------------------------------------------


def subject_active(subject: str) -> bool:
    """A grant's subject is the visitor's canopy user id; it must still be an
    active account. (What they may SEE is their own ACL, applied by each tool.)"""
    from django.contrib.auth import get_user_model

    try:
        return get_user_model().objects.filter(pk=int(subject), is_active=True).exists()
    except (TypeError, ValueError):
        return False


def _own_client_document(url: str) -> dict:
    """canopy's client metadata and JWKS, as the host half fetches them to
    authenticate the client — answered from the same functions the public
    `/oauth/client.json` and `/oauth/jwks.json` views serve."""
    if url == client_identity.client_id():
        return client_identity.client_metadata()
    if url == client_identity.jwks_uri():
        return client_identity.published_jwks()
    raise fetch.FetchError("canopy-web grants only to canopy's own client")


def grant_handler() -> GrantHandler:
    from canopy_sdk.django.stores import DjangoJtiStore, DjangoTokenStore

    return GrantHandler(
        config(), jti_store=DjangoJtiStore(), token_store=DjangoTokenStore(),
        client_keys=ClientKeyResolver(fetch_json=_own_client_document),
        subject_active=subject_active,
    )


def resource_verifier() -> ResourceVerifier:
    from canopy_sdk.django.stores import DjangoJtiStore, DjangoTokenStore

    return ResourceVerifier(config(), token_store=DjangoTokenStore(), replay_store=DjangoJtiStore(),
                            subject_active=subject_active)


def handle_token_request(form, dpop: str | None) -> tuple[int, dict, dict]:
    """The token endpoint: `(status, body, headers)`. Never logs the form."""
    try:
        handler = grant_handler()
    except HostNotConfigured:
        refused = GrantRefused("unsupported_grant_type", "This server does not accept the jwt-bearer grant.")
        return refused.status, refused.body(), refused.headers()
    try:
        result = handler.handle(form, dpop)
    except GrantRefused as refused:
        return refused.status, refused.body(), refused.headers()
    return 200, result.body(), result.headers()


def probe_handler() -> ProbeHandler:
    from canopy_sdk.django.stores import DjangoJtiStore

    return ProbeHandler(config(with_probe=True), jti_store=DjangoJtiStore(),
                        client_keys=ClientKeyResolver(fetch_json=_own_client_document),
                        subject_active=subject_active)


def handle_probe_request(form, dpop: str | None) -> tuple[int, dict, dict]:
    """The probe endpoint: `(status, body, headers)`. 404 while no probe user
    exists (or canopy is not a host). Never logs the form."""
    no_store = {"Cache-Control": "no-store", "Pragma": "no-cache"}
    try:
        result = probe_handler().handle(form, dpop)
    except (ProbeDisabled, HostNotConfigured):
        return 404, {"error": "not_found", "error_description": "No probe is configured here."}, no_store
    except GrantRefused as refused:
        return refused.status, refused.body(), refused.headers()
    return 200, result.body(), result.headers()


def as_metadata() -> dict:
    return authorization_server_metadata(config(with_probe=True))


def pr_metadata() -> dict:
    return protected_resource_metadata(config())


# --- loopback: a request to canopy's own URLs, answered by canopy ------------------


def _loopback_urls() -> set[str]:
    return {contract.normalize_url(u) for u in (
        contract.metadata_url(issuer()), contract.protected_resource_metadata_url(resource()),
        jwks_url(), token_endpoint(), probe_endpoint())}


def is_loopback_url(url: str) -> bool:
    """Whether a request to `url` is answered in-process. The MCP resource is
    deliberately NOT one: the gateway's call to it is real HTTP."""
    return configured() and contract.normalize_url(url) in _loopback_urls()


def loopback_get(url: str) -> dict | None:
    """The document at `url` if it is one of canopy-web's own host documents,
    else None (go out as usual)."""
    if not configured():
        return None
    target = contract.normalize_url(url)
    if target == contract.normalize_url(contract.metadata_url(issuer())):
        return as_metadata()
    if target == contract.normalize_url(contract.protected_resource_metadata_url(resource())):
        return pr_metadata()
    if target == contract.normalize_url(jwks_url()):
        return config().jwks()
    return None


def loopback_post(url: str, data, headers) -> tuple[int, dict, dict] | None:
    """The token (or probe) endpoint's answer if `url` is canopy-web's own, else None."""
    if not configured():
        return None
    target = contract.normalize_url(url)
    dpop = (headers or {}).get(contract.DPOP_HEADER)
    if target == contract.normalize_url(token_endpoint()):
        return handle_token_request(dict(data), dpop)
    if target == contract.normalize_url(probe_endpoint()):
        return handle_probe_request(dict(data), dpop)
    return None


# --- the issuer side: a grant for a visitor on one of canopy's pages ----------------


def scopes_for_page(page: str) -> tuple[str, ...]:
    return PAGES.scopes_for(page)


def grant_for_visitor(app, user, page: str) -> bool:
    """Issue and redeem a grant for `user` on `page`, if this site and page get
    one. Returns whether a grant is now held. Never raises: no grant is a
    working panel (the agent simply cannot act as the visitor on canopy)."""
    from . import host_grants

    scopes = scopes_for_page(page)
    if not scopes or user is None or not getattr(user, "is_authenticated", False):
        return False
    if not is_self_site(app) or not configured():
        return False
    subject = str(user.pk)
    try:
        id_jag = issue_id_jag(config(), subject, scopes)
        grant = host_grants.redeem(app, id_jag, subject=subject, user=user)
    except host_grants.HostGrantError as exc:
        host_grants.record_outcome(app, ok=False, subject=subject, code=exc.code, detail=exc.message)
        return False
    except Exception as exc:  # noqa: BLE001 - never fail the mint; the type only, never a token
        log.warning("canopy-web could not grant itself access for user %s: %s", user.pk, type(exc).__name__)
        host_grants.record_outcome(app, ok=False, subject=subject, code="error", detail=type(exc).__name__)
        return False
    host_grants.record_outcome(app, ok=True, subject=subject, scope=grant.scope)
    return True


def _reset_for_tests() -> None:
    with _lock:
        _ephemeral.clear()

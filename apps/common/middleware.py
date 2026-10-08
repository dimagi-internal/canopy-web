"""Authentication middleware: default-deny with an allowlist.

Authenticated callers (via session cookie OR Personal Access Token via
`apps.tokens.middleware.BearerTokenAuthMiddleware`) bypass this gate
automatically — `request.user.is_authenticated` becomes True for both.
"""
import re
from urllib.parse import urlencode

from django.conf import settings
from django.http import JsonResponse
from django.shortcuts import redirect

from apps.common.script_prefix import self_full_path
from config import public_site
from config.views import FLAT_ARTIFACT_PATH

PUBLIC_PATH_PREFIXES = (
    "/accounts/",            # allauth login/logout/callback
    "/admin/",               # Django admin has its own auth
    "/health/",              # health check for Cloud Run
    "/static/",              # static assets
    # The built SPA's content-hashed bundles — the same class of thing as
    # /static/, and already referenced by the login page's own HTML, so gating
    # them protects nothing. Gating them DID break things: an expired session
    # turned every <script> request into a 302 to Google, so the browser was
    # handed a sign-in page where it expected a module and rendered a white
    # page. Reproduced 2026-09-08.
    "/assets/",              # built SPA bundles
    "/api/csrf/",            # bootstraps CSRF cookie before login
    "/api/openapi.json",      # openapi-typescript fetches the schema
    "/api/docs/",             # Scalar HTML
    "/api/redoc/",            # Redoc HTML
    "/api/mcp/",              # FastMCP server — auth via Bearer in the request
    # NOTE: /auth/cli/authorize/ is deliberately NOT listed. It used to be, so
    # that the view's own @login_required would bounce and preserve
    # ?cb/?state/?label — but Django's decorator builds ?next= from
    # request.get_full_path(), which drops the /canopy script prefix (see
    # apps.common.script_prefix), so a first-time operator landed on Connect
    # Labs after signing in. This middleware's own bounce below preserves the
    # query string too AND keeps the prefix, so it is the correct handler.
    # The embed shell. Anonymous by necessity: the delegated token arrives
    # later over postMessage (v2 spec §3), never on this document request, so a
    # login bounce here would mean the widget could not load for anyone — in an
    # iframe it would silently render Google's sign-in page instead. The view
    # self-enforces the part that matters (it is served only for a registered,
    # unrevoked app that has valid frame origins, and carries no data).
    "/embed/",
    "/api/auth/contact-token",   # auth=None — self-enforces by verifying a signed assertion
    # canopy's OAuth CLIENT identity — the metadata document whose URL is its
    # client_id, and the public client key it names. A host reads both before
    # it trusts a redemption, so they cannot sit behind a login; neither holds
    # anything that can sign (apps/tokens/views_oauth.py). Exact paths.
    "/oauth/client.json",
    "/oauth/jwks.json",
    # The OAuth token endpoint and discovery documents. The token endpoint
    # self-enforces every grant it serves: canopy's own jwt-bearer grant
    # (private_key_jwt + DPoP + a signed grant, one allowed client —
    # apps/tokens/self_host.py) and a person's MCP login (a one-time code plus
    # its PKCE verifier, or a refresh token — apps/tokens/mcp_oauth.py). The
    # host key's PUBLIC half sits here too. Nothing here can sign or read
    # tenant data without one of those.
    "/oauth/token",
    # An MCP client registers itself before anyone signs in; registering grants
    # nothing (apps/tokens/mcp_oauth.py). `/oauth/authorize` is deliberately NOT
    # here: the consent page needs a signed-in person, so this middleware sends
    # an anonymous visitor to sign in and back.
    "/oauth/register",
    # canopy's live probe of its OWN host half: a real ID-JAG for the dedicated
    # probe user, for canopy's own client only (same private_key_jwt + DPoP).
    "/oauth/probe",
    "/oauth/host/jwks.json",
    "/.well-known/oauth-authorization-server",
    "/.well-known/oauth-protected-resource",
    # The Human Context Protocol discovery document (HCP v1 Appendix C) — what
    # this instance supports, read BEFORE a client holds a credential. It names
    # no person and returns no entry; every other /api/hcp/ route needs a token.
    # A full path, so it admits nothing else under /api/hcp/.
    "/api/hcp/.well-known/hcp-configuration",
    # The contact surface. A contact token deliberately produces no
    # `request.user`, so every one of these would bounce to a login page that
    # a person with no canopy account can never complete. Listed as a PREFIX
    # and nothing else is: this is the entire set of operations a contact may
    # reach, and adding one is a deliberate act of putting it under /api/contact/.
    # The routes self-enforce via `contact_auth`, which requires the principal
    # this middleware cannot see.
    "/api/contact/",
    # A2A Agent Card discovery. A client reads a card BEFORE it holds any
    # credential — that is what the card is for. The public card is served only
    # for an agent that offers outsiders something (404 otherwise, same as no
    # such slug); the extended card requires a session or PAT via Ninja's
    # `session_auth`. See apps/agents/a2a_api.py.
    "/api/a2a/",
    "/api/inbound/",          # auth=None — self-enforces via the Google-signed OIDC push token
    "/api/slack/",            # Slack webhooks — self-enforce via the Slack signing secret (apps/slack/views.py)
    "/api/system/public-stats",  # auth=None — aggregates only, no names/ids (public explainer)
    "/api/beta-requests",        # auth=None — the public site's request-access form; grants nothing
    # NOTE: "/about" is NOT here. Every other entry above ends in "/" (or is a
    # full path), so prefix-matching it is safe; "/about" alone would also
    # admit any future "/about-billing" or "/aboutus" route as a side effect.
    # See `_is_about` below for the exact-match version.
)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PATH_PREFIXES)


# The public viewers, under their workspace — /w/<ws>/walkthrough/<id>,
# /w/<ws>/review/<id>, /w/<ws>/share/<token>, /w/<ws>/storyboard/<slug>,
# /w/<ws>/narrative/<slug> and /w/<ws>/ddd-release/<narrative>/<run> — and the
# walkthrough's bytes (/w/<ws>/walkthrough/<id>/content): the ONLY address each
# has (canopy-web#1337). The SPA shell and the stream self-gate on their token:
# the page reads self-gating APIs passing `ws`, and the stream 404s a row from
# another workspace. `/w/<ws>/walkthroughs` and `/w/<ws>/storyboards` (the
# lists, plural) and every other tenant page stay behind the gate — the
# trailing slash after the viewer name is load-bearing.
_SCOPED_VIEWER = re.compile(
    r"^/w/[^/]+/(walkthrough|review|share|storyboard|narrative|ddd-release)/"
)


def _is_scoped_viewer(path: str) -> bool:
    return bool(_SCOPED_VIEWER.match(path))


# The retired flat addresses (`/walkthrough/…`, `/review/…`, `/share/…`,
# `/storyboard/…`, `/narrative/…`, `/ddd-release/…`, the pre-tenancy
# `/w/<uuid>/…`). They serve only a 404 that says links now carry
# the workspace (config.views.flat_artifact_gone); admitting them means a
# signed-out reader is told so, instead of being sent to sign in first.
_FLAT_ARTIFACT = re.compile(r"^/" + FLAT_ARTIFACT_PATH.removeprefix("^"))


def _is_flat_artifact(path: str) -> bool:
    return bool(_FLAT_ARTIFACT.match(path))


def _is_artifact_api(request) -> bool:
    # The per-artifact read APIs self-enforce token-or-session access:
    #   /api/share/<token>          — public read of a shared session;
    #   /api/reviews/<id>/…         — read + submit (token or session); the bare
    #                                 collection POST (/api/reviews/) is NOT here;
    #   GET /api/walkthroughs/<id>/ — detail (?t=<share_token>); list/upload
    #                                 (/api/walkthroughs/) stay auth'd.
    # The owner-side /api/sessions/ surface is NOT included.
    path = request.path
    if path.startswith("/api/share/"):
        return True
    if path.startswith("/api/reviews/") and path != "/api/reviews/":
        return True
    return (
        request.method == "GET"
        and path.startswith("/api/walkthroughs/")
        and path != "/api/walkthroughs/"
    )


_INVITE_TOKEN_LINK = re.compile(r"^/api/workspaces/invites/[^/]+/(preview|accept)$")


def _is_invite_link(request) -> bool:
    # /api/workspaces/invites/<token>/preview (auth=None — lets a not-yet-logged-in
    # visitor see what they were invited to before OAuth) and
    # /api/workspaces/invites/<token>/accept (already requires a session via Ninja's
    # own session_auth; letting an anonymous call through the middleware just moves
    # which layer issues the 401). Matched as an EXACT route shape, not a blanket
    # "/api/workspaces/invites/" prefix: the owner-only invite CRUD routes live at
    # /api/workspaces/{slug}/invites/... and would collide with that broader prefix
    # if a workspace's slug were literally "invites" (e.g.
    # /api/workspaces/invites/invites/ = list-invites for that workspace). Ninja's
    # own per-route auth (session_auth + `_require`) would still gate those even
    # under a blanket prefix, but this regex removes the ambiguity outright instead
    # of relying on that second layer.
    #
    # /invite/<token> (SPA shell) is also allowlisted here: the invitee has no
    # session yet (may not even be a Dimagi address), so the accept page must
    # render for them before OAuth — it calls the preview endpoint above to
    # render, and only needs a session at the moment they click Accept (which
    # 401s through Ninja's own auth if they somehow reach it unauthenticated).
    path = request.path
    if path.startswith("/invite/"):
        return True
    return bool(_INVITE_TOKEN_LINK.match(path))


def _is_storyboard_link(request) -> bool:
    # The storyboard read + feedback API self-enforces the ?t=<share_token> gate
    # (or a workspace-member session) inside the handler, so admit anonymous
    # callers and let the API decide. A wrong token 404s there rather than
    # 403ing, so existence never leaks. The pages themselves
    # (/w/<ws>/storyboard/<slug>, /w/<ws>/narrative/<slug>) are scoped viewers;
    # the flat /storyboard/ and /narrative/ are a 404 (canopy-web#1337).
    return request.path.startswith("/api/storyboards/")


def _is_about(path: str) -> bool:
    # The public explainer page shell. Exact match ONLY — its stats API is
    # allowlisted separately in PUBLIC_PATH_PREFIXES — so a future route that
    # merely begins "/about" (e.g. "/about-billing", "/aboutus") does not
    # silently become public too.
    return path == "/about"


def _is_ddd_release_link(request) -> bool:
    # The release read API (/api/ddd/release/<run_id>/) self-enforces the
    # ?t=<share_token> gate (or a workspace-member session) inside
    # build_release, so admit anonymous callers through the middleware. The
    # page is a scoped viewer (/w/<ws>/ddd-release/<narrative>/<run>); the flat
    # /ddd-release/ is a 404 (canopy-web#1337). The rest of /api/ddd/* stays
    # auth'd.
    return request.method == "GET" and request.path.startswith("/api/ddd/release/")


class LoginRequiredMiddleware:
    """Require authentication for every request except the allowlist.

    API routes (anything under /api/) get a 401 JSON response.
    Everything else is redirected to the login URL.

    Personal Access Tokens authenticate via
    `apps.tokens.middleware.BearerTokenAuthMiddleware`, which runs
    *before* this middleware in the chain. A valid PAT promotes
    `request.user` to a real authenticated user, so this gate admits
    the request through the standard `is_authenticated` branch — no
    special-case Bearer handling required here anymore.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not getattr(settings, "REQUIRE_AUTH", True):
            return self.get_response(request)

        if (
            request.user.is_authenticated
            or _is_public(request.path)
            or _is_about(request.path)
            or public_site.is_public_path(request.path)
            or _is_artifact_api(request)
            or _is_scoped_viewer(request.path)
            or _is_flat_artifact(request.path)
            or _is_ddd_release_link(request)
            or _is_storyboard_link(request)
            or _is_invite_link(request)
        ):
            return self.get_response(request)

        if request.path.startswith("/api/"):
            return JsonResponse({"detail": "Authentication required"}, status=401)

        # Build the post-login target so it works both locally (no prefix) and
        # on the labs sub-path deployment — a bare request.path would bounce the
        # user to a sibling tenant's path. See apps.common.script_prefix.
        next_target = self_full_path(request)
        return redirect(f"{settings.LOGIN_URL}?{urlencode({'next': next_target})}")

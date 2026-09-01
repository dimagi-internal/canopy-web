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

PUBLIC_PATH_PREFIXES = (
    "/accounts/",            # allauth login/logout/callback
    "/admin/",               # Django admin has its own auth
    "/health/",              # health check for Cloud Run
    "/static/",              # static assets
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
    "/api/auth/token-exchange",  # auth=None — self-enforces via the AppCredential Bearer header
    "/api/inbound/",          # auth=None — self-enforces via the Google-signed OIDC push token
)


def _is_public(path: str) -> bool:
    return any(path == p or path.startswith(p) for p in PUBLIC_PATH_PREFIXES)


def _is_share_link(path: str) -> bool:
    # /share/<token> (SPA shell) and the public read API (/api/share/<token>)
    # self-gate on the opaque share token, so let anonymous visitors through
    # the middleware. The owner-side /api/sessions/ surface is NOT included —
    # it stays auth'd.
    if path.startswith("/share/"):
        return True
    return path.startswith("/api/share/")


def _is_review_link(path: str) -> bool:
    # /review/<uuid>/  (SPA shell) and the per-review API read/submit endpoints
    # self-enforce token-or-session auth, so let the per-token public link
    # through the middleware without a session. The bare collection POST
    # (/api/reviews/) is NOT included — creating a review still requires auth.
    if path.startswith("/review/"):
        return True
    return path.startswith("/api/reviews/") and path != "/api/reviews/"


# Pre-reclaim content-stream URL, baked into already-rendered artifacts
# (DDD decks, review embeds). UUID-shaped only — workspace slugs never match.
_LEGACY_W_CONTENT = re.compile(
    r"^/w/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/content$"
)


def _is_walkthrough_link(request) -> bool:
    # The public walkthrough viewer SPA shell (/walkthrough/<uuid>) and the
    # content stream (/walkthrough/<uuid>/content), plus the per-walkthrough
    # detail GET, self-enforce token-gated public access (?t=<share_token>),
    # so let anonymous callers through the middleware. /w/ now means "workspace" (the authed tenant shell)
    # and is NOT allowlisted — except the legacy /w/<uuid>/content path, which
    # must reach its back-compat redirect. The bare collection
    # (/api/walkthroughs/) is NOT included — list/upload still require auth.
    path = request.path
    if path.startswith("/walkthrough/"):
        return True
    if _LEGACY_W_CONTENT.match(path):
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
    # own per-route auth (session_auth + _require_role) would still gate those even
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
    # /storyboard/<slug> (SPA shell) and its read + feedback API self-enforce the
    # ?t=<share_token> gate (or a workspace-member session) inside the handler,
    # so admit anonymous callers and let the API decide. A wrong token 404s there
    # rather than 403ing, so existence never leaks.
    #
    # /narrative/ was missed when the surface shipped: the FRONTEND allowlist
    # knew about it but this one did not, so an anonymous reader got the arc
    # fine and hit a Google login the moment they clicked "Read the scenes".
    path = request.path
    if path.startswith("/storyboard/") or path.startswith("/narrative/"):
        return True
    return path.startswith("/api/storyboards/")


def _is_ddd_release_link(request) -> bool:
    # /ddd-release/<slug>/<run_id> (SPA shell) and the read API
    # (/api/ddd/release/<run_id>/) self-enforce the ?t=<share_token> gate (or a
    # workspace-member session) inside build_release, so admit anonymous callers
    # through the middleware. The rest of /ddd/* and /api/ddd/* stay auth'd.
    path = request.path
    if path.startswith("/ddd-release/"):
        return True
    return request.method == "GET" and path.startswith("/api/ddd/release/")


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
            or _is_walkthrough_link(request)
            or _is_review_link(request.path)
            or _is_share_link(request.path)
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

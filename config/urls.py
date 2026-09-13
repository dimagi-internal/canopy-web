"""
URL configuration for canopy-web project.
"""
from django.contrib import admin
from django.urls import include, path, re_path
from django.views.generic import RedirectView

from apps.api.api import api as api_v2
from apps.api.views import redoc_docs, scalar_docs
from apps.tokens.cli_authorize_views import cli_authorize as views_cli_authorize
from apps.tokens.views_embed import embed_chat
from apps.walkthroughs.streaming import walkthrough_content as views_walkthrough_content
from config.views import csrf_view, health_check, spa_view

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("health/", health_check, name="health-check"),
    path("api/csrf/", csrf_view, name="csrf"),
    path("api/debug/", include("apps.common.urls_debug")),
    path("auth/cli/authorize/", views_cli_authorize, name="cli_authorize"),
    # The embed shell — the ONE framable canopy page. A bare view because it
    # sets per-request response headers (frame-ancestors from the app's
    # registered origins) and is X-Frame-Options-exempt; see
    # apps/tokens/views_embed.py for why the exemption is safe. Declared before
    # the SPA catch-all, whose negative lookahead does not exclude `embed/`.
    path("embed/chat", embed_chat, name="embed-chat"),
    # <str:> (not <uuid:>) so a malformed id is handled by the view (bare 404)
    # instead of falling through to the SPA catch-all and painting the whole app
    # inside a failed content embed. The view 404s any id it can't resolve.
    path("walkthrough/<str:wid>/content", views_walkthrough_content, name="walkthrough-content"),
    # Back-compat: the pre-reclaim stream URL is baked into already-rendered
    # artifacts (DDD decks, review embeds). Redirect, don't fall to the SPA.
    path(
        "w/<uuid:wid>/content",
        RedirectView.as_view(pattern_name="walkthrough-content", query_string=True),
        name="walkthrough-content-legacy",
    ),
    path("api/", api_v2.urls),
    path("api/docs/", scalar_docs, name="api_docs_scalar"),
    path("api/redoc/", redoc_docs, name="api_docs_redoc"),
    # Catch-all: serve the SPA for any non-API route (last).
    #
    # `assets/` is excluded, and that exclusion is load-bearing. Those paths are
    # content-hashed bundles, and every deploy rehashes them — so a browser
    # holding a cached index.html (the service worker precaches it) asks for a
    # filename that no longer exists on the server. WhiteNoise passes the miss
    # through, and without this the catch-all answered a `.js` request with
    # index.html at `200 text/html`. The browser cannot parse HTML as a module,
    # so the app rendered a WHITE PAGE until a force-refresh, with no error a
    # user could act on. Reported and reproduced 2026-09-08.
    #
    # A 404 is the honest answer to "that bundle is gone", and it is one the
    # client can handle: the request fails visibly rather than half-succeeding.
    re_path(
        r"^(?!api/|admin/|accounts/|health/|static/|auth/|assets/).*$",
        spa_view,
        name="spa",
    ),
]

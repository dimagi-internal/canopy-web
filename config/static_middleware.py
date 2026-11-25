"""WhiteNoise, with three changes.

1. ``/`` is NOT answered from ``frontend/dist/index.html``. WhiteNoise runs before
   sessions and auth, so it cannot know whether the visitor is signed in, and that
   is exactly what decides whether ``/`` is the site or the app. It falls through to
   Django, where ``spa_view`` asks ``public_site.serve``.
2. The site's own files under ``site/dist/site/`` are served at ``/site/``, with the
   same cache rule as the SPA's (hashed ⇒ immutable, else revalidate).
3. A CORS preflight for the SPA's built files (``/assets/``) is answered
   (``asset_preflight``). The dynamic-view sandbox (``apps/dynamic_views/frame.py``,
   which draws every workspace app page) has an opaque origin, so its module scripts
   are cross-origin requests; WhiteNoise already marks every file
   ``Access-Control-Allow-Origin: *``. A browser that sends a header on every request
   (an agent's headless browser signed in with ``Authorization: Bearer <its PAT>``)
   makes each of those a preflighted request, and WhiteNoise answers ``OPTIONS`` with a
   405, so the frame's code never loaded and the page stayed blank. Answering it grants
   nothing: the files are public (``/assets/`` is on the login allowlist) and do not
   read the header; the page's data still comes only from the parent page's REST calls,
   as the viewer.
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from whitenoise.middleware import WhiteNoiseMiddleware

ASSETS_PREFIX = "/assets/"


def asset_preflight(request: HttpRequest) -> HttpResponse | None:
    """The answer to a CORS preflight for a built SPA file, or None for anything else.

    Read-only methods only, no credentials (``Access-Control-Allow-Credentials`` is never
    sent, so a cookie still cannot ride along), and the requested headers are allowed
    because the response is the same public file whatever they say."""
    if request.method != "OPTIONS" or not request.path_info.startswith(ASSETS_PREFIX):
        return None
    wanted = request.META.get("HTTP_ACCESS_CONTROL_REQUEST_METHOD", "")
    if wanted not in ("GET", "HEAD"):
        return None
    response = HttpResponse(status=204)
    response["Access-Control-Allow-Origin"] = "*"
    response["Access-Control-Allow-Methods"] = "GET, HEAD"
    headers = request.META.get("HTTP_ACCESS_CONTROL_REQUEST_HEADERS", "")
    if headers:
        response["Access-Control-Allow-Headers"] = headers
    response["Access-Control-Max-Age"] = "86400"
    response["Vary"] = "Access-Control-Request-Headers"
    return response


class CanopyWhiteNoiseMiddleware(WhiteNoiseMiddleware):
    def __init__(self, get_response=None):
        super().__init__(get_response)
        site_assets = settings.SITE_DIST_DIR / "site"
        if site_assets.is_dir():
            self.add_files(str(site_assets), prefix="site/")

    def __call__(self, request):
        if request.path_info == "/":
            return self.get_response(request)
        preflight = asset_preflight(request)
        if preflight is not None:
            return preflight
        return super().__call__(request)

"""
Project-level views for canopy-web.
"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpResponse, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from config.static_cache import REVALIDATE


def health_check(request):
    """Simple health check endpoint."""
    return JsonResponse({"status": "ok"})


@require_GET
@ensure_csrf_cookie
def csrf_view(request):
    """Force the CSRF cookie to be set. The SPA hits this once at boot."""
    return JsonResponse({"ok": True})


def spa_view(request):
    """Serve the built SPA index.html for any non-API route.

    In production, WhiteNoise serves /static/ and /assets/ assets referenced
    by index.html. In development, Vite serves the SPA directly — this view
    is only hit when the frontend build output is present.

    This is the SECOND way the shell reaches a browser: WhiteNoise answers
    `/canopy/` (its index file), and every deep link — `/supervisor`,
    `/w/<ws>/…`, `/share/<token>` — lands here. It shipped with no cache headers
    at all, which leaves the freshness of the one file that names the current
    asset hashes up to each browser's heuristics. Same `no-cache` as WhiteNoise
    now sends, so both doors agree; see config/static_cache.py.
    """
    index_path: Path = settings.FRONTEND_DIST_DIR / "index.html"
    if not index_path.exists():
        return HttpResponse(
            "Frontend build not found. Run `cd frontend && npm run build` "
            "or use the Vite dev server.",
            status=503,
            content_type="text/plain",
        )
    response = FileResponse(open(index_path, "rb"), content_type="text/html")
    response["Cache-Control"] = REVALIDATE
    return response

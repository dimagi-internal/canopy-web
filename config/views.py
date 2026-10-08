"""
Project-level views for canopy-web.
"""
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, HttpResponse, JsonResponse
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET

from config import public_site
from config.static_cache import REVALIDATE


def health_check(request):
    """Simple health check endpoint."""
    return JsonResponse({"status": "ok"})


# The artifact addresses that no longer exist (canopy-web#1337): the flat
# viewers and their byte stream, and the pre-tenancy `/w/<uuid>` walkthrough
# links. Matched against Django's path (no leading slash). A workspace slug is
# never a UUID, so `w/<uuid>` cannot shadow a real workspace.
FLAT_ARTIFACT_PATH = (
    r"^(?:(?:walkthrough|review|share)(?:/.*)?"
    r"|w/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}(?:/.*)?)$"
)

_FLAT_ARTIFACT_GONE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Link not found · Canopy</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 34rem; margin: 15vh auto;
         padding: 0 16px; line-height: 1.5; color: #1c1917; background: #fafaf9; }
  @media (prefers-color-scheme: dark) { body { color: #e7e5e4; background: #1c1917; } }
  code { font-size: .95em; }
</style></head>
<body>
<h1>This link no longer works</h1>
<p>Canopy links now include the workspace, like
<code>/w/&lt;workspace&gt;/review/&lt;id&gt;</code>. Links without one were
retired and are not forwarded.</p>
<p>Ask whoever sent you this link for the current one.</p>
</body></html>
"""


def flat_artifact_gone(request, *args, **kwargs):
    """A flat artifact link: a plain 404 that says why. Never a redirect — there
    is one URL per artifact, under its workspace (owner decision, 2026-10-08)."""
    resp = HttpResponse(_FLAT_ARTIFACT_GONE, status=404, content_type="text/html; charset=utf-8")
    resp["Cache-Control"] = REVALIDATE
    return resp


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
    site_page = public_site.serve(request)
    if site_page is not None:
        return site_page

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

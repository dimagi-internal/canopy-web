"""Bare Django views for the harness (outside the Ninja API)."""
from __future__ import annotations

from django.http import HttpRequest, HttpResponseRedirect
from django.views.decorators.http import require_GET

from .models import RunnerAppPreference


@require_GET
def runner_app_home(request: HttpRequest) -> HttpResponseRedirect:
    """`/runner-app/` — where the Canopy Runner desktop app's window opens: the
    page the person chose in Settings (`RunnerAppPreference`), else the supervisor.
    Signed out goes to the supervisor too, which sends you to sign in."""
    page = RunnerAppPreference.PAGES[RunnerAppPreference.opens_on_for(request.user)]
    return HttpResponseRedirect(request.META.get("SCRIPT_NAME", "") + page)


_NO_RELEASE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Canopy Runner download unavailable</title>
<style>body{font:15px/1.5 system-ui,sans-serif;max-width:36rem;margin:3rem auto;padding:0 1rem;
color:#1c1917;background:#fafaf9}@media(prefers-color-scheme:dark){body{color:#e7e5e4;background:#1c1917}
a{color:#93c5fd}}</style></head>
<body><h1>No download for __LABEL__ yet</h1>
<p>The runner version this Canopy runs has not been built for __LABEL__.
Builds appear a few minutes after a change ships; try again shortly.</p>
<p><a href="__BACK__">Back to Get started</a></p>
</body></html>
"""


@require_GET
def download_runner(request: HttpRequest, platform: str):
    """`/download/runner/<platform>` — a signed-in person's installer download:
    a 302 to a 15-minute presigned URL for the deployed runner version's
    installer. Signed out: to sign in, and back. Nothing built for the platform
    (or downloads off): a small 404 page that says so."""
    from django.conf import settings
    from django.contrib.auth.views import redirect_to_login
    from django.http import Http404, HttpResponse
    from django.utils.html import escape

    from apps.common.script_prefix import self_full_path

    from . import runner_releases as rel

    if not request.user.is_authenticated:
        return redirect_to_login(self_full_path(request), settings.LOGIN_URL)
    label = rel.PLATFORMS.get(platform)
    if label is None:
        raise Http404("unknown platform")
    sha = rel.deployed_sha()
    art = None
    if sha:
        try:
            art = rel.artifact(rel.manifest(sha), platform, "installer")
        except rel.NotConfigured:
            art = None
    if art is None:
        back = request.META.get("SCRIPT_NAME", "") + "/get-started"
        page = _NO_RELEASE.replace("__LABEL__", escape(label)).replace("__BACK__", escape(back))
        resp = HttpResponse(page, status=404, content_type="text/html; charset=utf-8")
        resp["Cache-Control"] = "no-store"
        return resp
    resp = HttpResponseRedirect(rel.presign(sha, art["file"]))
    resp["Cache-Control"] = "no-store"
    return resp

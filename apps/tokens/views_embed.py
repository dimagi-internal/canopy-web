"""The embed shell — the one canopy page allowed to be framed.

A bare Django view rather than a Ninja route (same reasoning as the walkthrough
content stream): it returns HTML and sets response headers per-request, neither
of which fits the Ninja contract.

**Why this route has to exist at all.** `config/settings/base.py` enables
`XFrameOptionsMiddleware` with no `X_FRAME_OPTIONS` override, so Django's `DENY`
default applies to every response and no canopy page can be put in an iframe.
An embedded widget is impossible until something opts out.

**Why opting out is the risk, and how it is contained.** A page exempt from
`X-Frame-Options` with nothing in its place is frameable by *any* site — which
is clickjacking on every deployment that embeds it. So the exemption is paired
with a `frame-ancestors` directive built from origins an admin registered on the
`AppCredential`, and the pairing is unconditional: if there are no valid
origins, there is no shell to serve. A 404, not an unrestricted page.

**Why `?app=` is safe to take from the URL.** The parameter only selects *which
policy to apply*; the browser then enforces it. A hostile site that frames
`/embed/chat?app=connect-labs` receives `frame-ancestors
https://labs.connect.dimagi.com`, and its own origin is not on that list, so the
browser refuses to render the frame. The parameter cannot widen anything — and
the shell carries no data of its own, because the delegated token arrives later
over `postMessage` (see the v2 spec §3), never in this URL.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET

from .models import AppCredential

#: The frame announces itself, and nothing else. `targetOrigin` is `*` for this
#: ONE message on purpose: the frame cannot know its host's origin until the
#: host speaks first, and the message carries no data — it is "I exist". The
#: host replies with the token to the frame's own known origin, and the frame
#: validates `event.origin` against its host list before accepting anything.
#: Nothing may be posted to `*` after this point.
_SHELL = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canopy</title>
<style>
  html, body {{ margin: 0; height: 100%; font: 14px/1.5 system-ui, sans-serif;
                background: #1c1917; color: #e7e5e4; }}
  .boot {{ display: grid; place-items: center; height: 100%; opacity: .7; }}
</style>
</head>
<body>
<div class="boot" id="canopy-widget-boot">Starting&hellip;</div>
<script>
  window.CANOPY_EMBED = {{ app: {app!r} }};
  parent.postMessage({{ source: "canopy-widget", type: "ready" }}, "*");
</script>
</body>
</html>
"""


@require_GET
@xframe_options_exempt
def embed_chat(request: HttpRequest) -> HttpResponse:
    """Serve the widget shell for a registered app, framed only by its origins.

    Every refusal is a 404 rather than a 403, because there is nothing here
    worth distinguishing: an app name is not a secret, and a single status for
    "unknown app", "revoked app" and "no origins registered" keeps the
    dangerous state (a servable page with no policy) unreachable by any path.
    """
    name = (request.GET.get("app") or "").strip()
    if not name:
        raise Http404("embed requires ?app=")

    app = AppCredential.objects.filter(name=name, revoked_at__isnull=True).first()
    if app is None:
        raise Http404("no such embedding app")

    origins = app.frame_origins()
    if not origins:
        # The pairing is unconditional: no policy, no page. An XFO-exempt shell
        # with an empty frame-ancestors list is frameable by everyone, so this
        # is the one branch that must never fall through to a response.
        raise Http404("this app has no registered frame origins")

    response = HttpResponse(_SHELL.format(app=name), content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = "frame-ancestors " + " ".join(origins)
    # Matches config/static_cache.py's rule for anything without a
    # content-hashed name: an unhashed document is never cached, so a changed
    # policy (or a revoked app) takes effect on the next load rather than
    # whenever a proxy feels like it.
    response["Cache-Control"] = "no-cache"
    return response


@require_GET
def embed_widget_js(request: HttpRequest) -> HttpResponse:
    """Serve the widget loader — the file a host names in its `<script>` tag.

    Read straight out of the frontend build the way `config.views.spa_view`
    reads `index.html`, rather than relying on a static-files mapping: this is
    the one URL third parties hard-code, so it should not be able to break
    because a collectstatic step or a WhiteNoise prefix moved.

    Deliberately NOT framing-related and NOT app-scoped. It is a plain script
    with no secrets and no per-app behaviour — `canopy.init({app: …})` selects
    the app at call time, and the framing policy is enforced on the SHELL the
    frame loads (see `embed_chat`). A `<script src>` needs no CORS header, so
    none is sent.

    `no-cache` because the filename is fixed and cannot be content-hashed —
    hosts hard-code it, so a new loader has to reach them without their editing
    anything. That matches config/static_cache.py's rule for every unhashed
    file, and it costs a 304 rather than a download.
    """
    path: Path = settings.FRONTEND_DIST_DIR / "embed" / "widget.js"
    if not path.exists():
        # Same shape as spa_view's missing-build response: a plain-text 503 that
        # names the command, because the alternative is a host debugging a
        # silent 404 in someone else's page.
        return HttpResponse(
            "Widget loader not built. Run `cd frontend && npm run build:widget`.",
            status=503,
            content_type="text/plain",
        )
    response = FileResponse(open(path, "rb"), content_type="text/javascript")
    response["Cache-Control"] = "no-cache"
    return response

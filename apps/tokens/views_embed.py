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

import json
from functools import lru_cache
from pathlib import Path

from django.conf import settings
from django.http import FileResponse, Http404, HttpRequest, HttpResponse
from django.urls import get_script_prefix
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET

from .models import AppCredential

#: The vite entry, as it is keyed in the build manifest.
_EMBED_ENTRY = "src/embed/main.tsx"


@lru_cache(maxsize=1)
def _manifest() -> dict:
    """Vite's build manifest, or `{}` when the frontend has not been built.

    Cached because it is immutable for the life of a deployed image — a new
    build is a new container. The cache is keyed on nothing, so a dev box that
    rebuilds needs a server restart to see new hashes, which matches how
    `spa_view` already behaves for `index.html`.
    """
    path = settings.FRONTEND_DIST_DIR / ".vite" / "manifest.json"
    if not path.exists():
        return {}
    with open(path) as fh:
        return json.load(fh)


def _embed_assets() -> tuple[str | None, list[str]]:
    """`(entry js, css files)` for the in-frame app, from the build manifest.

    The CSS is NOT on the entry. Both the app and the frame import
    `src/index.css`, so vite hoists it onto a SHARED chunk and records it
    against that chunk instead — which means resolving it requires walking the
    entry's `imports` transitively. Reading only `entry["css"]` yields an
    unstyled frame, and does so silently.
    """
    manifest = _manifest()
    entry = manifest.get(_EMBED_ENTRY)
    if not entry:
        return None, []

    css: list[str] = list(entry.get("css", []))
    seen: set[str] = set()

    def walk(key: str) -> None:
        if key in seen:
            return
        seen.add(key)
        chunk = manifest.get(key)
        if not chunk:
            return
        css.extend(chunk.get("css", []))
        for nested in chunk.get("imports", []):
            walk(nested)

    for imported in entry.get("imports", []):
        walk(imported)

    # De-duplicated, order preserved: two chunks can legitimately name the same
    # stylesheet, and emitting it twice is a wasted request.
    return entry["file"], list(dict.fromkeys(css))

#: The shell is a LOADER, not the app: it carries the server-known facts the
#: frame cannot safely learn any other way (which app, and which origins may
#: talk to it) and then hands over to the bundle. The `ready` handshake lives
#: in the bundle (frontend/src/embed/hostLink.ts) rather than inline here, so
#: there is one implementation of the protocol instead of two.
_SHELL = """<!doctype html>
<html lang="en" class="{html_class}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Canopy</title>
{auto_script}
{css}
<style>
  html, body {{ margin: 0; height: 100%; }}
  #canopy-widget-boot {{ height: 100%; }}
  .canopy-boot {{ display: grid; place-items: center; height: 100%;
                  font: 14px/1.5 system-ui, sans-serif; color: #a8a29e; }}
  /* The boot screen paints before the app CSS applies, so it carries its own
     background in each mode — otherwise a light panel flashes dark first. */
  html {{ background: #fafaf9; }}
  html.dark {{ background: #0c0a09; }}
</style>
</head>
<body>
<div id="canopy-widget-boot"><div class="canopy-boot">Starting&hellip;</div></div>
<script>
  // Injected by the SERVER, which is the only party that can be trusted to say
  // which origins may talk to this frame — the host is the one being
  // authenticated, so a list it supplied would authenticate nothing.
  window.CANOPY_EMBED = {{ app: {app}, origins: {origins} }};
</script>
{script}
</body>
</html>
"""


#: The panel's light/dark, from `?theme=` — set by the loader from the host's
#: `theme.mode`. Rendered by the SERVER, not applied later by the frame's JS,
#: so the panel is in the right mode from its first paint instead of flashing
#: dark on every page view of a light host. Anything unrecognised is the
#: historical default.
THEME_MODES = ("light", "dark", "auto")
DEFAULT_THEME_MODE = "dark"

#: `auto` is the one mode only the browser can resolve. In <head>, before any
#: stylesheet, so the class is right before anything paints.
_AUTO_THEME = (
    "<script>if(window.matchMedia&&!matchMedia('(prefers-color-scheme: dark)').matches)"
    "document.documentElement.classList.remove('dark')</script>"
)


def _theme_mode(request: HttpRequest) -> str:
    mode = (request.GET.get("theme") or "").strip().lower()
    return mode if mode in THEME_MODES else DEFAULT_THEME_MODE


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

    js, css = _embed_assets()
    prefix = get_script_prefix().rstrip("/")

    if js is None:
        # The policy resolved but the bundle is missing — say so rather than
        # frame a blank page. A host debugging an empty iframe has no way to
        # tell this from a broken token.
        body = (
            "<!doctype html><meta charset=utf-8>"
            "<p style='font:14px system-ui;padding:1rem'>"
            "Canopy widget not built. Run <code>cd frontend &amp;&amp; npm run build</code>."
            "</p>"
        )
    else:
        mode = _theme_mode(request)
        body = _SHELL.format(
            # From an allowlist, never the raw parameter: it lands in an HTML
            # attribute.
            html_class="dark" if mode in ("dark", "auto") else "",
            auto_script=_AUTO_THEME if mode == "auto" else "",
            # json.dumps, not an f-string: these values land inside a <script>
            # block, and an app name or origin containing a quote would
            # otherwise end the string and inject.
            app=json.dumps(name),
            origins=json.dumps(origins),
            css="\n".join(
                f'<link rel="stylesheet" href="{prefix}/{href}">' for href in css
            ),
            script=f'<script type="module" src="{prefix}/{js}"></script>',
        )

    response = HttpResponse(body, content_type="text/html; charset=utf-8")
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

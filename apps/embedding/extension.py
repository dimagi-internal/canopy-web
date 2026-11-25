"""The Dimagi Canopy browser extension's side panel: the one canopy page it may frame.

The extension's panel (an extension page, `chrome-extension://<id>/panel.html`) is a
full-size iframe of `GET /ext/panel/`, which picks the workspace-app page for the
tab and draws it in the dynamic-view sandbox. Design and wire protocol:
docs/architecture/browser-extension.md.

**Framing is the risk, and it is paired, as for the embed shell** (`views_embed.py`).
Every other canopy page keeps `X-Frame-Options: DENY`. This page is exempt from it
only together with `frame-ancestors` naming exactly the allowed extension origins
(`settings.BROWSER_EXTENSION_IDS`), so a web page that frames it is refused by the
browser; with no ids configured there is no page (404), never an unrestricted one.
The sandbox frame the panel nests also lists these origins among its ancestors
(`apps/dynamic_views/frame.py::frame_page`), because `frame-ancestors` is checked
against every ancestor, not only the parent.

**The page carries no data.** It is the SPA shell plus the allowed origins in a
`<meta>`; everything it shows it reads afterwards with the viewer's own session,
and it talks only to an allowed extension origin that is actually its parent
(`frontend/src/ext/extensionBridge.ts`).

**It must render signed out.** In an iframe a bounce to Google's sign-in is refused
(Google denies framing), so `LoginRequiredMiddleware` admits this path and the page
shows a "Sign in to Canopy" button that asks the extension to open the sign-in in a
tab (`canopy-ext:open`).
"""
from __future__ import annotations

import json
from html import escape

from django.conf import settings
from django.http import Http404, HttpRequest, HttpResponse
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.http import require_GET

#: The panel's address (`config/urls.py`), admitted signed out by `LoginRequiredMiddleware`.
PANEL_PATH = "/ext/panel/"
#: Where the sign-in the panel opens in a tab lands: "you're signed in, return to the panel".
SIGNED_IN_PATH = "/ext/signed-in/"
#: The `<meta name>` the SPA reads the allowed extension origins from.
ORIGINS_META = "canopy-ext-origins"


def extension_origins() -> list[str]:
    """`chrome-extension://<id>` for every allowed extension id, in settings order.
    The ids are validated at boot (`config/settings/base.py`)."""
    return list(dict.fromkeys(f"chrome-extension://{i}" for i in settings.BROWSER_EXTENSION_IDS or []))


def panel_csp(origins: list[str]) -> str:
    return "frame-ancestors " + " ".join(origins)


@require_GET
@xframe_options_exempt
def ext_panel(request: HttpRequest) -> HttpResponse:
    """The SPA shell, framable only by the allowed extension origins."""
    origins = extension_origins()
    if not origins:
        # The pairing is unconditional: an XFO-exempt page with an empty frame-ancestors
        # is framable by everyone, so no allowed extension means no page.
        raise Http404("no browser extension is allowed to frame canopy")
    index = settings.FRONTEND_DIST_DIR / "index.html"
    if not index.exists():
        body = ("<!doctype html><meta charset=utf-8><p style='font:14px system-ui;padding:1rem'>"
                "The frontend is not built. Run <code>cd frontend &amp;&amp; npm run build</code>.</p>")
    else:
        meta = f'<meta name="{ORIGINS_META}" content="{escape(json.dumps(origins), quote=True)}">'
        body = index.read_text(encoding="utf-8").replace("</head>", meta + "</head>", 1)
    response = HttpResponse(body, content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = panel_csp(origins)
    # An unhashed document is never cached (config/static_cache.py), so a changed list
    # of allowed extensions takes effect on the next load.
    response["Cache-Control"] = "no-cache"
    return response

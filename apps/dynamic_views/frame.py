"""The frame a dynamic view's code runs in: `GET /dynamic-views/frame/`, nothing else.

The canopy page frames this document with `sandbox="allow-scripts"` and no
`allow-same-origin`, and the response carries `Content-Security-Policy: sandbox
allow-scripts` so it is opaque even when opened top-level. Like the MCP Apps
sandbox proxy (`apps/mcp_apps_host/views_mcp_apps.py`), whose reasoning this
follows:

* **No session, no cookies.** `DynamicViewFrameMiddleware` answers before the
  session and auth middleware, so nothing here can set or read a cookie, and an
  opaque document cannot read canopy's either.
* **No network.** `connect-src 'none'`; scripts and styles only from canopy's own
  build (the frame runtime, `frontend/src/dynamicViews/frame/`), plus
  `'unsafe-eval'` so the runtime can run the view's compiled code; images only
  `data:`/`blob:`. The code reaches data only by asking the canopy page, which
  answers from the catalog (`catalog.py`) as the viewer.
* **Framed only by canopy.** `frame-ancestors` is canopy's own origins.
* **The document carries no data.** The view's code and its data arrive later,
  over `postMessage`, from the page that framed it. The one inline script is the
  server-known list of origins the runtime may accept messages from, under a
  per-response nonce.
"""
from __future__ import annotations

import json
import secrets

from django.http import HttpRequest, HttpResponse
from django.urls import get_script_prefix

from apps.embedding.views_embed import entry_assets
from apps.mcp_apps_host.views_mcp_apps import host_origins

PATH = "/dynamic-views/frame/"
PREFIX = "/dynamic-views/"
FRAME_ENTRY = "src/dynamicViews/frame/main.tsx"

_SHELL = """<!doctype html>
<html lang="en" class="{html_class}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>canopy view</title>
{css}
<style>html,body{{margin:0;background:transparent}}</style>
</head>
<body>
<div id="dv-root"></div>
<script nonce="{nonce}">window.CANOPY_DV = {{ origins: {origins} }};</script>
{script}
</body>
</html>
"""


def csp(origins: list[str], nonce: str) -> str:
    own = " ".join(origins)
    return "; ".join([
        "sandbox allow-scripts",
        "default-src 'none'",
        f"script-src 'nonce-{nonce}' {own} 'unsafe-eval'",
        f"style-src {own} 'unsafe-inline'",
        f"font-src {own} data:",
        "img-src data: blob:",
        "connect-src 'none'",
        "media-src 'none'",
        "object-src 'none'",
        "frame-src 'none'",
        "worker-src 'none'",
        "form-action 'none'",
        "base-uri 'none'",
        f"frame-ancestors {own}",
    ])


def frame_page(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        response = HttpResponse(status=405)
        response["Allow"] = "GET, HEAD"
        return response
    origins = host_origins(request)
    nonce = secrets.token_urlsafe(16)
    js, css = entry_assets(FRAME_ENTRY)
    prefix = get_script_prefix().rstrip("/")
    if js is None:
        body = ("<!doctype html><meta charset=utf-8><p style='font:14px system-ui;padding:1rem'>"
                "The view frame is not built. Run <code>cd frontend &amp;&amp; npm run build</code>.</p>")
    else:
        body = _SHELL.format(
            html_class="dark" if request.GET.get("theme") == "dark" else "",
            css="\n".join(f'<link rel="stylesheet" href="{prefix}/{href}">' for href in css),
            nonce=nonce,
            # json.dumps into a <script>, with `<` escaped so no value can end the block.
            origins=json.dumps(origins).replace("<", "\\u003c"),
            script=f'<script type="module" src="{prefix}/{js}"></script>',
        )
    response = HttpResponse(body, content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = csp(origins, nonce)
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "no-store"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


class DynamicViewFrameMiddleware:
    """Answers `/dynamic-views/…` before sessions, auth and CSRF exist (see module)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        path = request.path_info
        if not path.startswith(PREFIX) and path != PREFIX.rstrip("/"):
            return self.get_response(request)
        if path in (PATH, PATH.rstrip("/")):
            return frame_page(request)
        response = HttpResponse("Not found", status=404, content_type="text/plain")
        response["Cache-Control"] = "no-store"
        return response

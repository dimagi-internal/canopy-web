"""The MCP Apps SANDBOX PROXY (SEP-1865 §Sandbox proxy) — one page, nothing else.

canopy's chat frames this page for every View it renders; the page frames the
View's HTML and relays JSON-RPC between the two. Spec 2026-10-08 §7, as amended
by owner decision 1 (no new DNS):

* **Served at a canopy sub-path, isolated by an OPAQUE origin.** The host frames
  it with `sandbox="allow-scripts"` and NO `allow-same-origin`, and the response
  carries `Content-Security-Policy: sandbox allow-scripts` so it is opaque even
  opened top-level. An opaque document cannot read canopy's cookies, storage or
  DOM, and is cross-origin to every canopy page — the isolation the standard's
  "different origin + `allow-same-origin`" buys. This DEVIATES from §Sandbox
  proxy 2 (which requires `allow-same-origin` on the proxy); the cost is that a
  View cannot use storage. `MCP_APPS_SANDBOX_URL` moves it to its own origin.
* **No session, no cookies, no CSRF.** `McpAppsSandboxMiddleware` answers before
  the session and auth middleware run, so nothing here can set or read a cookie
  (the browser may SEND canopy's cookies on this same-origin request; nothing
  reads them, and the document that results cannot either).
* **Only the proxy.** `GET /mcp-apps/sandbox/`. Every other path under
  `/mcp-apps/` is a 404, and every other method a 405.
* **The View's CSP is this response's CSP.** The View is the proxy's `srcdoc`
  child and inherits its policy container, so the header built from `?csp=`
  (already narrowed by the host to the site's own origins, re-validated here)
  binds the View — a header, not a `<meta>` the View's HTML could precede.

The relay itself (inline below): it accepts the host's messages only from
`window.parent` AND a canopy origin; the View's only from the inner frame's
`contentWindow`; it swallows `ui/notifications/sandbox-*` in both directions and
synthesizes no requests (§Sandbox proxy 6, 7).
"""
from __future__ import annotations

import json
from urllib.parse import urlsplit

from django.conf import settings
from django.http import HttpRequest, HttpResponse

from . import mcp_apps

PREFIX = "/mcp-apps/"


def _origin(url: str) -> str | None:
    parts = urlsplit(url or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return None
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname.lower()}{port}"


def host_origins(request: HttpRequest) -> list[str]:
    """The canopy page origins that frame this proxy and may talk to it."""
    configured = [o for o in (_origin(u) for u in settings.MCP_APPS_HOST_ORIGINS) if o]
    if configured:
        return list(dict.fromkeys(configured))
    urls = [settings.CANOPY_PUBLIC_BASE_URL, *(settings.CANOPY_FORMER_BASE_URLS or [])]
    found = [o for o in (_origin(u) for u in urls) if o]
    own = _origin(request.build_absolute_uri("/"))
    if own:
        found.append(own)
    return list(dict.fromkeys(found))


def frame_ancestors(request: HttpRequest) -> list[str]:
    """canopy's own origins, plus every live Connected site's frame origins —
    the embed panel nests four deep (site page → canopy embed → proxy → View),
    and `frame-ancestors` checks every ancestor, not just the parent."""
    from .models import AppCredential

    out = host_origins(request)
    for app in AppCredential.objects.filter(revoked_at__isnull=True):
        out.extend(app.frame_origins())
    return list(dict.fromkeys(out))


_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>canopy view sandbox</title>
<meta name="referrer" content="no-referrer">
<style>html,body{{margin:0;padding:0;height:100%;overflow:hidden;background:transparent}}
iframe{{border:0;width:100%;height:100%;display:block}}</style></head>
<body><script>
(function () {{
  "use strict";
  var HOSTS = {hosts};
  var READY = "ui/notifications/sandbox-proxy-ready";
  var RESOURCE = "ui/notifications/sandbox-resource-ready";
  var RESERVED = "ui/notifications/sandbox-";
  if (window.self === window.top) {{
    document.body.textContent = "This page only runs inside canopy.";
    return;
  }}
  var parentOrigin = null;
  var inner = null;
  function isRpc(d) {{ return d && typeof d === "object" && d.jsonrpc === "2.0"; }}
  function reserved(d) {{ return typeof d.method === "string" && d.method.indexOf(RESERVED) === 0; }}
  function toHost(msg) {{
    // Until the host has spoken we do not know WHICH canopy origin framed us;
    // a postMessage to a non-matching targetOrigin is silently dropped, so
    // offering it to each is exactly one delivery.
    var targets = parentOrigin ? [parentOrigin] : HOSTS;
    for (var i = 0; i < targets.length; i++) window.parent.postMessage(msg, targets[i]);
  }}
  function load(params) {{
    if (inner || !params || typeof params.html !== "string") return;
    inner = document.createElement("iframe");
    // Opaque, like this page: never allow-same-origin. The outer frame's flags
    // cap the inner's anyway, so a host override cannot widen this.
    inner.setAttribute("sandbox", "allow-scripts");
    inner.setAttribute("title", "app view");
    inner.srcdoc = params.html;
    document.body.appendChild(inner);
  }}
  window.addEventListener("message", function (ev) {{
    var d = ev.data;
    if (ev.source === window.parent) {{
      if (HOSTS.indexOf(ev.origin) < 0) return;
      if (parentOrigin && ev.origin !== parentOrigin) return;
      parentOrigin = ev.origin;
      if (!isRpc(d)) return;
      if (d.method === RESOURCE) {{ load(d.params); return; }}
      if (reserved(d)) return;
      if (inner && inner.contentWindow) inner.contentWindow.postMessage(d, "*");
      return;
    }}
    if (inner && ev.source === inner.contentWindow) {{
      if (!isRpc(d) || reserved(d)) return;
      toHost(d);
    }}
  }});
  toHost({{ jsonrpc: "2.0", method: READY, params: {{}} }});
}})();
</script></body></html>
"""


def sandbox_page(request: HttpRequest) -> HttpResponse:
    if request.method not in ("GET", "HEAD"):
        response = HttpResponse(status=405)
        response["Allow"] = "GET, HEAD"
        return response
    csp = mcp_apps.decode_csp(request.GET.get("csp", ""))
    # json.dumps into a <script>: origins are scheme://host[:port], but escape
    # `<` anyway so no value can ever end the script block.
    hosts = json.dumps(host_origins(request)).replace("<", "\\u003c")
    response = HttpResponse(_PAGE.format(hosts=hosts), content_type="text/html; charset=utf-8")
    response["Content-Security-Policy"] = mcp_apps.csp_header(
        csp, frame_ancestors=frame_ancestors(request))
    response["X-Content-Type-Options"] = "nosniff"
    response["Referrer-Policy"] = "no-referrer"
    response["Cache-Control"] = "no-store"
    response["Cross-Origin-Resource-Policy"] = "same-origin"
    return response


class McpAppsSandboxMiddleware:
    """Answers `/mcp-apps/…` before sessions, auth and CSRF exist.

    Placed right after the legacy-prefix rewrite and BEFORE SessionMiddleware, so
    `SESSION_SAVE_EVERY_REQUEST` cannot re-issue a signed-in viewer's session
    cookie on a response that frames untrusted HTML, and so the login wall never
    sees the path. The proxy needs no identity: it holds nothing and calls nothing.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request: HttpRequest):
        path = request.path_info
        if not path.startswith(PREFIX) and path != PREFIX.rstrip("/"):
            return self.get_response(request)
        if path in ("/mcp-apps/sandbox/", "/mcp-apps/sandbox"):
            return sandbox_page(request)
        response = HttpResponse("Not found", status=404, content_type="text/plain")
        response["Cache-Control"] = "no-store"
        return response

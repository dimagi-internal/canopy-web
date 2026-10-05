"""The old address (labs.connect.dimagi.com/canopy), after the move to the root.

canopy is served at the root of canopy.dimagi.com (2026-10-05): ``FORCE_SCRIPT_NAME``
is unset and the SPA is built with base ``/``. The old address still reaches the
same container — the shared ALB's ``/canopy/*`` rule — and
``config.asgi_prefix.StripScriptName`` strips the prefix and marks the scope.
This middleware decides what such a request gets:

* **A browser page** (GET/HEAD outside the machine prefixes) is redirected to the
  same path at ``CANOPY_PUBLIC_BASE_URL``. It cannot be served where it is: the
  SPA's assets and API calls are root-relative now, and the root of the labs
  host is connect-labs.
* **Everything else keeps working in place** — the API, OAuth and well-known
  documents, health — because runners, the canopy plugin, MCP clients, Slack,
  Gmail push and connected sites all hold the old address, and none of them
  follows a redirect reliably (a 302 turns a POST into a GET; a WebSocket never
  follows one; WebSockets do not pass through here at all). For those requests
  the script prefix is put back, so a URL generated for an old-address caller
  points at the old address, exactly as before the move.

Inert unless a request arrived prefixed, and inert while ``FORCE_SCRIPT_NAME`` is
set (a deployment still served under the prefix has nowhere to redirect to).
"""
from __future__ import annotations

from django.conf import settings
from django.http import HttpResponseRedirect
from django.urls import set_script_prefix

from config.asgi_prefix import SCOPE_KEY

#: Paths a machine calls at the old address. Matched against the STRIPPED path.
MACHINE_PREFIXES = ("/api/", "/oauth/", "/.well-known/", "/health/")


class LegacyPrefixMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        prefix = (getattr(request, "scope", None) or {}).get(SCOPE_KEY)
        if not prefix or settings.FORCE_SCRIPT_NAME:
            return self.get_response(request)

        base = (getattr(settings, "CANOPY_PUBLIC_BASE_URL", "") or "").rstrip("/")
        if base and request.method in ("GET", "HEAD") and not request.path.startswith(MACHINE_PREFIXES):
            # 302, not 301: a permanent redirect is cached by every browser that
            # sees it, which would make the move hard to undo.
            return HttpResponseRedirect(base + request.get_full_path())

        request.META["SCRIPT_NAME"] = prefix
        request.script_name = prefix
        set_script_prefix(prefix)
        return self.get_response(request)

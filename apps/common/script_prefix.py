"""Building a URL that points back at THIS request, under a script prefix.

canopy-web runs as a path-prefixed tenant on labs.connect.dimagi.com/canopy.
``config.asgi_prefix.StripScriptName`` removes ``/canopy`` from the incoming
ASGI scope so the Starlette mounts (MCP at ``/api/mcp``, Django at ``/``) match,
and ``FORCE_SCRIPT_NAME`` independently re-adds it to URLs Django *generates*
via ``reverse()``.

The gap between those two halves is ``request.path``. Django's ``ASGIRequest``
sets ``self.path = scope["path"]`` verbatim — unlike ``WSGIRequest``, which
rebuilds it as ``script_name + path_info`` — so under the stripping wrapper
``request.path`` and ``request.get_full_path()`` come back WITHOUT the prefix.

That is deliberate and load-bearing: every allowlist and route check in
``apps.common.middleware`` (``_is_public``, ``path.startswith("/api/")``, …)
is written against the stripped path. Do NOT "fix" it by restoring the prefix
in the ASGI layer — that silently reverses those checks.

What it breaks instead is any URL built to point back at the current request.
``request.get_full_path()`` yields ``/auth/cli/authorize/?…``, which on labs is
a *Connect Labs* path, not a canopy-web one: it 404s with Resolver404, or —
worse, as a ``?next=`` target — bounces the operator into a sibling tenant
after a successful login.

Use this helper for that case. It is the idiom already relied on by
``LoginRequiredMiddleware``; it is correct both locally (empty ``SCRIPT_NAME``)
and on the prefixed deployment, and it preserves the query string, which
matters for flows like the CLI authorize handshake that carry ``?cb``,
``?state`` and ``?label`` through an OAuth round trip.
"""
from __future__ import annotations

from django.http import HttpRequest


def self_full_path(request: HttpRequest) -> str:
    """Return the path+query of THIS request, including any script prefix.

    Prefer this over ``request.get_full_path()`` anywhere the result is handed
    back to a browser — a form ``action``, a ``?next=`` target, a redirect to
    self. See the module docstring for why the two differ.
    """
    return request.META.get("SCRIPT_NAME", "") + request.get_full_path_info()

"""ASGI middleware that strips a script-name prefix (e.g. ``/canopy``) from
incoming http/websocket scopes.

canopy-web runs as a path-prefixed tenant on labs.connect.dimagi.com/canopy, but
its monolith has no nginx to strip the prefix (ace-web relies on nginx for /ace).
The ALB forwards ``/canopy/...`` verbatim to the container, where the ASGI app is
a Starlette router mounting MCP at ``/api/mcp`` and Django at ``/`` — so an
unstripped ``/canopy/api/mcp`` would miss the MCP mount and ``/canopy/api/me``
would reach Django as ``/canopy/api/me`` (no route).

This middleware strips the prefix on the way IN (so the inner routers see
``/api/mcp``, ``/api/me``, …); ``FORCE_SCRIPT_NAME`` independently re-adds it to
URLs Django GENERATES (redirects, reverse(), static). Mirrors what ace-web's
nginx does, but keeps canopy's single-container model.

No-op when ``prefix`` is empty (i.e. every non-labs environment).

Since canopy moved to the root of canopy.dimagi.com (2026-10-05) the prefix is
LEGACY: ``FORCE_SCRIPT_NAME`` is unset, so Django generates root URLs, and only
the old address (labs.connect.dimagi.com/canopy) arrives prefixed. A stripped
scope is therefore marked with ``SCOPE_KEY`` so
``apps.common.legacy_prefix.LegacyPrefixMiddleware`` can tell the two apart —
re-adding the prefix to URLs generated for an old-address caller, and sending
browsers on to the new one. Deliberately NOT ``root_path``: Starlette's mounts
and Channels' URLRouter both read ``root_path`` and expect ``path`` to start
with it, which a stripped path no longer does.
"""
from __future__ import annotations

#: Set on a scope whose path arrived under the prefix; its value is the prefix.
SCOPE_KEY = "canopy.legacy_prefix"


class StripScriptName:
    def __init__(self, app, prefix: str):
        self.app = app
        self.prefix = (prefix or "").rstrip("/")

    async def __call__(self, scope, receive, send):
        if self.prefix and scope["type"] in ("http", "websocket"):
            path = scope.get("path", "")
            if path == self.prefix or path.startswith(self.prefix + "/"):
                stripped = path[len(self.prefix):] or "/"
                scope = dict(scope)
                scope["path"] = stripped
                scope[SCOPE_KEY] = self.prefix
                if scope.get("raw_path") is not None:
                    # preserve any query string already split out of raw_path
                    scope["raw_path"] = stripped.encode("utf-8")
        await self.app(scope, receive, send)

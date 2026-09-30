"""
ASGI config for canopy-web project.

It exposes the ASGI callable as a module-level variable named ``application``.

The FastMCP 3.x Streamable-HTTP server (apps/mcp) is mounted at /api/mcp/.
Auth is enforced INSIDE the MCP app via FastMCP MultiAuth (per-user PAT +
optional OAuth) — there is no hand-rolled gate here anymore.

Streamable-HTTP requires the MCP app's lifespan to run for session
management. Django's bare ASGI app has no lifespan, so we build a
combined app: a Starlette router that mounts the Django ASGI app at "/"
and the MCP app under /api/mcp, with the MCP app's lifespan wired into
the Starlette app. Starlette owns the ASGI `lifespan` events; Django
sub-mounts only ever see `http`/`websocket` scopes.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.development")

from django.core.asgi import get_asgi_application  # noqa: E402

# Initialize Django (populates the app registry) before importing any
# module that touches ORM models (apps.mcp.server imports tools that
# import services/models).
_django_asgi_app = get_asgi_application()

from channels.routing import ProtocolTypeRouter, URLRouter  # noqa: E402
from channels.security.websocket import AllowedHostsOriginValidator  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402

from apps.canopy_sessions.routing import websocket_urlpatterns as chat_ws_urlpatterns  # noqa: E402
from apps.mcp.server import build_http_app  # noqa: E402
from apps.realtime.channels_auth import RealtimeAuthMiddleware  # noqa: E402
from apps.realtime.routing import websocket_urlpatterns as realtime_ws_urlpatterns  # noqa: E402

_websocket_urlpatterns = realtime_ws_urlpatterns + chat_ws_urlpatterns

_MCP_PREFIX = "/api/mcp"

# Streamable-HTTP ASGI app. path="/" -> the MCP endpoint is the mount
# root, i.e. /api/mcp/.
_mcp_app = build_http_app()

# The catch-all: HTTP goes to Django as before; WebSocket goes through the
# realtime handshake auth + URL router (apps/realtime). Same shape ace-web uses.
_django_with_ws = ProtocolTypeRouter(
    {
        "http": _django_asgi_app,
        "websocket": AllowedHostsOriginValidator(
            RealtimeAuthMiddleware(URLRouter(_websocket_urlpatterns))
        ),
    }
)

from apps.mcp.delegation import gate as _dpop_gate  # noqa: E402

application = Starlette(
    routes=[
        # The DPoP gate: a host-grant token (canopy-web as a host of its own
        # MCP, apps/tokens/self_host.py) arrives as `Authorization: DPoP`; the
        # gate verifies the proof and hands FastMCP a plain bearer. Every other
        # request passes through untouched.
        Mount(_MCP_PREFIX, app=_dpop_gate(_mcp_app)),
        # Django + realtime WS handle everything else (mounted last as catch-all).
        Mount("/", app=_django_with_ws),
    ],
    # Run the MCP session-manager lifespan for the whole process.
    lifespan=_mcp_app.lifespan,
)



def _mcp_without_slash(app):
    """`/api/mcp` reaches the MCP app, not Django.

    Claude Code stores an MCP URL without its trailing slash (`claude mcp add
    … /api/mcp/` becomes `…/api/mcp`), and Starlette's `Mount("/api/mcp")`
    matches only `/api/mcp/…`, so the bare path fell through to Django: a 401
    with no OAuth challenge before sign-in, and a 404 HTML page for every MCP
    call after it (2026-09-30)."""

    async def wrapped(scope, receive, send):
        if scope["type"] in ("http", "websocket") and scope.get("path") == _MCP_PREFIX:
            scope = {**scope, "path": _MCP_PREFIX + "/", "raw_path": (_MCP_PREFIX + "/").encode()}
        await app(scope, receive, send)

    return wrapped


application = _mcp_without_slash(application)

# When deployed under a path prefix (labs.connect.dimagi.com/canopy), strip it
# from incoming scopes so the mounts above (MCP at /api/mcp, Django at /) match.
# FORCE_SCRIPT_NAME independently re-adds the prefix to URLs Django generates.
# No-op everywhere the env var is unset (GCP/dev/CI).
from config.asgi_prefix import StripScriptName  # noqa: E402

_script_name = os.environ.get("FORCE_SCRIPT_NAME", "")
if _script_name:
    application = StripScriptName(application, _script_name)

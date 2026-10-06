"""Which web pages may open a canopy WebSocket.

Channels' `AllowedHostsOriginValidator` checks a socket's Origin against
ALLOWED_HOSTS — and this deployment sets ALLOWED_HOSTS to `*`, because the
load balancer's health check arrives with the container's IP as its Host. So
the check passed every origin: any web page could open a socket to canopy, and
only the token stood between it and a conversation.

This validator decides from origins instead of hosts:

* **A browser always sends Origin** on a WebSocket. It must be canopy's own
  address (current or former) or a live connected site's declared origin — the
  same list that may call the API cross-origin (`apps/tokens/cors.py`).
* **No Origin** means a non-browser client — a script, the e2e checks.
  (The runners' `websocket-client` does send one: the host it connects to,
  which is canopy's own.) Cross-site WebSocket hijacking is a browser attack, and these clients
  authenticate by token alone, so they pass through to the token check.

With DEBUG on (local development, tests) every origin is allowed, as before.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from channels.db import database_sync_to_async
from django.conf import settings


def _origin_of(url: str) -> str:
    parts = urlsplit(url or "")
    return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else ""


def own_origins() -> set[str]:
    """canopy's own addresses: where people visit it, its identity, and any it has left."""
    urls = [getattr(settings, "CANOPY_PUBLIC_BASE_URL", ""),
            getattr(settings, "CANOPY_IDENTITY_BASE_URL", ""),
            *(getattr(settings, "CANOPY_FORMER_BASE_URLS", None) or [])]
    return {o for o in map(_origin_of, urls) if o}


@database_sync_to_async
def allowed(origin: str) -> bool:
    if origin in own_origins():
        return True
    from apps.tokens.models import AppCredential

    return any(origin in (origins or []) for origins in AppCredential.objects.filter(
        revoked_at__isnull=True).values_list("allowed_frame_origins", flat=True))


class OriginValidator:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket" and not settings.DEBUG:
            origin = next((v.decode("latin1") for k, v in scope.get("headers", [])
                           if k == b"origin"), None)
            if origin is not None and not await allowed(origin.rstrip("/")):
                # Channels' own refusal: take the connect, close before accept
                # (the server answers the handshake 403).
                await receive()
                await send({"type": "websocket.close", "code": 403})
                return
        return await self.app(scope, receive, send)

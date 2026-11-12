"""The answer for a WebSocket path no route matches: there is no such thing here.

Channels' URLRouter raises `ValueError("No route found")` for an unmatched path,
which the ASGI server turns into a handshake 500. A client cannot tell that from a
server fault, so it reconnects forever: a session link with a stray trailing
character (`…/sessions/<uuid>.` from a sentence-ending period) is one tab hammering
the server and paging the 5xx alarm. This route is matched last, accepts, and
closes with 4004, which the chat client reads as "no such conversation" and stops
asking (`REFUSAL_CLOSE_CODES` in canopy-ui's useSessionSocket).

It accepts before closing because a close sent before accept() rejects the
handshake at the HTTP level, and the browser then sees 1006 rather than 4004.
"""
from __future__ import annotations

import logging

from channels.generic.websocket import AsyncWebsocketConsumer

log = logging.getLogger(__name__)

NOT_FOUND = 4004


class UnknownPathConsumer(AsyncWebsocketConsumer):
    async def connect(self):
        log.info("websocket path matches no route: %r", (self.scope.get("path") or "")[:200])
        await self.accept()
        await self.close(code=NOT_FOUND)

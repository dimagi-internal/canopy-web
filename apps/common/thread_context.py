"""Give each WebSocket connection and each MCP request its own sync thread.

`database_sync_to_async` / `sync_to_async(thread_sensitive=True)` runs a call on the executor
of the current `ThreadSensitiveContext`, and with no context on ONE thread shared by the whole
process. Django's HTTP handler opens a context per request; Channels consumers and the FastMCP
apps do not. So every runner socket, browser socket and MCP call queued behind one thread: a
single slow query or row-lock wait stalled every runner heartbeat, and past the offline
threshold the fleet read as down.

This middleware opens the context Django would, once per connection (a WebSocket) or per
request (MCP HTTP), so each gets its own thread for its whole life and keeps thread affinity
for its own calls. On the way out it closes that thread's DB connections, and the thread
is joined.

`CANOPY_PER_CONNECTION_THREADS=0` restores the shared thread.
"""
from __future__ import annotations

import os

from asgiref.sync import ThreadSensitiveContext, sync_to_async
from django.db import connections
from fastmcp.server.middleware import Middleware


async def close_thread_connections() -> None:
    """Close the DB connections the context's thread opened, before the thread is joined.

    Django connections are per thread. A plain `sync_to_async` ORM call (MCP uses many) leaves
    its connection open, and a thread that exits without closing it holds a socket on the
    shared RDS until garbage collection. Django's own handler closes at request end the same
    way (`request_finished`).

    A connection inside an atomic block is left open: its transaction belongs to a caller
    further out on the same thread, not to this request. That never happens on a request's own
    thread; it happens when a sync caller runs the app through `async_to_sync` (every test that
    calls an MCP tool in-process), where `thread_sensitive` runs this on the caller's thread.
    Closing there killed the caller's transaction — on PostgreSQL every later query failed with
    "the connection is closed" (SQLite's in-memory test database ignores close())."""
    await sync_to_async(_close_idle_connections, thread_sensitive=True)()


def _close_idle_connections() -> None:
    for conn in connections.all(initialized_only=True):
        if not conn.in_atomic_block:
            conn.close()


def enabled() -> bool:
    return os.environ.get("CANOPY_PER_CONNECTION_THREADS", "1").strip().lower() not in ("0", "false", "no", "off")


class PerConnectionThread:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") not in ("http", "websocket") or not enabled():
            return await self.app(scope, receive, send)
        async with ThreadSensitiveContext():
            try:
                return await self.app(scope, receive, send)
            finally:
                await close_thread_connections()


def mcp_middleware():
    """The same, for an MCP message. A stateful Streamable-HTTP session would run its handlers in
    a task the session manager started at lifespan, outside any HTTP request's context, where the
    ASGI wrapper alone never reaches a tool call; the servers are stateless, and this keeps a tool
    call on its own thread either way. Register FIRST (outermost), so the scope
    middlewares' own DB reads run inside it too."""
    class McpPerMessageThread(Middleware):
        async def on_message(self, context, call_next):
            if not enabled():
                return await call_next(context)
            async with ThreadSensitiveContext():
                try:
                    return await call_next(context)
                finally:
                    await close_thread_connections()

    return McpPerMessageThread()

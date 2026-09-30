"""`/api/mcp` without its slash reaches the MCP app (config/asgi.py).

Claude Code stores an MCP URL without the trailing slash; Starlette's mount
matches only `/api/mcp/…`, so the bare path used to fall through to Django.
"""
from __future__ import annotations

from asgiref.sync import async_to_sync


def _seen(path):
    from config.asgi import _mcp_without_slash

    seen = {}

    async def app(scope, receive, send):
        seen.update(scope)

    async def call():
        await _mcp_without_slash(app)({"type": "http", "path": path, "raw_path": path.encode()}, None, None)

    async_to_sync(call)()
    return seen["path"], seen["raw_path"]


def test_the_bare_path_is_the_mcp_endpoint():
    assert _seen("/api/mcp") == ("/api/mcp/", b"/api/mcp/")


def test_other_paths_are_untouched():
    for path in ("/api/mcp/", "/api/mcpx", "/api/me/", "/"):
        assert _seen(path) == (path, path.encode())

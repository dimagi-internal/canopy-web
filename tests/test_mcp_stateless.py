"""canopy-web's MCP servers are stateless Streamable-HTTP (apps/mcp/server.py::build_http_app), so
any web process or task answers any call: no `mcp-session-id` lives in one process's memory."""
from __future__ import annotations

import pytest


@pytest.mark.parametrize("module", ["apps.mcp.server", "apps.mcp.projects", "apps.mcp.project_gateway"])
def test_built_stateless(module):
    import importlib

    app = importlib.import_module(module).build_http_app()
    (route,) = [r for r in app.routes if getattr(r, "path", None) == "/"]
    # A stateful server also serves GET (the standalone server-to-client stream) and keeps
    # sessions in its process; the stateless transport serves only POST and DELETE.
    assert "GET" not in route.methods
    assert "POST" in route.methods

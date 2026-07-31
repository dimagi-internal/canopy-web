"""MCP tool registration.

Importing this package registers every tool against the `mcp` instance
(via the `@mcp.tool` decorators in the submodules). server.py imports it
exactly once, after the FastMCP instance is constructed.
"""
from . import (
    insights,  # noqa: F401
    schedules,  # noqa: F401
    sessions,  # noqa: F401
)

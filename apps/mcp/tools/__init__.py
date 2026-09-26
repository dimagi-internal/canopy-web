"""MCP tool registration.

Importing this package registers every tool against the `mcp` instance
(via the `@mcp.tool` decorators in the submodules). server.py imports it
exactly once, after the FastMCP instance is constructed.
"""
from . import (
    caller,  # noqa: F401
    conversations,  # noqa: F401
    insights,  # noqa: F401
    items,  # noqa: F401
    page,  # noqa: F401
    schedules,  # noqa: F401
    sessions,  # noqa: F401
    site,  # noqa: F401
    skill_history,  # noqa: F401
    slack,  # noqa: F401
)

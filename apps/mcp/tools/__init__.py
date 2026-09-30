"""Hand-written MCP tool registration — only for what is not a REST route.

Every REST route is already a tool (`apps/mcp/api_tools.py`); a tool belongs
here only when there is no route for it to be.

Importing this package registers every tool against the `mcp` instance
(via the `@mcp.tool` decorators in the submodules). server.py imports it
exactly once, after the FastMCP instance is constructed.
"""
from . import (
    caller,  # noqa: F401
    conversations,  # noqa: F401
    page,  # noqa: F401
    sessions,  # noqa: F401
    site,  # noqa: F401
    slack,  # noqa: F401
)

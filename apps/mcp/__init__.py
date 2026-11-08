"""canopy-web MCP server (FastMCP 4.x).

A best-practice in-process MCP server: tools are explicit Python
functions that run AS the authenticated user (resolved from a per-user
Personal Access Token or an OAuth login), exposed over Streamable HTTP
and mounted into the Django ASGI app at /api/mcp/.

This module is intentionally written as a portable "house pattern" —
auth.py / server.py / tools/ / rate_limit.py / models.py — so it can be
lifted into sibling Django projects with minimal change. Only the tool
bodies and the PAT store reference are project-specific.
"""

default_app_config = "apps.mcp.apps.McpConfig"

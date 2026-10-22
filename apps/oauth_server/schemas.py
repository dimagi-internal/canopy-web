"""Schemas for the MCP clients a person connected by signing in."""
from __future__ import annotations

import datetime as dt

from apps.common.schemas import StrictModel


class McpClientOut(StrictModel):
    """An MCP client a person connected by signing in (an `OAuthGrant`)."""
    id: int
    client_name: str
    connected_at: dt.datetime
    last_used_at: dt.datetime | None = None


class McpClientsOut(StrictModel):
    #: The address to give an MCP client — it finds the sign-in from there.
    mcp_url: str
    apps: list[McpClientOut]

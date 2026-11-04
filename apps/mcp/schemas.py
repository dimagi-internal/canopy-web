"""Request/response models for the MCP audit log route (`apps/mcp/audit_api.py`)."""
from __future__ import annotations

from apps.common.schemas import StrictModel


class McpCallOut(StrictModel):
    id: int
    created_at: str
    workspace: str
    user_email: str
    tool: str
    args_summary: str
    ok: bool
    error: str
    #: A call made with an act-as-you token: the agent that acted as `user_email`, and
    #: the permission it held. "" otherwise.
    acting_agent: str = ""
    act_as_grant: str = ""


class McpCallListOut(StrictModel):
    items: list[McpCallOut]

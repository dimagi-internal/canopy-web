"""Django-free ACP (Agent Client Protocol) client for canopy's runners.

ACP is the open standard canopy hand-rolled: emdash drives Claude through
`@agentclientprotocol/claude-agent-acp` wrapping `@anthropic-ai/claude-agent-sdk`,
and the protocol already specifies tool-call lifecycle, streamed reply text,
thinking, usage and rate limits. See
`docs/superpowers/specs/2026-07-27-acp-adoption-design.md`.

Same precedent as `canopy_cron` and `canopy_transcript`: shared by the server
and the runners so a behaviour can't exist in two versions.
"""
from .client import (
    AcpAgent,
    AcpConnection,
    PermissionDecision,
    Pending,
    allow_first_option,
    find_adapter,
)
from .menus import menu_from_permission_request, option_id_for
from .updates import TERMINAL_STATUSES, ToolCall, UpdateReducer

__all__ = [
    "AcpAgent", "AcpConnection", "Pending", "PermissionDecision",
    "TERMINAL_STATUSES", "ToolCall", "UpdateReducer",
    "allow_first_option", "find_adapter",
    "menu_from_permission_request", "option_id_for",
]

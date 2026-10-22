"""Pydantic schemas for the /api/tokens/ surface."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel
from apps.contacts.people_schemas import SessionMemoryOut


class PersonalTokenOut(StrictModel):
    """A token as listed to its owner. Never contains the raw value."""
    id: int
    label: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
    last_used_client: str = ""
    """What last used it: the caller's User-Agent (a runner sends its host)."""
    revoked_at: dt.datetime | None = None
    expires_at: dt.datetime | None = None
    """When the token stops authenticating; null means it never expires."""


class PersonalTokenCreateIn(StrictModel):
    label: str = Field(min_length=1, max_length=200)
    ttl_days: int | None = Field(
        default=None,
        ge=0,
        description=(
            "Days until this token expires. Omit for the server default "
            "(PAT_DEFAULT_TTL_DAYS, 180). 0 means it never expires. There is no "
            "upper bound — with 0 available, a cap would only hand a caller a "
            "shorter token than they asked for without telling them."
        ),
    )


class PersonalTokenCreatedOut(PersonalTokenOut):
    """Returned exactly once at creation — includes the raw token."""
    raw: str


class EmbedAgentOut(StrictModel):
    """One agent an embedding app may offer the caller — `GET /api/embed/agents`.

    Deliberately thin: what a picker needs to render a choice, and nothing that
    describes how the agent RUNS. An embedded widget is the least trusted
    surface canopy has, so runtime detail (repo, engine, secret-reference names,
    runner assignments) stays on the owner-only agent routes.

    `workspace` is included because it is the one non-cosmetic field — it tells
    the host which tenant a session started with this agent will belong to.
    """

    slug: str
    name: str
    description: str
    avatar_url: str
    workspace: str


class EmbedSelfOut(StrictModel):
    """`GET /api/embed/self` — whether canopy offers the widget on its own pages.

    Its own endpoint rather than a field on `/api/me/` because it is a
    deployment setting, not a property of the user, and the frontend reads it
    once to decide whether to mount anything at all.
    """

    enabled: bool
    app: str
    #: Optional preselected agent; empty means show the picker.
    agent: str


class WsTicketOut(StrictModel):
    #: Put on the socket URL as `?ticket=`; good for one socket, for `expires_in` seconds.
    ticket: str
    expires_in: int


class EmbedSelfTokenOut(StrictModel):
    """`POST /api/embed/token` — a delegated token for the caller.

    Same shape a third-party host's own token endpoint returns to its page, so
    the frontend code path is identical whether the host is canopy or not.
    """

    token: str
    expires_at: str
    #: Whether canopy also holds a grant to act as you at its OWN tools for
    #: the page you named (`?page=`) — see `apps/tokens/self_host.py`. False
    #: for an unregistered page, or when canopy-web is not set up as a host.
    host_grant: bool = False


class EmbedSessionMemoryOut(SessionMemoryOut):
    """`GET /api/embed/sessions/{id}/agent-memory` — agent memory in this session
    and what you granted its agent, as the widget may SHOW it. Read-only: a site
    acting for you never changes it or grants anything; `manage_path` is where you
    do that yourself, in canopy."""

    #: canopy's own chat page for this session, relative to canopy's base.
    manage_path: str


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

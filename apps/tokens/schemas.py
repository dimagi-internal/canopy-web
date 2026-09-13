"""Pydantic schemas for the /api/tokens/ surface."""
from __future__ import annotations

import datetime as dt

from pydantic import Field

from apps.common.schemas import StrictModel


class PersonalTokenOut(StrictModel):
    """A token as listed to its owner. Never contains the raw value."""
    id: int
    label: str
    created_at: dt.datetime
    last_used_at: dt.datetime | None = None
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

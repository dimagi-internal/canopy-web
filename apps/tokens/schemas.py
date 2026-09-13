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


class GitHubConnectionOut(StrictModel):
    """Response for GET /api/tokens/github.

    Deliberately says nothing about the token itself — not a masked form, not a
    length, not an expiry of the access token (there is no stored access token
    to have an expiry). The only questions the UI asks are "is this deployment
    set up", "am I connected", "as whom", and "do I need to press the button
    again".
    """

    configured: bool = Field(
        description=(
            "Whether this DEPLOYMENT has GitHub App credentials at all. False on a "
            "fresh checkout and before the client secret is set, which is a real "
            "state rather than an error — the UI says 'not set up' instead of "
            "offering a button that cannot work."
        )
    )
    connected: bool = Field(description="Whether the caller has a stored grant.")
    github_login: str = Field(
        default="",
        description="The GitHub account this grant belongs to, so a user can tell "
                    "they connected the one they meant to. Display only — never "
                    "used for authorization.",
    )
    needs_reconnect: bool = Field(
        default=False,
        description=(
            "The stored grant can no longer mint an access token: the refresh token "
            "is missing, expired, or was rejected (revoked by the user, or the app's "
            "permissions changed and the install has not re-approved). One flag "
            "because all three have the same remedy."
        ),
    )
    install_url: str = Field(
        default="",
        description="Where to install the app on another account or org. Empty when "
                    "the app slug is unset, and the UI then omits the link rather "
                    "than rendering one that 404s.",
    )


class GitHubInstallationOut(StrictModel):
    """One place the caller has installed the app — the owner dropdown's source.

    `installation_id` is included for display/debugging only. Nothing
    authorizes on it: GitHub warns that a spoofed `installation_id` can be
    posted to the callback, so the server always re-reads this list with the
    user's own token rather than trusting an id from a client.
    """

    installation_id: int
    account_login: str
    account_type: str
    is_org: bool

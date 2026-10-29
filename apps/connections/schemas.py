"""Schemas for connections (`/api/connections…`, an agent's connections, my CLI token).

Nothing here carries a token except `AccessTokenOut`, which only the two
token-handout routes return.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import Field

from apps.common.schemas import StrictModel


class ProviderOut(StrictModel):
    name: str = Field(description="The provider's stable key, e.g. `salesforce`.")
    title: str = Field(description="What to call it in the UI.")
    configured: bool = Field(
        description="Whether this deployment has credentials for the provider. False is a real "
                    "state, not an error: the UI says 'not set up' instead of offering a button that cannot work."
    )


class ConnectionOut(StrictModel):
    id: int
    provider: str
    title: str = Field(description="The provider's display name.")
    account_label: str = Field(description="Which account this is (e.g. a Salesforce username).")
    instance_url: str
    needs_reconnect: bool = Field(description="The stored grant can no longer mint a token; connect again.")
    connected_at: datetime
    last_used_at: datetime | None = None
    connected_by_email: str = Field(
        description="Who completed the sign-in; empty when that person's account no longer exists.")
    granted_from: str | None = Field(
        default=None,
        description="For an agent's list: the slug of the agent that owns this connection, "
                    "when it is shared with this agent rather than its own.",
    )
    shared_with: list[str] = Field(
        default_factory=list,
        description="For an agent's own connection: slugs of the agents it is shared with, sorted. "
                    "Empty for a person's connection and for one shared with this agent.",
    )


class ConnectionsOut(StrictModel):
    providers: list[ProviderOut]
    connections: list[ConnectionOut]


class AccessTokenOut(StrictModel):
    access_token: str
    instance_url: str
    expires_at: datetime
    account_label: str

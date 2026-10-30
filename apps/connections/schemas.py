"""Schemas for connections (`/api/connections…`, an agent's connections, a workspace's
connection apps, the token routes).

Nothing here carries a token except `AccessTokenOut`, which only the two
token-handout routes return.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from apps.common.schemas import StrictModel


class ProviderOut(StrictModel):
    name: str = Field(description="The provider's stable key, e.g. `salesforce`: the `{provider}` in every "
                                  "connection URL.")
    title: str = Field(description="What to call it in the UI, e.g. `Salesforce`.")
    workspace: str = Field(
        default="",
        description="The workspace whose app this row connects with: the `{workspace}` in "
                    "`/auth/connections/{provider}/{workspace}/start/`. Empty on a person's row for a "
                    "provider no workspace of theirs has set up.")
    workspace_name: str = Field(default="", description="That workspace's display name.")
    configured: bool = Field(
        description="Whether the workspace has an app (client id and secret) for the provider. False "
                    "is a real state, not an error: the UI says 'not set up' instead of offering a button "
                    "that cannot work."
    )


class ScopeChoiceOut(StrictModel):
    scope: str = Field(description="The scope as the provider names it.")
    label: str = Field(description="What it allows, in words.")


class ConnectionOut(StrictModel):
    id: int
    provider: str
    title: str = Field(description="The provider's display name.")
    workspace: str = Field(description="The workspace whose app made this connection.")
    workspace_name: str = Field(description="That workspace's display name.")
    account_label: str = Field(description="Which account this is (e.g. a Salesforce username).")
    instance_url: str = Field(
        description="The account's own API base URL, for a provider that has one per account "
                    "(Salesforce); empty for a provider whose API host is fixed.")
    scopes: list[str] = Field(
        default_factory=list,
        description="What the grant allows at the provider (e.g. `api`, `refresh_token`).",
    )
    needs_reconnect: bool = Field(description="The stored grant can no longer mint a token; connect again.")
    usable: bool = Field(
        description="Whether it can hand out a token now. A person's: only while they can still read "
                    "`workspace` (False once they have left it; disconnect it). An agent's own: only while the "
                    "agent is in `workspace` (False once it has moved; disconnect it and connect again in its "
                    "workspace). One shared with an agent: only while its owner is in `workspace`.")
    missing_scopes: list[ScopeChoiceOut] = Field(
        default_factory=list,
        description="Scopes the app now asks for that this grant lacks (an admin chose them after it was "
                    "connected), with what each allows; reconnect to allow them. Empty when nothing is missing.")
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


class OptionFieldOut(StrictModel):
    name: str = Field(description="The key in `options`.")
    label: str
    kind: Literal["url", "int", "text"]
    required: bool
    default: str | int | None = None
    help: str = Field(default="", description="What to enter and how it is used, for the form to show.")


class ConnectionAppOut(StrictModel):
    provider: str = Field(description="The provider's key, e.g. `salesforce`.")
    title: str = Field(description="The provider's display name.")
    configured: bool = Field(description="A client id and secret are saved, so people and agents can connect.")
    client_id: str = Field(description="The OAuth client id (public); empty when nothing is saved.")
    has_secret: bool = Field(description="A client secret is saved and readable. The secret itself is never "
                                         "returned. False when the saved one can no longer be decrypted: "
                                         "enter it again.")
    options: dict[str, Any] = Field(default_factory=dict,
                                    description="The provider's own settings, as saved (see `option_fields`).")
    scopes: list[str] = Field(default_factory=list,
                              description="Extra scopes chosen from `scope_choices`, asked for on every connect.")
    scope_choices: list[ScopeChoiceOut] = Field(default_factory=list,
                                                description="Extra scopes this provider offers.")
    option_fields: list[OptionFieldOut] = Field(default_factory=list,
                                                description="The settings this provider takes.")
    callback_url: str = Field(description="Register this as the callback (redirect) URL of the OAuth client "
                                          "at the provider.")
    locked_options: list[str] = Field(
        default_factory=list,
        description="The `option_fields` that name the provider's authorization server (Salesforce: "
                    "`login_url`): like `client_id`, they cannot change while `connection_count` is above zero "
                    "(409 `app_locked`).")
    connection_count: int = Field(description="Connections made with this app; removing it disconnects them.")
    updated_at: datetime | None = None
    configured_by_email: str = Field(default="", description="Who last saved it; empty if unknown.")


class ConnectionAppsOut(StrictModel):
    apps: list[ConnectionAppOut] = Field(description="One row per provider, set up or not.")


class ConnectionAppIn(StrictModel):
    client_id: str = Field(max_length=255, description="The OAuth client id (consumer key) from the provider.")
    client_secret: str = Field(
        default="", max_length=4096,
        description="The client secret. Blank keeps the saved one (send a new one to rotate it); required "
                    "the first time and with a new client id.")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="The provider's settings (`option_fields`), merged over the saved ones.")
    scopes: list[str] = Field(
        default_factory=list,
        description="The full list of extra scopes, from `scope_choices`; replaces the saved list.")


class AccessTokenOut(StrictModel):
    access_token: str
    instance_url: str = Field(description="The account's API base URL; empty when the provider's is fixed.")
    expires_at: datetime
    account_label: str


class TurnConnectionsOut(StrictModel):
    providers: list[str] = Field(
        description="Provider keys the turn's agent has a connection for (its own or one shared "
                    "with it), sorted. Empty for most agents.")

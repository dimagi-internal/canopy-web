from __future__ import annotations

from pydantic import Field

from apps.common.schemas import StrictModel

#: The longest history window an owner may allow — a day. A policy the owner
#: picks inside this, not a second policy.
HISTORY_MAX_MINUTES_LIMIT = 24 * 60


class SlackCommandsOut(StrictModel):
    # canopy can edit the Slack app's slash commands (app id + config token).
    managed: bool
    app_id: str
    set_by_email: str
    synced_at: str
    error: str


class SlackAgentOut(StrictModel):
    # Whether the Slack app is declared an agent — Slack's own "Working…"
    # indicator and its Stop button are drawn only for one that is.
    declared: bool
    declared_at: str


class SlackHistoryOut(StrictModel):
    # `--history <minutes>`: may an ask hand the agent the channel's recent
    # past, and how far back. This workspace's own policy.
    enabled: bool
    max_minutes: int


class SlackHistoryIn(StrictModel):
    enabled: bool
    max_minutes: int = Field(ge=1, le=HISTORY_MAX_MINUTES_LIMIT)


class SlackChannelAllowOut(StrictModel):
    id: str
    agent: str
    channel: str
    channel_name: str
    follow: bool
    note: str
    added_by_email: str
    added_at: str


class SlackChannelAllowIn(StrictModel):
    agent: str = Field(description="The agent's slug (an agent of this workspace).")
    channel: str = Field(description="The Slack channel: its id (C… or G…) or #name.")
    follow: bool = Field(default=False, description="Also allow following new messages, not only one read.")
    note: str = Field(default="", max_length=500, description="Why — shown on the settings page and in the log.")


class SlackChannelIngestSwitchOut(StrictModel):
    # Whether this workspace's agents may ASK to read a whole channel (each
    # read still needs an in-channel approval).
    enabled: bool
    # Channels an owner allow-listed for one agent: a matching request reads at
    # once, with a notice in the channel instead of Approve/Decline.
    allowed: list[SlackChannelAllowOut]


class SlackChannelIngestSwitchIn(StrictModel):
    enabled: bool


class SlackConfigOut(StrictModel):
    workspace: str
    connected: bool
    team_name: str
    installed_by_email: str
    installed_at: str
    install_url: str
    commands: SlackCommandsOut
    agent: SlackAgentOut
    history: SlackHistoryOut
    channel_ingest: SlackChannelIngestSwitchOut


class ChannelIngestIn(StrictModel):
    agent: str = Field(description="The agent's slug.")
    channel: str = Field(description="The Slack channel: its id (C… or G…), or #name.")
    purpose: str = Field(min_length=1, max_length=2000,
                         description="Why the agent wants the channel; shown to the people asked to approve.")
    follow: bool = Field(default=False, description="Keep reading new messages until revoked.")


class ChannelIngestOut(StrictModel):
    id: str
    agent: str
    workspace: str
    channel: str
    channel_name: str
    purpose: str
    follow: bool
    status: str = Field(description="pending | reading | ready | declined | revoked | failed")
    requested_by_email: str
    created_at: str
    decided_by_email: str
    decided_by_slack_id: str
    decided_at: str
    revoked_at: str
    messages: int
    backfilled_at: str
    followed_at: str
    error: str
    # Where the transcript is read — by the agent's own login only.
    transcript_url: str
    read_mark: int = Field(description="The agent's read mark: pass it as `after` to get only newer messages")
    read_at: str


class ChannelIngestTranscriptOut(StrictModel):
    id: str
    status: str
    messages: int = Field(description="Messages in this markdown (only the new ones when `after` was given)")
    markdown: str
    total: int = Field(description="Messages in the whole transcript")
    after: int = Field(description="The read mark this read started after (0 = the whole transcript)")
    mark: int = Field(description="The read mark to record once these messages are kept")
    first_ts: str = Field(description="Slack ts of the oldest message here ('' when none)")
    last_ts: str = Field(description="Slack ts of the newest message here ('' when none)")


class ChannelIngestReadMarkIn(StrictModel):
    mark: int = Field(ge=0, description="The `mark` a transcript read returned")


class SlackConfigTokenIn(StrictModel):
    refresh_token: str


class SlackSyncOut(StrictModel):
    status: str
    detail: str = ""
    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    unfit: list[str] = []
    #: Bot scopes newly put on the app — they take effect only on a re-install.
    scopes_added: list[str] = []
    #: Webhooks (`event_subscriptions`, `interactivity`) moved off an address
    #: canopy has left.
    webhooks_moved: list[str] = []
    #: Sign-in callbacks removed from the app because they sat on an address
    #: canopy has left.
    redirects_removed: list[str] = []


class SlackDeclareAgentOut(StrictModel):
    status: str
    detail: str = ""
    changed: list[str] = []
    reinstall_required: bool = False
    install_url: str = ""

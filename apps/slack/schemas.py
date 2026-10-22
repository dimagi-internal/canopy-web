from __future__ import annotations

import uuid

from ninja import Schema
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


class SlackReinstallOut(StrictModel):
    # Whether this install is missing permissions canopy now asks for — they
    # land only when someone re-installs — and, in plain words, what they turn
    # on. False when every one is granted, or when Slack has not said.
    needed: bool
    turns_on: list[str]


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
    reinstall: SlackReinstallOut
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
    #: The app was made an agent in this sync (Slack's own working indicator).
    declared: list[str] = []


class SlackDeclareAgentOut(StrictModel):
    status: str
    detail: str = ""
    changed: list[str] = []
    reinstall_required: bool = False
    install_url: str = ""


# ---- a session's Slack thread: share, detach, new thread (session_api.py) ----


class SlackDetachIn(Schema):
    """Which session to disconnect — the same identifiers `share_session_to_slack`
    takes, tried in this order: canopy's `session_id`, the Claude Code session id
    (`$CLAUDE_CODE_SESSION_ID`), the emdash task + project. An agent names its
    own conversation with `turn_id` instead (its caller envelope's `turn_id`)."""
    session_id: str = ""
    claude_session_id: str = ""
    emdash_task: str = ""
    emdash_project: str = ""
    #: A live turn of the caller's own: the agent's own login acting on the
    #: conversation it is having (`access.own_turn_session`). Tried first.
    turn_id: str = ""


class SlackShareIn(Schema):
    """What to post, where, and which session it is about."""
    #: Markdown: what we are doing, why, and where it stands.
    summary: str
    #: A channel name (`#dev`) or id; the canopy app must be in it. Optional when
    #: the session is already bound (the update goes into its own thread).
    channel: str = ""
    #: `broadcast` (one post) or `bind` (the thread becomes the session's own).
    mode: str = "broadcast"
    claude_session_id: str = ""
    emdash_task: str = ""
    emdash_project: str = ""
    session_id: str = ""
    #: Needed only when you belong to several workspaces with Slack connected
    #: and no session is named.
    workspace: str = ""
    #: An agent's own live turn (the caller envelope's `turn_id`) — tried first.
    turn_id: str = ""


class SlackShareOut(Schema):
    #: "shared", or "updated" when a bound session's thread got the summary.
    status: str
    message: str
    mode: str
    channel: str = ""
    ts: str = ""
    permalink: str = ""
    session_id: uuid.UUID | None = None


class SlackNewThreadIn(Schema):
    """Which conversation to move, and what the new thread opens with."""
    #: Markdown: what the new thread is for. Required — it is the thread's first post.
    summary: str
    #: An agent's own live turn (the caller envelope's `turn_id`) — tried first.
    turn_id: str = ""
    session_id: str = ""
    claude_session_id: str = ""


class SlackPlaceOut(Schema):
    channel: str
    thread_ts: str
    permalink: str = ""


class SlackNewThreadOut(Schema):
    #: "moved".
    status: str
    message: str
    session_id: uuid.UUID
    channel: str
    #: The channel's name when canopy knows it (needs Slack's channels:read); "" otherwise.
    channel_name: str = ""
    thread_ts: str
    permalink: str = ""
    #: The thread the conversation left (it was told where it went).
    previous: SlackPlaceOut


class SlackDetachOut(Schema):
    #: "detached", or "not_bound" when there was no thread to disconnect (also
    #: what a second detach answers — the call is idempotent).
    status: str
    message: str
    session_id: uuid.UUID
    channel: str = ""
    thread_ts: str = ""
    #: Whether the "no longer connected" note reached the thread (best-effort).
    notice_posted: bool = False


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


class SlackDeclareAgentOut(StrictModel):
    status: str
    detail: str = ""
    changed: list[str] = []
    reinstall_required: bool = False
    install_url: str = ""

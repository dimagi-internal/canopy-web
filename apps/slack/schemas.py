from __future__ import annotations

from apps.common.schemas import StrictModel


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


class SlackConfigOut(StrictModel):
    workspace: str
    connected: bool
    team_name: str
    installed_by_email: str
    installed_at: str
    install_url: str
    commands: SlackCommandsOut
    agent: SlackAgentOut


class SlackConfigTokenIn(StrictModel):
    refresh_token: str


class SlackSyncOut(StrictModel):
    status: str
    detail: str = ""
    added: list[str] = []
    removed: list[str] = []
    unfit: list[str] = []


class SlackDeclareAgentOut(StrictModel):
    status: str
    detail: str = ""
    changed: list[str] = []
    reinstall_required: bool = False
    install_url: str = ""

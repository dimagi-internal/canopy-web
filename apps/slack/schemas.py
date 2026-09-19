from __future__ import annotations

from apps.common.schemas import StrictModel


class SlackCommandsOut(StrictModel):
    # canopy can edit the Slack app's slash commands (app id + config token).
    managed: bool
    app_id: str
    set_by_email: str
    synced_at: str
    error: str


class SlackConfigOut(StrictModel):
    workspace: str
    connected: bool
    team_name: str
    installed_by_email: str
    installed_at: str
    install_url: str
    commands: SlackCommandsOut


class SlackConfigTokenIn(StrictModel):
    refresh_token: str


class SlackSyncOut(StrictModel):
    status: str
    detail: str = ""
    added: list[str] = []
    removed: list[str] = []
    unfit: list[str] = []

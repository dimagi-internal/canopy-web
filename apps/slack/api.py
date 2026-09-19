"""Django Ninja router for /api/slack-config — a workspace's Slack connection.

Deliberately NOT under /api/slack/: that prefix is allowlisted in the login
middleware for Slack's own signed webhooks, and nothing an owner configures
should share a door with an anonymous caller. Reads are member, writes owner,
the same split `apps/inbound` uses for its config.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

from . import commands, services
from .models import SlackInstallation
from .schemas import SlackConfigOut, SlackConfigTokenIn, SlackSyncOut

router = Router(auth=session_auth, tags=["slack"])


def _workspace_or_404(user, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.user_workspace_slugs(user):
        raise HttpError(404, "no such workspace")
    return ws


def _owner_workspace_or_404(user, slug: str) -> Workspace:
    ws = _workspace_or_404(user, slug)
    if wsvc.member_role(user, ws) != WorkspaceMembership.OWNER:
        raise HttpError(403, "requires the owner role")
    return ws


def _installation_or_409(ws: Workspace) -> SlackInstallation:
    inst = SlackInstallation.objects.filter(workspace=ws).first()
    if inst is None:
        raise HttpError(409, "Slack is not connected to this workspace")
    return inst


def _out(ws: Workspace) -> dict:
    inst = SlackInstallation.objects.filter(workspace=ws).select_related("installed_by", "config_set_by").first()
    iso = lambda d: d.isoformat() if d else ""  # noqa: E731
    return {
        "workspace": ws.slug,
        "connected": inst is not None,
        "team_name": inst.team_name if inst else "",
        "installed_by_email": getattr(getattr(inst, "installed_by", None), "email", "") or "",
        "installed_at": iso(inst.installed_at) if inst else "",
        "install_url": services.public_url(f"/auth/slack/install/?workspace={ws.slug}"),
        "commands": {
            "managed": bool(inst and inst.manages_commands),
            "app_id": inst.app_id if inst else "",
            "set_by_email": getattr(getattr(inst, "config_set_by", None), "email", "") or "",
            "synced_at": iso(inst.commands_synced_at) if inst else "",
            "error": inst.commands_sync_error if inst else "",
        },
    }


@router.get("/{workspace}", response=SlackConfigOut, summary="This workspace's Slack connection")
def get_config(request: HttpRequest, workspace: str) -> dict:
    return _out(_workspace_or_404(request.user, workspace))


@router.put("/{workspace}/config-token", response=SlackSyncOut,
            summary="Let canopy manage the Slack app's slash commands (owner)")
def set_config_token(request: HttpRequest, workspace: str, payload: SlackConfigTokenIn) -> dict:
    """Takes the REFRESH token of a Slack app configuration token, then syncs
    the app's slash commands to the agents that are on for Slack."""
    ws = _owner_workspace_or_404(request.user, workspace)
    inst = _installation_or_409(ws)
    try:
        commands.set_config_token(inst, payload.refresh_token, user=request.user)
    except Exception as e:  # noqa: BLE001
        raise HttpError(422, f"Slack refused that token: {e}") from e
    return commands.sync_quietly(inst)


@router.delete("/{workspace}/config-token", response=SlackConfigOut,
               summary="Stop managing the Slack app's slash commands (owner)")
def clear_config_token(request: HttpRequest, workspace: str) -> dict:
    ws = _owner_workspace_or_404(request.user, workspace)
    commands.clear_config_token(_installation_or_409(ws))
    return _out(ws)


@router.post("/{workspace}/sync", response=SlackSyncOut, summary="Sync slash commands now (owner)")
def sync(request: HttpRequest, workspace: str) -> dict:
    ws = _owner_workspace_or_404(request.user, workspace)
    return commands.sync_quietly(_installation_or_409(ws))

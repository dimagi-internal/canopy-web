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

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import commands, ingest, services
from .models import SlackChannelIngestAllow, SlackInstallation, SlackWorkspaceLink
from .schemas import (
    SlackChannelAllowIn,
    SlackChannelIngestSwitchIn,
    SlackConfigOut,
    SlackConfigTokenIn,
    SlackDeclareAgentOut,
    SlackHistoryIn,
    SlackSyncOut,
)

#: What each permission canopy asks for turns on, for the reinstall notice.
#: A scope missing from here is named as itself.
_SCOPE_FEATURES = {
    "assistant:write": "Slack's own \"Working…\" indicator and Stop button",
    "channels:read": "finding channels by name (#general)", "groups:read": "finding channels by name (#general)",
    "channels:history": "reading channel messages", "groups:history": "reading channel messages",
    "files:read": "files and screenshots people send",
    "chat:write.customize": "replies under each agent's own name and picture",
}


def _missing_scopes(inst: SlackInstallation) -> list[str]:
    """Scopes canopy asks for that this install never granted. An install
    from before canopy kept them is asked once (`X-OAuth-Scopes`); if Slack
    does not say, nothing is reported missing rather than guessed."""
    from . import client
    from .views_auth import BOT_SCOPES

    if not inst.granted_scopes:
        try:
            got = client.granted_scopes(inst.bot_token)
        except Exception:  # noqa: BLE001 — a settings page must not fail on Slack
            got = None
        if not got:
            return []
        inst.granted_scopes = ",".join(got)
        SlackInstallation.objects.filter(pk=inst.pk).update(granted_scopes=inst.granted_scopes)
    have = {s.strip() for s in inst.granted_scopes.split(",")}
    return [s for s in BOT_SCOPES if s not in have]


def _reinstall_out(inst: SlackInstallation | None) -> dict:
    missing = _missing_scopes(inst) if inst is not None else []
    turns_on = list(dict.fromkeys(_SCOPE_FEATURES.get(s, s) for s in missing))
    return {"needed": bool(missing), "turns_on": turns_on}


router = Router(auth=session_auth, tags=["slack"])


def _workspace_or_404(user, slug: str) -> Workspace:
    ws = Workspace.objects.filter(slug=slug).first()
    if ws is None or slug not in wsvc.user_workspace_slugs(user):
        raise HttpError(404, "no such workspace")
    return ws


def _workspace_with(user, slug: str, capability: str) -> Workspace:
    """The workspace, if the caller holds `capability` there (`permissions`).
    The Slack APP is the owner's (config token, declaring it an agent — one
    Slack can serve several workspaces, and declaring cannot be undone);
    reading history in and syncing are integrations, run by an admin."""
    ws = _workspace_or_404(user, slug)
    if not perms.can(user, ws, capability):
        raise HttpError(403, f"requires the {perms.MINIMUM_ROLE[capability]} role or above")
    return ws


def _installation_or_409(ws: Workspace) -> SlackInstallation:
    inst = services.installation_for_workspace(ws.slug)
    if inst is None:
        raise HttpError(409, "Slack is not connected to this workspace")
    return inst


def _out(ws: Workspace) -> dict:
    link = (SlackWorkspaceLink.objects.filter(workspace=ws)
            .select_related("installation", "installation__config_set_by", "linked_by").first())
    inst = link.installation if link is not None else None
    iso = lambda d: d.isoformat() if d else ""  # noqa: E731
    return {
        "workspace": ws.slug,
        "connected": inst is not None,
        "team_name": inst.team_name if inst else "",
        # Who connected THIS workspace, and when — the Slack itself may have
        # been installed earlier, for another tenant.
        "installed_by_email": getattr(getattr(link, "linked_by", None), "email", "") or "",
        "installed_at": iso(link.linked_at) if link else "",
        "install_url": services.public_url(f"/auth/slack/install/?workspace={ws.slug}"),
        "commands": {
            "managed": bool(inst and inst.manages_commands),
            "app_id": inst.app_id if inst else "",
            "set_by_email": getattr(getattr(inst, "config_set_by", None), "email", "") or "",
            "synced_at": iso(inst.commands_synced_at) if inst else "",
            "error": inst.commands_sync_error if inst else "",
        },
        "reinstall": _reinstall_out(inst),
        "history": {
            "enabled": bool(link and link.history_enabled),
            "max_minutes": link.history_max_minutes if link else
            SlackWorkspaceLink._meta.get_field("history_max_minutes").default,
        },
        "channel_ingest": {"enabled": bool(link and link.channel_ingest_enabled),
                           "allowed": [_allow_out(a) for a in SlackChannelIngestAllow.objects
                                       .select_related("agent", "added_by").filter(workspace=ws)
                                       .order_by("channel_name", "agent__slug")]},
    }


def _allow_out(a: SlackChannelIngestAllow) -> dict:
    return {"id": str(a.pk), "agent": a.agent.slug, "channel": a.channel_id, "channel_name": a.channel_name,
            "follow": a.follow, "note": a.note, "added_by_email": getattr(a.added_by, "email", "") or "",
            "added_at": a.added_at.isoformat() if a.added_at else ""}


@router.get("/{workspace}", response=SlackConfigOut, summary="This workspace's Slack connection")
@gate("member")
def get_config(request: HttpRequest, workspace: str) -> dict:
    return _out(_workspace_or_404(request.user, workspace))


@router.put("/{workspace}/config-token", response=SlackSyncOut,
            summary="Let canopy manage the Slack app's slash commands (owner)")
@gate("own", setting=True)
def set_config_token(request: HttpRequest, workspace: str, payload: SlackConfigTokenIn) -> dict:
    """Takes the REFRESH token of a Slack app configuration token, then syncs
    the app's slash commands to the agents that are on for Slack."""
    ws = _workspace_with(request.user, workspace, perms.OWN)
    inst = _installation_or_409(ws)
    try:
        commands.set_config_token(inst, payload.refresh_token, user=request.user)
    except Exception as e:  # noqa: BLE001
        raise HttpError(422, f"Slack refused that token: {e}") from e
    return commands.sync_quietly(inst)


@router.delete("/{workspace}/config-token", response=SlackConfigOut,
               summary="Stop managing the Slack app's slash commands (owner)")
@gate("own", setting=True)
def clear_config_token(request: HttpRequest, workspace: str) -> dict:
    ws = _workspace_with(request.user, workspace, perms.OWN)
    commands.clear_config_token(_installation_or_409(ws))
    return _out(ws)


@router.put("/{workspace}/history", response=SlackConfigOut,
            summary="Allow reading channel history, and how far back (admin or owner)")
@gate("integrations", setting=True)
def set_history(request: HttpRequest, workspace: str, payload: SlackHistoryIn) -> dict:
    """The policy for `@canopy <agent> --history <minutes> <ask>` in this workspace:
    whether it may read the channel's recent past at all, and the longest window."""
    ws = _workspace_with(request.user, workspace, perms.INTEGRATIONS)
    _installation_or_409(ws)
    SlackWorkspaceLink.objects.filter(workspace=ws).update(
        history_enabled=payload.enabled, history_max_minutes=payload.max_minutes)
    return _out(ws)


@router.put("/{workspace}/channel-ingest", response=SlackConfigOut,
            summary="Allow this workspace's agents to ask to read a whole channel (owner)")
@gate("own", setting=True)  # revoking withdraws an agent's access to a channel
def set_channel_ingest(request: HttpRequest, workspace: str, payload: SlackChannelIngestSwitchIn) -> dict:
    """The opt-in for permissioned whole-channel reads (`/api/slack-ingests/`).

    On, an agent may ASK; every read still waits for an approval posted in the
    channel itself. Off, new requests are refused and live reads stop."""
    ws = _workspace_with(request.user, workspace, perms.OWN)
    _installation_or_409(ws)
    SlackWorkspaceLink.objects.filter(workspace=ws).update(channel_ingest_enabled=payload.enabled)
    return _out(ws)


@router.post("/{workspace}/channel-ingest/allowed", response=SlackConfigOut,
             summary="Allow-list one channel for one agent to read whole (admin or owner)")
@gate("integrations", setting=True)  # an admin's standing grant of one channel to one agent
def allow_channel_ingest(request: HttpRequest, workspace: str, payload: SlackChannelAllowIn) -> dict:
    """Explicit, standing permission: when this agent asks to read this channel
    (`POST /api/slack-ingests/`) canopy reads it at once — approved by you — and
    posts a notice with Revoke in the channel instead of Approve/Decline.
    Works whether or not asking is on for the workspace. Adding it again
    updates `follow` and `note`."""
    ws = _workspace_with(request.user, workspace, perms.INTEGRATIONS)
    _installation_or_409(ws)
    agent = Agent.objects.filter(slug=payload.agent, workspace=ws).first()
    if agent is None:
        raise HttpError(404, f"agent '{payload.agent}' is not in '{ws.slug}'")
    try:
        ingest.allow(workspace=ws, agent=agent, channel=payload.channel, follow=payload.follow,
                     note=payload.note, by=request.user)
    except ingest.IngestError as e:
        raise HttpError(e.status, str(e)) from e
    return _out(ws)


@router.delete("/{workspace}/channel-ingest/allowed/{allow_id}", response=SlackConfigOut,
               summary="Remove a channel from an agent's allow-list; revokes its reads (admin or owner)")
# also revokes the reads the entry allowed
# an admin's standing grant of one channel to one agent
@gate("integrations", setting=True)
def disallow_channel_ingest(request: HttpRequest, workspace: str, allow_id: str) -> dict:
    ws = _workspace_with(request.user, workspace, perms.INTEGRATIONS)
    try:
        entry = (SlackChannelIngestAllow.objects.select_related("agent", "workspace", "installation")
                 .filter(pk=allow_id, workspace=ws).first())
    except Exception:  # noqa: BLE001 — not a UUID
        entry = None
    if entry is None:
        raise HttpError(404, "no such allow-list entry")
    ingest.disallow(entry, by=request.user)
    return _out(ws)


@router.post("/{workspace}/sync", response=SlackSyncOut, summary="Sync slash commands now (admin or owner)")
@gate("integrations", kind="action")
def sync(request: HttpRequest, workspace: str) -> dict:
    ws = _workspace_with(request.user, workspace, perms.INTEGRATIONS)
    return commands.sync_quietly(_installation_or_409(ws))


@router.post("/{workspace}/declare-agent", response=SlackDeclareAgentOut,
             summary="Declare the Slack app an agent (owner)")
@gate("own", setting=True)
def declare_agent(request: HttpRequest, workspace: str) -> dict:
    """Turn on Slack's own working indicator for this workspace's Slack app.

    Three manifest edits through the configuration token canopy already holds —
    `features.agent_view`, the `assistant:write` scope, the agent events — after
    which Slack draws a "Working…" indicator and a Stop button in the thread
    instead of only the status line canopy posts.

    Owner-only, and a deliberate act rather than a deploy step: it changes how
    the app presents itself to everyone in the Slack workspace, and Slack does
    not allow `agent_view` to be swapped back to the older `assistant_view`.
    The new scope lands only on a re-install, so the response says so and
    carries the URL.
    """
    from django.utils import timezone

    ws = _workspace_with(request.user, workspace, perms.OWN)
    inst = _installation_or_409(ws)
    try:
        result = commands.declare_agent(inst)
    except commands.NotConfigured:
        raise HttpError(409, "canopy can't edit this Slack app yet — connect a configuration token first")
    except ValueError as e:
        raise HttpError(422, str(e)) from e
    except Exception as e:  # noqa: BLE001
        raise HttpError(502, f"Slack refused that change: {e}") from e
    SlackInstallation.objects.filter(pk=inst.pk).update(agent_declared_at=timezone.now())
    changed = result["changed"]
    return {
        "status": "declared" if changed else "already_declared",
        "detail": ("Slack will draw its own working indicator once the app is re-installed."
                   if result["reinstall_required"] else
                   ("Declared." if changed else "Already declared an agent — nothing to change.")),
        "changed": changed,
        "reinstall_required": result["reinstall_required"],
        "install_url": services.public_url(f"/auth/slack/install/?workspace={ws.slug}"),
    }

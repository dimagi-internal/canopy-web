"""An agent's read mode and connected sites over HTTP (`apps/agents/answering.py`).

Any member may read them, as they may read the interface; only the agent's owner or an admin changes
them. MCP is the REST API, so each route here is a tool.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.route_gates import gate
from apps.events import audit
from apps.tokens.models import AppCredential

from . import answering
from .gates import agent_for_admin, agent_for_read
from .schemas import ConnectedSiteIn, ConnectedSitesOut, ReadModeIn, ReadModeSettingOut

router = Router(tags=["agents"])


def _changed(fields) -> dict:
    return fields.model_dump(exclude_unset=True, exclude={"dry_run"})


def _run(edit, *args, **kwargs):
    try:
        return edit(*args, **kwargs)
    except answering.SettingsError as exc:
        raise HttpError(422, str(exc)) from exc


def _sites_out(agent) -> dict:
    live = (AppCredential.objects.filter(workspace_id=agent.workspace_id, revoked_at__isnull=True)
            .order_by("name").values_list("name", flat=True))
    return {"sites": answering.connected_sites_of(agent), "available": list(live),
            "new_site_defaults": answering.new_site_defaults()}


@router.get("/{slug}/read-mode", response=ReadModeSettingOut,
            summary="What someone who may only read gets from this agent")
@gate("member")
def get_read_mode(request: HttpRequest, slug: str):
    return answering.read_mode_of(agent_for_read(request, slug))


@router.put("/{slug}/read-mode", response=ReadModeSettingOut,
            summary="Customise read mode: only the fields sent change. dry_run checks without saving")
@gate("agent-admin", setting=True)
def put_read_mode(request: HttpRequest, slug: str, payload: ReadModeIn):
    agent = agent_for_admin(request, slug)
    locked, before, after = _run(answering.set_read_mode, agent, _changed(payload), by=request.user,
                                 dry_run=payload.dry_run)
    audit.note_change(scope=f"agent:{agent.slug}/read-mode", before=before,
                      after=before if payload.dry_run else after,
                      summary=f"read mode customised for {agent.slug}")
    if payload.dry_run:
        # `locked` still holds the saved document: show what the draft would be.
        locked.interface = {"capabilities": after}
    return answering.read_mode_of(locked)


@router.delete("/{slug}/read-mode", response=ReadModeSettingOut,
               summary="Put read mode back on canopy's default")
@gate("agent-admin", setting=True)
def reset_read_mode(request: HttpRequest, slug: str):
    agent = agent_for_admin(request, slug)
    locked, before, after = _run(answering.reset_read_mode, agent, by=request.user)
    audit.note_change(scope=f"agent:{agent.slug}/read-mode", before=before, after=after,
                      summary=f"read mode back on the default for {agent.slug}")
    return answering.read_mode_of(locked)


@router.get("/{slug}/connected-sites", response=ConnectedSitesOut,
            summary="The sites this agent can be reached from, and the workspace's sites it could add")
@gate("member")
def list_connected_sites(request: HttpRequest, slug: str):
    return _sites_out(agent_for_read(request, slug))


@router.put("/{slug}/connected-sites/{site}", response=ConnectedSitesOut,
            summary="Add or change one connected site: only the fields sent change. dry_run checks without saving")
@gate("agent-admin", setting=True)
def put_connected_site(request: HttpRequest, slug: str, site: str, payload: ConnectedSiteIn):
    agent = agent_for_admin(request, slug)
    locked, before, after = _run(answering.set_site, agent, site, _changed(payload), by=request.user,
                                 dry_run=payload.dry_run)
    # A note that changed nothing is what keeps a dry run out of the audit.
    audit.note_change(scope=f"agent:{agent.slug}/site/{site}", before=before,
                      after=before if payload.dry_run else after,
                      summary=f"connected site {site} changed on {agent.slug}")
    if payload.dry_run:
        locked.interface = {"capabilities": after}
    return _sites_out(locked)


@router.delete("/{slug}/connected-sites/{site}", response=ConnectedSitesOut,
               summary="Remove one connected site from this agent")
@gate("agent-admin", setting=True)
def delete_connected_site(request: HttpRequest, slug: str, site: str):
    agent = agent_for_admin(request, slug)
    try:
        locked, before, after = _run(answering.remove_site, agent, site, by=request.user)
    except LookupError as exc:
        raise HttpError(404, f"{agent.slug} is not connected to {site}") from exc
    audit.note_change(scope=f"agent:{agent.slug}/site/{site}", before=before, after=after,
                      summary=f"connected site {site} removed from {agent.slug}")
    return _sites_out(locked)

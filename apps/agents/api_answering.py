"""An agent's read mode, host sites and app entries over HTTP (`apps/agents/profile_settings.py`).

Any member may read them, as they may read the interface; only the agent's owner or an admin changes
them. MCP is the REST API, so each route here is a tool.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.errors import refusals
from apps.api.route_gates import gate
from apps.events import audit
from apps.tokens.models import AppCredential

from . import app_entries, profile_settings
from .gates import agent_for_admin, agent_for_read
from .schemas import (
    AgentAppEntriesOut,
    AgentAppEntryIn,
    AgentHostSiteIn,
    AgentHostSitesOut,
    ReadModeIn,
    ReadModeSettingOut,
)

router = Router(tags=["agents"])


def _changed(fields) -> dict:
    return fields.model_dump(exclude_unset=True, exclude={"dry_run"})


def _run(edit, *args, **kwargs):
    try:
        return edit(*args, **kwargs)
    except profile_settings.SettingsError as exc:
        raise HttpError(422, str(exc)) from exc


def _sites_out(agent) -> dict:
    live = (AppCredential.objects.filter(workspace_id=agent.workspace_id, revoked_at__isnull=True)
            .order_by("name").values_list("name", flat=True))
    return {"sites": profile_settings.host_sites_of(agent), "available": list(live),
            "new_site_defaults": profile_settings.new_site_defaults()}


@router.get("/{slug}/read-mode", response=ReadModeSettingOut,
            summary="What someone who may only read gets from this agent")
@gate("member")
def get_read_mode(request: HttpRequest, slug: str):
    return profile_settings.read_mode_of(agent_for_read(request, slug))


@router.put("/{slug}/read-mode", response={200: ReadModeSettingOut, **refusals(422)},
            summary="Customise read mode: only the fields sent change. dry_run checks without saving")
@gate("agent-admin", setting=True)
def put_read_mode(request: HttpRequest, slug: str, payload: ReadModeIn):
    """Malformed read paths, or a pinned skill commit that is not a commit sha, are a 422."""
    agent = agent_for_admin(request, slug)
    locked, before, after = _run(profile_settings.set_read_mode, agent, _changed(payload), by=request.user,
                                 dry_run=payload.dry_run)
    audit.note_change(scope=f"agent:{agent.slug}/read-mode", before=before,
                      after=before if payload.dry_run else after,
                      summary=f"read mode customised for {agent.slug}")
    if payload.dry_run:
        # `locked` still holds the saved document: show what the draft would be.
        locked.interface = {"capabilities": after}
    return profile_settings.read_mode_of(locked)


@router.delete("/{slug}/read-mode", response=ReadModeSettingOut,
               summary="Put read mode back on canopy's default")
@gate("agent-admin", setting=True)
def reset_read_mode(request: HttpRequest, slug: str):
    agent = agent_for_admin(request, slug)
    locked, before, after = _run(profile_settings.reset_read_mode, agent, by=request.user)
    audit.note_change(scope=f"agent:{agent.slug}/read-mode", before=before, after=after,
                      summary=f"read mode back on the default for {agent.slug}")
    return profile_settings.read_mode_of(locked)


@router.get("/{slug}/host-sites", response=AgentHostSitesOut,
            summary="The sites this agent can be reached from, and the workspace's sites it could add")
@gate("member")
def list_agent_host_sites(request: HttpRequest, slug: str):
    return _sites_out(agent_for_read(request, slug))


@router.put("/{slug}/host-sites/{site}", response={200: AgentHostSitesOut, **refusals(422)},
            summary="Add or change one host site: only the fields sent change. dry_run checks without saving")
@gate("agent-admin", setting=True)
def put_agent_host_site(request: HttpRequest, slug: str, site: str, payload: AgentHostSiteIn):
    """A field of the wrong shape (an empty or over-long `bash` entry, a malformed path) is a 422."""
    agent = agent_for_admin(request, slug)
    locked, before, after = _run(profile_settings.set_site, agent, site, _changed(payload), by=request.user,
                                 dry_run=payload.dry_run)
    # A note that changed nothing is what keeps a dry run out of the audit.
    audit.note_change(scope=f"agent:{agent.slug}/site/{site}", before=before,
                      after=before if payload.dry_run else after,
                      summary=f"host site {site} changed on {agent.slug}")
    if payload.dry_run:
        locked.interface = {"capabilities": after}
    return _sites_out(locked)


@router.delete("/{slug}/host-sites/{site}", response=AgentHostSitesOut,
               summary="Remove one host site from this agent")
@gate("agent-admin", setting=True)
def delete_agent_host_site(request: HttpRequest, slug: str, site: str):
    agent = agent_for_admin(request, slug)
    try:
        locked, before, after = _run(profile_settings.remove_site, agent, site, by=request.user)
    except LookupError as exc:
        raise HttpError(404, f"{agent.slug} is not connected to {site}") from exc
    audit.note_change(scope=f"agent:{agent.slug}/site/{site}", before=before, after=after,
                      summary=f"host site {site} removed from {agent.slug}")
    return _sites_out(locked)


def _apps_out(agent) -> dict:
    host = app_entries.host()
    available = host.available(agent.workspace_id) if host is not None else []
    return {"entries": profile_settings.app_entries_of(agent), "available": available,
            "narrowable": list(app_entries.NARROWABLE)}


@router.get("/{slug}/app-entries", response=AgentAppEntriesOut,
            summary="The workspace apps this agent is bound to, and the apps it could be bound to")
@gate("member")
def list_agent_app_entries(request: HttpRequest, slug: str):
    return _apps_out(agent_for_read(request, slug))


@router.put("/{slug}/app-entries/{app}", response={200: AgentAppEntriesOut, **refusals(422)},
            summary="Bind this agent to one workspace app, or change that entry. dry_run checks without saving")
@gate("agent-admin", setting=True)
def put_agent_app_entry(request: HttpRequest, slug: str, app: str, payload: AgentAppEntryIn):
    """An app entry's tools are canopy's own (the app's query, actions and tasks, as the person asking); only
    its description, its entry command and a `ceiling` that narrows those tools can be set. An app not
    installed in the agent's workspace, or a ceiling naming anything else, is a 422. Who may use it is the
    access list (`use:<key>`)."""
    agent = agent_for_admin(request, slug)
    locked, before, after = _run(profile_settings.set_app_entry, agent, app, _changed(payload), by=request.user,
                                 dry_run=payload.dry_run)
    audit.note_change(scope=f"agent:{agent.slug}/app/{app}", before=before,
                      after=before if payload.dry_run else after,
                      summary=f"app entry {app} changed on {agent.slug}")
    if payload.dry_run:
        locked.interface = {"capabilities": after}
    return _apps_out(locked)


@router.delete("/{slug}/app-entries/{app}", response=AgentAppEntriesOut,
               summary="Unbind this agent from one workspace app")
@gate("agent-admin", setting=True)
def delete_agent_app_entry(request: HttpRequest, slug: str, app: str):
    agent = agent_for_admin(request, slug)
    try:
        locked, before, after = _run(profile_settings.remove_app_entry, agent, app, by=request.user)
    except LookupError as exc:
        raise HttpError(404, f"{agent.slug} is not bound to the app {app}") from exc
    audit.note_change(scope=f"agent:{agent.slug}/app/{app}", before=before, after=after,
                      summary=f"app entry {app} removed from {agent.slug}")
    return _apps_out(locked)

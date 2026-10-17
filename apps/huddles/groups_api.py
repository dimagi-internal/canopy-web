"""/api/huddle-groups — work groups and their runs (apps/huddles/groups.py).

Members read groups and runs; a workspace owner or admin writes groups. Nothing
here reads a Slack channel or can start a read: a run is read only when its lead
presses Confirm on the announcement in Slack. `finished` is the leader agent's
own login reporting the run done.
"""
from __future__ import annotations

from canopy_cron import next_slots
from django.db.models import Q
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import groups
from .models import HuddleGroup, HuddleGroupRun
from .schemas import (
    WorkGroupIn,
    WorkGroupOut,
    WorkGroupPatch,
    WorkGroupRunFinishIn,
    WorkGroupRunOut,
)

router = Router(tags=["huddles"])


def group_out(g: HuddleGroup, user) -> dict:
    s = g.schedule
    try:
        nxt = next_slots(s.cron, s.timezone, now=timezone.now(), count=3) if s is not None and g.enabled else []
    except Exception:  # noqa: BLE001
        nxt = []
    p = g.project
    last = g.runs.order_by("-created_at").values_list("status", flat=True).first()
    return {
        "slug": g.slug, "workspace": g.workspace_id, "name": g.name, "topic": g.topic,
        "lead_email": getattr(g.lead, "email", "") or "", "lead_slack_id": g.lead_slack_id,
        "channel_id": g.channel_id, "channel_name": g.channel_name, "agents": list(g.agents or []),
        "leader_agent": g.leader_agent.slug, "mode": "single" if g.single else "multi",
        "cron": s.cron if s else "", "timezone": s.timezone if s else "UTC", "next_runs": nxt,
        "schedule_id": s.pk if s else None,
        "project": ({"agent": p.agent.slug, "ref": p.ext_id, "name": p.name, "status": p.status,
                     "url": f"/w/{p.agent.workspace_id}/agents/{p.agent.slug}/projects/{p.ext_id}"}
                    if p is not None else None),
        "enabled": g.enabled, "last_run_status": last or "",
        "url": f"/w/{g.workspace_id}/huddles/groups/{g.slug}",
        "can_edit": perms.can(user, g.workspace_id, perms.INTEGRATIONS),
        "created_at": g.created_at, "updated_at": g.updated_at,
    }


def run_out(r: HuddleGroupRun, *, material: bool = False) -> dict:
    g = r.group
    return {
        "id": r.pk, "group": g.slug, "group_name": g.name, "workspace": g.workspace_id, "slot": r.slot,
        "manual": r.manual, "status": r.status, "note": r.note, "announce_ts": r.announce_ts,
        "slack_url": (f"https://slack.com/archives/{g.channel_id}/p{r.announce_ts.replace('.', '')}"
                      if r.announce_ts else ""),
        "confirmed_by_email": getattr(r.confirmed_by, "email", "") or "", "confirmed_at": r.confirmed_at,
        "read_messages": r.read_messages, "read_first_ts": r.read_first_ts, "read_last_ts": r.read_last_ts,
        "read_since": r.read_since, "material": r.material if material else "",
        "turn_id": str(r.turn_id or ""), "huddle_id": r.huddle_id, "result_url": r.result_url,
        "summary": r.summary, "finished_at": r.finished_at, "created_at": r.created_at,
    }


def _qs():
    return HuddleGroup.objects.select_related("workspace", "lead", "leader_agent", "schedule", "project",
                                              "project__agent")


def _group_or_404(request: HttpRequest, slug: str) -> HuddleGroup:
    rows = list(_qs().filter(slug=slug, workspace_id__in=wsvc.request_workspace_slugs(request)))
    pin = getattr(request, "workspace_slug", None)
    if len(rows) > 1 and pin:
        rows = [g for g in rows if g.workspace_id == pin] or rows
    if not rows:
        raise HttpError(404, f"work group '{slug}' not found")
    if len(rows) > 1:
        raise HttpError(422, f"more than one workspace has a work group '{slug}' — use /api/w/<workspace>/…")
    return rows[0]


def _require_admin(request: HttpRequest, workspace_id: str) -> None:
    if not perms.can(request.user, workspace_id, perms.INTEGRATIONS):
        raise HttpError(403, f"only an owner or admin of {workspace_id} can change its work groups")


def _save(request: HttpRequest, workspace, data: dict, group: HuddleGroup | None = None) -> HuddleGroup:
    try:
        return groups.save(workspace=workspace, by=request.user, data=data, group=group)
    except groups.GroupError as e:
        raise HttpError(e.status, str(e)) from e


@router.get("/", response=list[WorkGroupOut], summary="List work groups")
def list_huddle_groups(request: HttpRequest) -> list[dict]:
    """Work groups in your workspaces: a lead, a Slack channel, the agents that
    work it, and its cadence."""
    rows = _qs().filter(workspace_id__in=wsvc.request_workspace_slugs(request)).order_by("name")
    return [group_out(g, request.user) for g in rows]


@router.post("/", response={201: WorkGroupOut}, summary="Create a work group")
def create_huddle_group(request: HttpRequest, payload: WorkGroupIn):
    """Owner or admin. Checks the lead is in the workspace AND the channel, and that
    the leader agent may run every other agent; then puts the channel on the leader's
    read allow-list, creates its schedule (`/canopy:huddle`, the group's cadence) and,
    with one agent, the group's project. Reads nothing: each run is read only when
    the lead presses Confirm on its announcement in Slack."""
    ws = wsvc.creation_workspace(request)
    if ws is None:
        raise HttpError(422, wsvc.creation_refusal(request))
    _require_admin(request, ws.slug)
    g = _save(request, ws, payload.model_dump())
    return Status(201, group_out(_qs().get(pk=g.pk), request.user))


@router.get("/runs/{run_id}", response=WorkGroupRunOut, summary="Get one work group run, with its material")
def get_huddle_group_run(request: HttpRequest, run_id: int) -> dict:
    """One run: status, the read window and the channel material it read (members
    of the group's workspace, and the leader agent's own login)."""
    return run_out(_run_or_404(request, run_id), material=True)


@router.post("/runs/{run_id}/finished", response=WorkGroupRunOut, summary="Report a work group run done")
def finish_huddle_group_run(request: HttpRequest, run_id: int, payload: WorkGroupRunFinishIn) -> dict:
    """The leader agent's own login only. Marks the run done and posts one line in
    the announcement's thread pointing at the result — the content stays in canopy."""
    r = _run_or_404(request, run_id)
    if r.group.leader_agent.user_id is None or r.group.leader_agent.user_id != request.user.pk:
        raise HttpError(403, f"only {r.group.leader_agent.slug}'s own login can finish this run")
    try:
        r = groups.finish(r, huddle_id=payload.huddle_id, result_url=payload.result_url, summary=payload.summary)
    except groups.GroupError as e:
        raise HttpError(e.status, str(e)) from e
    return run_out(r, material=True)


def _run_or_404(request: HttpRequest, run_id: int) -> HuddleGroupRun:
    r = (HuddleGroupRun.objects.select_related("group", "group__workspace", "group__leader_agent", "confirmed_by")
         .filter(pk=run_id).filter(Q(group__workspace_id__in=wsvc.request_workspace_slugs(request))
                                   | Q(group__leader_agent__user_id=request.user.pk)).first())
    if r is None:
        raise HttpError(404, f"work group run {run_id} not found")
    return r


@router.get("/{slug}", response=WorkGroupOut, summary="Get one work group")
def get_huddle_group(request: HttpRequest, slug: str) -> dict:
    return group_out(_group_or_404(request, slug), request.user)


@router.patch("/{slug}", response=WorkGroupOut, summary="Change a work group")
def update_huddle_group(request: HttpRequest, slug: str, payload: WorkGroupPatch) -> dict:
    """Owner or admin. Sends only what changes; every check runs again (lead in the
    workspace and the channel, leader may run the members)."""
    g = _group_or_404(request, slug)
    _require_admin(request, g.workspace_id)
    g = _save(request, g.workspace, payload.model_dump(exclude_none=True), group=g)
    return group_out(_qs().get(pk=g.pk), request.user)


@router.delete("/{slug}", response={204: None}, summary="Delete a work group")
def delete_huddle_group(request: HttpRequest, slug: str):
    """Owner or admin. Takes the channel off the leader's read allow-list (unless
    another group still uses it), deletes the group's schedule, and lapses any
    announcement still waiting for a confirm."""
    g = _group_or_404(request, slug)
    _require_admin(request, g.workspace_id)
    groups.delete(g, by=request.user)
    return Status(204, None)


@router.get("/{slug}/runs", response=list[WorkGroupRunOut], summary="List a work group's runs")
def list_huddle_group_runs(request: HttpRequest, slug: str, limit: int = 50) -> list[dict]:
    """Newest first: status, the read window, the Slack post, the result (no material)."""
    g = _group_or_404(request, slug)
    rows = g.runs.select_related("group", "confirmed_by").order_by("-created_at")[:max(1, min(limit, 200))]
    return [run_out(r) for r in rows]

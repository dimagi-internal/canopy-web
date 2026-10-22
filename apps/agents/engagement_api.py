"""/api/engagement — what people do with projects and huddles (`EngagementEvent`).

* `POST /view` — the view beacon the project page and the huddle page fire on
  load: one `viewed` event per person, per object, per UTC day.
* `GET /summary` — what people actually did: the report Ada reads weekly.

Every other event is recorded where its act happens (`engagement.record`); this
router is only the acts that have no other route — opening a page.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.harness.api import visible_turns_qs
from apps.huddles import services as huddles
from apps.realtime.groups import turn_workspace_slug
from apps.workspaces import services as wsvc

from . import engagement
from .gates import listed_agent_workspaces, visible_agent_workspaces
from .models import AgentProject
from .schemas import EngagementSummaryOut, EngagementViewIn, EngagementViewOut

router = Router(auth=session_auth, tags=["engagement"])


def _huddle_workspace(request: HttpRequest, huddle_id: str) -> str:
    """The workspace of a huddle the caller can see — the same lookup the huddle
    page makes (`apps/huddles/api.get_huddle`), so a guess confirms nothing."""

    anchor = (huddles.anchors(visible_turns_qs(request, slugs=wsvc.member_scope(request.user)))
              .filter(origin_ref__huddle=huddle_id).order_by("-created_at").first())
    home = turn_workspace_slug(anchor) if anchor is not None else ""
    if not home:
        raise HttpError(404, f"huddle {huddle_id!r} not found")
    return home


@router.post("/view", response=EngagementViewOut, summary="Record that you opened a project or a huddle")
def record_engagement_view(request: HttpRequest, payload: EngagementViewIn) -> dict:
    """Name exactly one of `project` (its numeric id) or `huddle_id`. Counted once
    per person, per object, per UTC day — `recorded` is false for a repeat. 404
    for anything the caller cannot see."""
    if (payload.project is None) == (not payload.huddle_id):
        raise HttpError(422, "name exactly one of project or huddle_id")
    surface = engagement.surface_of(request)
    if payload.project is not None:
        project = (AgentProject.objects.select_related("agent")
                   .filter(pk=payload.project,
                           agent__workspace_id__in=visible_agent_workspaces(request)).first())
        if project is None:
            raise HttpError(404, f"project {payload.project} not found")
        row = engagement.record("viewed", workspace=project.agent.workspace_id, surface=surface,
                                user=request.user, project=project, dedupe_day=True)
    else:
        workspace = _huddle_workspace(request, payload.huddle_id)
        row = engagement.record("viewed", workspace=workspace, surface=surface, user=request.user,
                                huddle_id=payload.huddle_id, dedupe_day=True)
    return {"recorded": row is not None}


@router.get("/summary", response=EngagementSummaryOut,
            summary="What people actually did with projects, huddles and check-in runs")
def engagement_summary(request: HttpRequest, days: int = 14, checkin: str = "", agent: str = "") -> dict:
    """Over the last `days` (default 14), in the workspaces you can list: event
    counts by kind and surface; the unique people who did anything; for each
    project proposed in the window, the time to its first human reaction, the time
    to its decision, its backers and comments; the proposals nobody reacted to —
    for proposed projects and for the tasks check-ins proposed; and check-in runs
    announced vs confirmed vs skipped vs expired, with the median time to
    confirm. `checkin` (a check-in's id, or its name) and `agent` narrow it."""
    return engagement.summary(listed_agent_workspaces(request), days=days, checkin=checkin, agent=agent)

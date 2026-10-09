"""Which session, turn and agent project MADE an artifact — resolved once, at create.

Why (board task hal/T76, 2026-10-09): walkthroughs, narratives and storyboards
carried no link to the work that produced them — 0 of 63 post-OES supply
walkthroughs and 0 of 11 narratives had even a project. So canopy could not say
which turn made a video, which project a narrative served, or (with the
reaction signal beside it) whether that work was any good.

The client already says where it is running: every canopy uploader sends the
``X-Canopy-Parent-{Turn,Session}`` / ``X-Canopy-Claude-Session`` provenance
headers (``apps/common/request_context.py``). So the session and turn are
stamped SERVER side from those, the same resolution a Turn or Session gets
(``provenance.parent_fields``) — an uploader that never heard of this field is
covered. An explicit ``session_id`` / ``turn_id`` in the payload overrides them.

The agent project is resolved in order, first hit wins:
  1. explicit — ``"<agent>/<ext_id>"`` (or ``:``), a bare ext_id (the CALLER's
     own agent, else the parent turn's agent), or a numeric id;
  2. the DDD run doc — ``AgentRun.ext_id == run_id`` names its project
     (``apps/agent_runs/documents.py``); this is how a DDD artifact finds P5;
  3. the parent turn's board task — ``Turn.raised_from_task.project``.

Everything resolved must live in a workspace the caller belongs to; anything
else is dropped, never an error. Provenance is a record, not a gate: a bad id
never refuses the upload.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

_EXPLICIT_PROJECT = re.compile(r"^(?:(?P<agent>[A-Za-z0-9_.-]{1,100})[/:])?(?P<ext>[A-Za-z0-9_.-]{1,64})$")


def _turn_workspace(turn) -> str | None:
    if turn.agent_id:
        return turn.agent.workspace_id
    if turn.chat_session_id:
        return turn.chat_session.workspace_id
    return turn.workspace_id


def _explicit_project(ref: str, *, user, turn):
    from apps.agents.models import Agent, AgentProject

    ref = (ref or "").strip()
    if not ref:
        return None
    if ref.isdigit():
        return AgentProject.objects.select_related("agent").filter(pk=int(ref)).first()
    m = _EXPLICIT_PROJECT.match(ref)
    if not m:
        return None
    agent = None
    if m.group("agent"):
        agent = Agent.objects.filter(slug=m.group("agent")).first()
    else:
        agent = Agent.objects.filter(user=user).first() if getattr(user, "pk", None) else None
        if agent is None and turn is not None:
            if turn.agent_id:
                agent = turn.agent
            elif turn.chat_session_id and turn.chat_session.agent_id:
                agent = turn.chat_session.agent
    if agent is None:
        return None
    return AgentProject.objects.select_related("agent").filter(agent=agent, ext_id=m.group("ext")).first()


def resolve(request, *, session: str = "", turn: str = "", agent_project: str = "",
            run_id: str | None = None) -> dict:
    """``{"source_session", "source_turn", "agent_project"}`` for a new artifact.

    Values are model instances or None. Never raises."""
    out = {"source_session": None, "source_turn": None, "agent_project": None}
    try:
        from apps.agent_runs.models import AgentRun
        from apps.workspaces import services as wsvc

        from . import provenance

        user = getattr(request, "user", None)
        scope = wsvc.user_workspace_slugs(user) if getattr(user, "is_authenticated", False) else set()

        explicit = {k: v.strip() for k, v in (("session", session), ("turn", turn)) if (v or "").strip()}
        fields, _record = provenance.parent_fields(explicit or None)
        parent_turn = fields.get("parent_turn")
        parent_session = fields.get("parent_session")
        if parent_turn is not None and _turn_workspace(parent_turn) in scope:
            out["source_turn"] = parent_turn
        if parent_session is not None and parent_session.workspace_id in scope:
            out["source_session"] = parent_session

        the_turn = out["source_turn"]
        project = _explicit_project(agent_project, user=user, turn=the_turn)
        if project is None and run_id:
            run = AgentRun.objects.select_related("project__agent").filter(
                ext_id=run_id, project__isnull=False
            ).first()
            project = run.project if run is not None else None
        if project is None and the_turn is not None and the_turn.raised_from_task_id:
            task = the_turn.raised_from_task
            project = task.project if task.project_id else None
        if project is not None and project.agent.workspace_id in scope:
            out["agent_project"] = project
    except Exception:  # noqa: BLE001 — a record must never fail the creation
        log.exception("could not resolve an artifact's origin")
    return out


def project_out(project) -> dict | None:
    """The agent project as the artifact APIs show it."""
    if project is None:
        return None
    return {
        "id": project.pk,
        "agent": project.agent.slug if project.agent_id else "",
        "ext_id": project.ext_id,
        "name": project.name,
    }


def parse_project_filter(value: str):
    """A list endpoint's ``agent_project`` filter → a Q-able dict, or None when
    the value names nothing (``"hal/P5"``, ``"hal:P5"`` or a numeric id)."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return {"agent_project_id": int(value)}
    m = _EXPLICIT_PROJECT.match(value)
    if not m or not m.group("agent"):
        return {"agent_project__ext_id": value}
    return {"agent_project__agent__slug": m.group("agent"), "agent_project__ext_id": m.group("ext")}

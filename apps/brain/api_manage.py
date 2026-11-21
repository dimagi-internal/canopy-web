"""Managing canopy learning in a workspace, from its brain page (`/w/<ws>/brain`): "Learning reads" and its learners.

Each route is PINNED (`/api/w/<ws>/brain/…`) and is the workspace's owners' and admins' (`perms.BRAIN_MANAGE`; an
ancestor's owner or admin holds it below, never a sub-workspace's or a sibling's), or a canopy admin's, as a PERSON
only (`api_state.the_person`: never an agent's login, a turn's or caller token, a runner or an MCP call), and none is
an MCP tool. Each is a setting (`@gate(..., setting=True)`): every change is a `SettingChange`, from one plumbing.

A learner (`BrainCapability`) is canopy-wide. Configuring one here is bounded by where its operator agent lives: the
learner's operator, if it has one, must be an agent of this workspace or one below it, and so must the one it is
given (`refusal`). A canopy admin registers learners and changes what people agreed to (its class, purposes,
audience) through `/api/brain/capabilities/`; this route changes only whether it runs and how (`enabled`,
`operator`, `entry`, and `scope`, which may only narrow), and only a project or system learner: a per-person learner
(coaching, memory) and the built-in project readers are canopy-wide, never a workspace's.

Where a learner runs is its targets (`targets.py`): a workspace's owners and admins (or an ancestor's) aim a learner
at their workspace, one below it or a workspace project there, and take a target away, never above their own
workspace; each target's schedule is a normal canopy schedule on the operator, and Run now on the page is that
schedule's own Run now."""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router

from apps.agents.models import Agent
from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.events import audit
from apps.events.models import SettingChange
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import learning_reads, readers, registry, targets
from .api_state import the_person
from .models import ABOUT_A_PERSON, BrainCapability, BrainRun, BrainTarget
from .schemas import (
    BrainRunState,
    BrainTargetIn,
    BrainTargetOut,
    CapabilityOut,
    LearnerConfigIn,
    LearningReadsIn,
    LearningReadsOut,
)

router = Router(auth=session_auth, tags=["brain"])

#: How many of the latest changes of "Learning reads" the page shows.
CHANGES_SHOWN = 10
SET_READS_OP = "set_learning_reads"


def may_manage(user, ws: str) -> bool:
    """May `user` manage learning at `ws`: a canopy admin, or an owner or admin there (inherited from above)."""
    if not ws or not getattr(user, "is_authenticated", False):
        return False
    return bool(getattr(user, "is_superuser", False)) or perms.can(user, ws, perms.BRAIN_MANAGE)


def _subtree(ws: str) -> set[str]:
    return {ws} | wsvc.descendant_slugs({ws})


def learner_refusal(user, ws: str, cap: BrainCapability) -> str:
    """Why `user` may not configure `cap` at `ws`, or ""."""
    if cap.slug == readers.PROJECT_READERS:
        return "the project readers are built in: canopy's, never a workspace's to configure"
    if cap.purpose_class in ABOUT_A_PERSON:
        return f"{cap.slug} learns about one person wherever they work: a canopy admin configures it"
    if not may_manage(user, ws):
        return f"only {ws}'s owners and admins (or an ancestor's) configure its learners"
    if cap.operator_id is not None and cap.operator.workspace_id not in _subtree(ws):
        return f"{cap.slug}'s operator lives outside {ws}; it is configured where its operator is"
    return ""


def _pinned(request: HttpRequest) -> str:
    ws = getattr(request, "workspace_slug", None)
    if not ws:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                           detail="name the workspace: call it under /api/w/<workspace>/")
    return ws


def _changes(ws: str) -> list[dict]:
    rows = SettingChange.objects.filter(workspace_id=ws, operation=SET_READS_OP).order_by("-created_at", "-pk")
    out = []
    for r in rows[:CHANGES_SHOWN]:
        note = next((c for c in r.changes or [] if str(c.get("scope", "")).endswith("/learning_reads")), None)
        if note is None:
            continue
        before, after = note.get("before") or {}, note.get("after") or {}
        out.append({"at": r.created_at, "by": r.actor_email, "before": before.get("own"), "after": after.get("own"),
                    "was_on": bool(before.get("on")), "now_on": bool(after.get("on"))})
    return out


def reads_out(user, ws: str) -> dict:
    """"Learning reads" at `ws` as the page shows it; its changes only to those who may edit it."""
    eff = learning_reads.effective(ws)
    can = may_manage(user, ws)
    return {"workspace": ws, "on": eff.on, "own": eff.own, "source": eff.source, "can_edit": can,
            "changes": _changes(ws) if can else []}


def _state(e: learning_reads.Effective) -> dict:
    return {"own": e.own, "on": e.on, "from": e.source}


def _word(v: bool | None) -> str:
    return "inherit" if v is None else ("on" if v else "off")


@router.put("/brain/learning-reads", response={200: LearningReadsOut, **refusals(403, 422)},
            summary="Turn learning reads on or off for a workspace (its owners and admins)")
@gate("brain.manage", setting=True)  # an owner or admin here or above, or a canopy admin; a person only, off MCP
def set_learning_reads(request: HttpRequest, payload: LearningReadsIn) -> dict:
    """Whether learners may read the sessions of the workspace the request is pinned to (`/api/w/<ws>/…`): on, off,
    or null to inherit from the nearest workspace above that sets it (none: off). Its workspaces below inherit it
    unless they set their own. On, a session is still read only when everyone in it agreed. Every change is
    audited and listed on the brain page."""
    user = the_person(request)
    ws = _pinned(request)
    if not may_manage(user, ws):
        raise ProblemError(403, f"Only {ws}'s owners and admins (or an ancestor's) change its learning reads",
                           type_=TYPE_FORBIDDEN)
    workspace = Workspace.objects.get(slug=ws)
    before, after = learning_reads.set_reads(workspace, payload.on, by=user)
    audit.note_change(scope=f"workspace:{ws}/learning_reads", before=_state(before), after=_state(after),
                      workspace=workspace,
                      summary=(f"{user.email} set learning reads in {ws} from {_word(before.own)} to "
                               f"{_word(after.own)} (in effect: {'on' if after.on else 'off'})"))
    return reads_out(user, ws)


@router.patch("/brain/learners/{slug}/", response={200: CapabilityOut, **refusals(403, 404, 422)},
              summary="Enable or configure a learner from a workspace (its owners and admins)")
@gate("brain.manage", setting=True)  # an owner or admin here or above, or a canopy admin; a person only, off MCP
def configure_learner(request: HttpRequest, slug: str, payload: LearnerConfigIn) -> dict:
    """Enable or disable a learner, name its operator agent and the plugin command a run starts with, or narrow how
    much of a session it reads, from the workspace the request is pinned to. Only a learner whose operator (if it has
    one) is an agent of this workspace or below, and only to such an agent, which must also live where every target
    of it is or above (its targets' schedules move to it). Enabling one that nobody active answers for makes you its
    accountable admin: its targets' schedules act for you. What it may learn
    and who sees it (its class, purposes, audience) are a canopy admin's, at its registration."""
    from .api_admin import _cap_out  # noqa: PLC0415 — api_admin imports this module

    user = the_person(request)
    ws = _pinned(request)
    cap = BrainCapability.objects.filter(slug=slug).select_related("operator").first()
    if cap is None:
        raise ProblemError(404, "No such learner", type_=TYPE_NOT_FOUND)
    why = learner_refusal(user, ws, cap)
    if why:
        raise ProblemError(403, why, type_=TYPE_FORBIDDEN)
    changes = payload.model_dump(exclude_unset=True)
    if "operator" in changes:
        name = (changes["operator"] or "").strip()
        agent = Agent.objects.filter(slug=name).first() if name else None
        if name and (agent is None or agent.workspace_id not in _subtree(ws)):
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                               detail=f"no agent {name!r} in {ws} or a workspace below it")
        changes["operator"] = agent
        why = targets.operator_refusal(cap, agent)
        if why:
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=why)
    changes = {k: v for k, v in changes.items() if v is not None or k == "operator"}
    try:
        cap = registry.update(cap, by=user, **changes)
    except registry.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    targets.follow(cap)
    return _cap_out(cap)


# ---- targets ------------------------------------------------------------------------------------------------

def _visible_targets(ws: str):
    """The targets in `ws` and the workspaces below it."""
    return (BrainTarget.objects.filter(workspace_id__in=_subtree(ws))
            .select_related("capability", "project", "schedule").order_by("capability__slug", "workspace_id", "pk"))


def target_out(t: BrainTarget, *, can_manage: bool, last=None) -> dict:
    s = t.schedule
    return {"id": t.pk, "learner": t.capability.slug, "workspace": t.workspace_id,
            "project": t.project.handle if t.project_id else "", "label": targets.label(t),
            "schedule_id": s.pk, "agent": s.agent.slug, "cron": s.cron, "timezone": s.timezone, "enabled": s.enabled,
            "created_by": getattr(t.created_by, "email", "") or "", "created_at": t.created_at,
            "last_run": ({"id": last.pk, "state": last.state, "created_at": last.created_at} if last else None),
            "can_manage": can_manage}


def targets_out(user, ws: str) -> list[dict]:
    """Every target at `ws` and below, with its schedule and last run; `can_manage` where the viewer manages it."""
    rows = list(_visible_targets(ws).select_related("schedule__agent", "created_by"))
    last: dict[int, BrainRun] = {}
    for r in BrainRun.objects.filter(target__in=rows).only("pk", "target_id", "state", "created_at").order_by("-pk"):
        last.setdefault(r.target_id, r)
    managed = perms.slugs_with(user, perms.BRAIN_MANAGE, within={t.workspace_id for t in rows})
    sup = bool(getattr(user, "is_superuser", False))
    return [target_out(t, can_manage=sup or t.workspace_id in managed, last=last.get(t.pk)) for t in rows]


def _target_or_404(ws: str, target_id: int) -> BrainTarget:
    t = _visible_targets(ws).filter(pk=target_id).first()
    if t is None:
        raise ProblemError(404, "No such target here", type_=TYPE_NOT_FOUND)
    return t


@router.post("/brain/targets", response={200: BrainTargetOut, **refusals(403, 404, 422)},
             summary="Aim a learner at a workspace or a project (its owners and admins)")
@gate("brain.manage", setting=True)  # an owner or admin of the target's workspace or above; a person only, off MCP
def add_learning_target(request: HttpRequest, payload: BrainTargetIn) -> dict:
    """Aim a learner at the workspace the request is pinned to or one below it (`workspace`, default the pinned one), or
    at a workspace project there (`project`, a handle). Its runs then read only there, on a schedule of their own: a
    normal canopy schedule on the learner's operator (`cron`, `timezone`; daily at 06:00 UTC by default), listed beside
    the agent's other schedules, firing as its accountable admin's standing request. A system learner takes a workspace
    or a project, a project learner a project. Refused when its operator lives outside that workspace and the ones
    above it, it has no operator or nobody answers for it, or it is aimed there already."""
    user = the_person(request)
    ws = _pinned(request)
    cap = BrainCapability.objects.filter(slug=payload.learner).select_related("operator").first()
    if cap is None:
        raise ProblemError(404, "No such learner", type_=TYPE_NOT_FOUND)
    where = (payload.workspace or ws).strip().lower()
    if where not in _subtree(ws) or not may_manage(user, where):
        raise ProblemError(403, f"Only the owners and admins of {where} (or an ancestor's) aim a learner there, from "
                           f"{where} or above", type_=TYPE_FORBIDDEN)
    workspace = Workspace.objects.get(slug=where)
    project = None
    if payload.project.strip():
        from apps.agents.models import AgentProject  # noqa: PLC0415 — as the other lazy model imports here

        project = AgentProject.objects.filter(workspace=workspace, handle=payload.project.strip().lower(),
                                              visibility=AgentProject.WORKSPACE).first()
        if project is None:
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                               detail=f"no workspace project {payload.project!r} in {where}")
    why = targets.run_place_refusal(cap, where)
    if why:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=why)
    try:
        t = targets.add(cap, workspace=workspace, project=project, by=user, cron=payload.cron or targets.DEFAULT_CRON,
                        tz=payload.timezone or targets.DEFAULT_TZ)
    except targets.TargetRefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return target_out(t, can_manage=True)


@router.delete("/brain/targets/{target_id}", response={204: None, **refusals(403, 404)},
               summary="Take a learner's target away (its owners and admins)")
@gate("brain.manage", setting=True)  # an owner or admin of the target's workspace or above; a person only, off MCP
def remove_learning_target(request: HttpRequest, target_id: int):
    """The learner stops running there; its schedule goes too. A run still open for it finishes; what was found stays."""
    from ninja import Status  # noqa: PLC0415 — only this route answers 204

    user = the_person(request)
    t = _target_or_404(_pinned(request), target_id)
    if not may_manage(user, t.workspace_id):
        raise ProblemError(403, f"Only the owners and admins of {t.workspace_id} (or an ancestor's) change its targets",
                           type_=TYPE_FORBIDDEN)
    audit.note_change(scope=f"brain-target:{t.pk}", before=target_out(t, can_manage=True) | {"created_at": str(
        t.created_at)}, after=None, workspace=t.workspace,
        summary=f"{user.email} took {t.capability.slug}'s target {targets.label(t)} away")
    targets.remove(t)
    return Status(204, None)


@router.post("/brain/targets/{target_id}/runs", response={200: BrainRunState, **refusals(403, 404, 422)},
             summary="Run a learner's target now (its owners and admins)")
@gate("brain.manage", kind="turn")  # an owner or admin of the target's workspace or above; a person only, off MCP
def run_learning_target(request: HttpRequest, target_id: int) -> dict:
    """The target's schedule's own Run now: one run, now, for this target, answering to the schedule's creator (the
    learner's accountable admin), with you recorded as who pressed it. Refused with the reason when it cannot start
    (learning reads off there, the learner disabled or without an operator or entry, a run still open, nothing new)."""
    user = the_person(request)
    t = _target_or_404(_pinned(request), target_id)
    if not may_manage(user, t.workspace_id):
        raise ProblemError(403, f"Only the owners and admins of {t.workspace_id} (or an ancestor's) run it",
                           type_=TYPE_FORBIDDEN)
    try:
        run = targets.run_now(t, by=user)
    except targets.TargetRefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    subject = t.project.handle if t.project_id else "system"
    return {"id": run.pk, "learner": t.capability.slug, "purpose": run.purpose, "subject_type": run.subject_type,
            "subject": subject, "scope": run.scope, "state": run.state, "created_at": run.created_at,
            "finished_at": None, "reads": 0, "sessions": [], "hidden": 0}

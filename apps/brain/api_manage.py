"""Managing canopy learning in a workspace, from its brain page (`/w/<ws>/brain`): "Learning reads" and its learners.

Each route is PINNED (`/api/w/<ws>/brain/…`) and is the workspace's owners' and admins' (`perms.BRAIN_MANAGE`; an
ancestor's owner or admin holds it below, never a sub-workspace's or a sibling's), or a canopy admin's, asked by ONE
rule (`actor.of`): the person as themselves, or an admin agent's own login inside its own running turn whose
accountable person manages there too; never a learning run's own turn, an OAuth, delegated, act-as or caller token,
a runner or a guest. A project target is also its project's admins' (`may_manage_target`). Each is an MCP tool (an
agent reaches canopy through MCP) and each is a setting (`@gate(..., setting=True)`): every change is a
`SettingChange`, from one plumbing, naming the agent, its turn and the person it acted for (`actor.note`).

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

import uuid

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from ninja import Router, Status

from apps.agents import member_roles, project_members
from apps.agents.models import Agent, AgentProject
from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session
from apps.events import audit
from apps.events.models import SettingChange
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace, WorkspaceMembership

from . import actor as actor_mod
from . import (
    alerts_removal,
    budget,
    consent,
    eligibility,
    learning_reads,
    participants,
    readers,
    registry,
    sources,
    targets,
)
from .api_admin import _cap_out
from .models import ABOUT_A_PERSON, BrainCapability, BrainRun, BrainRunArtifact, BrainTarget
from .schemas import (
    BrainRunState,
    BrainTargetBudgetIn,
    BrainTargetIn,
    BrainTargetOut,
    BrainWhyOut,
    CapabilityOut,
    ConsentPersonOut,
    LearnerConfigIn,
    LearningOptInIn,
    LearningReadsIn,
    LearningReadsOut,
    OptInPersonOut,
)

router = Router(auth=session_auth, tags=["brain"])

#: How many of the latest changes of "Learning reads" the page shows.
CHANGES_SHOWN = 10
SET_READS_OP = "set_learning_reads"


#: May `user` manage learning at `ws` (`actor.may_manage`): a canopy admin, or an owner or admin there or above.
may_manage = actor_mod.may_manage


def _can(who, ws: str) -> bool:
    """`may_manage` for a user, or for an `Actor` (both its sides)."""
    return who.may_manage(ws) if isinstance(who, actor_mod.Actor) else may_manage(who, ws)


def _project_admin(user, project) -> bool:
    m = project_members.members(project).get(getattr(user, "pk", None))
    return m is not None and member_roles.is_admin(m.role)


def may_manage_target(actor: actor_mod.Actor, ws: str, project=None) -> bool:
    """May `actor` change a target at `ws` (of `project`): it manages learning there, or the target is a project's and
    it is that project's lead or admin; an agent, a member agent of the project whose turn answers to its lead or an
    admin of it."""
    if actor.may_manage(ws):
        return True
    if project is None:
        return False
    if not actor.is_agent:
        return _project_admin(actor.user, project)
    return (wsvc.is_member(actor.user, project.workspace_id)
            and project.member_agents.filter(pk=actor.agent.pk).exists() and _project_admin(actor.person, project))


def _subtree(ws: str) -> set[str]:
    return {ws} | wsvc.descendant_slugs({ws})


def learner_refusal(user, ws: str, cap: BrainCapability) -> str:
    """Why `user` may not configure `cap` at `ws`, or ""."""
    if cap.slug == readers.PROJECT_READERS:
        return "the project readers are built in: canopy's, never a workspace's to configure"
    if cap.purpose_class in ABOUT_A_PERSON:
        return f"{cap.slug} learns about one person wherever they work: a canopy admin configures it"
    if not _can(user, ws):
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
        acting = r.acting or {}
        by = f"{acting['agent']} for {acting.get('for', '')}" if acting.get("agent") else r.actor_email
        out.append({"at": r.created_at, "by": by, "before": before.get("own"), "after": after.get("own"),
                    "was_on": bool(before.get("on")), "now_on": bool(after.get("on"))})
    return out


def reads_out(user, ws: str) -> dict:
    """"Learning reads" at `ws` as the page shows it; its changes only to those who may edit it."""
    eff = learning_reads.effective(ws)
    can = _can(user, ws)
    return {"workspace": ws, "on": eff.on, "own": eff.own, "source": eff.source, "can_edit": can,
            "changes": _changes(ws) if can else []}


def _state(e: learning_reads.Effective) -> dict:
    return {"own": e.own, "on": e.on, "from": e.source}


def _word(v: bool | None) -> str:
    return "inherit" if v is None else ("on" if v else "off")


@router.put("/brain/learning-reads", response={200: LearningReadsOut, **refusals(403, 422)},
            summary="Turn learning reads on or off for a workspace (its owners and admins)")
@gate("brain.manage", setting=True)  # an owner or admin here or above, or a canopy admin; a person or an admin agent
def set_learning_reads(request: HttpRequest, payload: LearningReadsIn) -> dict:
    """Whether learners may read the sessions of the workspace the request is pinned to (`/api/w/<ws>/…`): on, off,
    or null to inherit from the nearest workspace above that sets it (none: off). Its workspaces below inherit it
    unless they set their own. On, a session is still read only when everyone in it agreed. Every change is
    audited and listed on the brain page."""
    actor = actor_mod.of(request)
    ws = _pinned(request)
    if not actor.may_manage(ws):
        raise ProblemError(403, f"Only {ws}'s owners and admins (or an ancestor's) change its learning reads",
                           type_=TYPE_FORBIDDEN)
    workspace = Workspace.objects.get(slug=ws)
    before, after = learning_reads.set_reads(workspace, payload.on, by=actor.user)
    actor_mod.note(request, actor)
    audit.note_change(scope=f"workspace:{ws}/learning_reads", before=_state(before), after=_state(after),
                      workspace=workspace,
                      summary=(f"{actor.label} set learning reads in {ws} from {_word(before.own)} to "
                               f"{_word(after.own)} (in effect: {'on' if after.on else 'off'})"))
    return reads_out(actor, ws)


@router.patch("/brain/learners/{slug}/", response={200: CapabilityOut, **refusals(403, 404, 422)},
              summary="Enable or configure a learner from a workspace (its owners and admins)")
@gate("brain.manage", setting=True)  # an owner or admin here or above, or a canopy admin; a person or an admin agent
def configure_learner(request: HttpRequest, slug: str, payload: LearnerConfigIn) -> dict:
    """Enable or disable a learner, name its operator agent and the plugin command a run starts with, or narrow how
    much of a session it reads, from the workspace the request is pinned to. Only a learner whose operator (if it has
    one) is an agent of this workspace or below, and only to such an agent, which must also live where every target
    of it is or above (its targets' schedules move to it). Enabling one that nobody active answers for makes you its
    accountable admin: its targets' schedules act for you (an agent's turn: the person it answers to). What it may learn
    and who sees it (its class, purposes, audience) are a canopy admin's, at its registration."""
    actor = actor_mod.of(request)
    ws = _pinned(request)
    cap = BrainCapability.objects.filter(slug=slug).select_related("operator").first()
    if cap is None:
        raise ProblemError(404, "No such learner", type_=TYPE_NOT_FOUND)
    why = learner_refusal(actor, ws, cap)
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
    actor_mod.note(request, actor)
    try:
        # A learner nobody answers for becomes the PERSON's: an agent never answers for a learner.
        cap = registry.update(cap, by=actor.person, **changes)
    except registry.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    targets.follow(cap)
    return _cap_out(cap)


# ---- what a removal could not undo ---------------------------------------------------------------------------

def flags_out(user, ws: str, slugs) -> list[dict]:
    """Flagged artifacts at `ws` and below that `user` (or an `Actor`) sees: in a workspace they manage, or raised to
    them."""
    where = set(slugs) & _subtree(ws)
    if isinstance(user, actor_mod.Actor):
        managed, sup, user = user.managed(where), False, user.user
    else:
        managed = perms.slugs_with(user, perms.BRAIN_MANAGE, within=where)
        sup = bool(getattr(user, "is_superuser", False))
    qs = (BrainRunArtifact.objects.filter(state=BrainRunArtifact.FLAGGED, workspace_id__in=where)
          .select_related("run__capability", "owner").order_by("-changed_at", "-pk"))
    return [{"id": a.pk, "kind": a.kind, "ref": a.ref, "workspace": a.workspace_id, "run": a.run_id,
             "learner": a.run.capability.slug, "owner": getattr(a.owner, "email", "") or "", "reason": a.reason,
             "changed_at": a.changed_at, "acknowledged": a.acknowledged_at is not None}
            for a in qs[:200] if sup or a.workspace_id in managed or a.owner_id == user.pk]


@router.post("/brain/flags/{flag_id}/acknowledge", response={204: None, **refusals(403, 404)},
             summary="Acknowledge something a removal could not undo (its owner or the workspace's managers)")
@gate("brain.manage", "self", kind="action")  # its owner, or a manager of its workspace; a person or an admin agent
def acknowledge_learning_flag(request: HttpRequest, flag_id: int):
    """You have seen what a learning run made that canopy could not take back after a session it came from was
    removed, and decided what to do with it: the alert raised to its owner closes."""
    actor = actor_mod.of(request)
    ws = _pinned(request)
    art = BrainRunArtifact.objects.filter(pk=flag_id, state=BrainRunArtifact.FLAGGED,
                                          workspace_id__in=_subtree(ws)).first()
    if art is None:
        raise ProblemError(404, "No such flag here", type_=TYPE_NOT_FOUND)
    allowed = actor.may_manage(art.workspace_id) if actor.is_agent else alerts_removal.may_acknowledge(actor.user, art)
    if not allowed:
        raise ProblemError(403, "Only its owner or the workspace's managers acknowledge it", type_=TYPE_FORBIDDEN)
    alerts_removal.acknowledge(art, actor.user)
    actor_mod.event(actor, kind="flag.acknowledged", summary=f"acknowledged learning flag {art.pk}",
                    workspace=art.workspace, payload={"flag": art.pk})
    return Status(204, None)


# ---- targets ------------------------------------------------------------------------------------------------

def _visible_targets(ws: str):
    """The targets in `ws` and the workspaces below it."""
    return (BrainTarget.objects.filter(workspace_id__in=_subtree(ws))
            .select_related("capability", "project", "schedule").order_by("capability__slug", "workspace_id", "pk"))


def target_out(t: BrainTarget, *, can_manage: bool, last=None) -> dict:
    s = t.schedule
    return {"budget": budget.out(t), "id": t.pk, "learner": t.capability.slug, "workspace": t.workspace_id,
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
    where = {t.workspace_id for t in rows}
    if isinstance(user, actor_mod.Actor):
        managed, sup = user.managed(where), False
    else:
        managed = perms.slugs_with(user, perms.BRAIN_MANAGE, within=where)
        sup = bool(getattr(user, "is_superuser", False))
    return [target_out(t, can_manage=sup or t.workspace_id in managed, last=last.get(t.pk)) for t in rows]


def _target_or_404(ws: str, target_id: int) -> BrainTarget:
    t = _visible_targets(ws).filter(pk=target_id).first()
    if t is None:
        raise ProblemError(404, "No such target here", type_=TYPE_NOT_FOUND)
    return t


@router.post("/brain/targets", response={200: BrainTargetOut, **refusals(403, 404, 422)},
             summary="Aim a learner at a workspace or a project (its owners and admins)")
@gate("brain.manage", setting=True)  # a manager of its workspace or above, or the project's (actor.of)
def add_learning_target(request: HttpRequest, payload: BrainTargetIn) -> dict:
    """Aim a learner at the workspace the request is pinned to or one below it (`workspace`, default the pinned one), or
    at a workspace project there (`project`, a handle). Its runs then read only there, on a schedule of their own: a
    normal canopy schedule on the learner's operator (`cron`, `timezone`; daily at 06:00 UTC by default), listed beside
    the agent's other schedules, firing as its accountable admin's standing request. A system learner takes a workspace
    or a project, a project learner a project. Refused when its operator lives outside that workspace and the ones
    above it, it has no operator or nobody answers for it, or it is aimed there already."""
    actor = actor_mod.of(request)
    # Unpinned, the body's `workspace` is where it acts: it is also the MCP tool's routing argument, so the tool
    # reaches this route unpinned. The same rule then holds, with that workspace as the pin.
    if not getattr(request, "workspace_slug", None) and payload.workspace.strip():
        request.workspace_slug = payload.workspace.strip().lower()
    ws = _pinned(request)
    cap = BrainCapability.objects.filter(slug=payload.learner).select_related("operator").first()
    if cap is None:
        raise ProblemError(404, "No such learner", type_=TYPE_NOT_FOUND)
    where = (payload.workspace or ws).strip().lower()
    refused = ProblemError(403, f"Only the owners and admins of {where} (or an ancestor's), or of the project, aim a "
                           f"learner there, from {where} or above", type_=TYPE_FORBIDDEN)
    if where not in _subtree(ws) or not Workspace.objects.filter(slug=where).exists():
        raise refused
    if not payload.project.strip() and not actor.may_manage(where):
        raise refused
    workspace = Workspace.objects.get(slug=where)
    project = None
    if payload.project.strip():
        project = AgentProject.objects.filter(workspace=workspace, handle=payload.project.strip().lower(),
                                              visibility=AgentProject.WORKSPACE).first()
        if project is None:
            raise (refused if not actor.may_manage(where) else
                   ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                                detail=f"no workspace project {payload.project!r} in {where}"))
        if not may_manage_target(actor, where, project):
            raise refused
    why = targets.run_place_refusal(cap, where)
    if why:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=why)
    try:
        actor_mod.note(request, actor)
        t = targets.add(cap, workspace=workspace, project=project, by=actor.user,
                        cron=payload.cron or targets.DEFAULT_CRON, tz=payload.timezone or targets.DEFAULT_TZ)
    except targets.TargetRefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return target_out(t, can_manage=True)


@router.delete("/brain/targets/{target_id}", response={204: None, **refusals(403, 404)},
               summary="Take a learner's target away (its owners and admins)")
@gate("brain.manage", setting=True)  # a manager of its workspace or above, or the project's (actor.of)
def remove_learning_target(request: HttpRequest, target_id: int):
    """The learner stops running there; its schedule goes too. A run still open for it finishes; what was found stays."""
    actor = actor_mod.of(request)
    t = _target_or_404(_pinned(request), target_id)
    if not may_manage_target(actor, t.workspace_id, t.project):
        raise ProblemError(403, f"Only the owners and admins of {t.workspace_id} (or an ancestor's) change its targets",
                           type_=TYPE_FORBIDDEN)
    actor_mod.note(request, actor)
    audit.note_change(scope=f"brain-target:{t.pk}", before=target_out(t, can_manage=True) | {"created_at": str(
        t.created_at)}, after=None, workspace=t.workspace,
        summary=f"{actor.label} took {t.capability.slug}'s target {targets.label(t)} away")
    targets.remove(t)
    return Status(204, None)


@router.post("/brain/targets/{target_id}/runs", response={200: BrainRunState, **refusals(403, 404, 422)},
             summary="Run a learner's target now (its owners and admins)")
@gate("brain.manage", kind="turn")  # a manager of its workspace or above, or the project's (actor.of)
def run_learning_target(request: HttpRequest, target_id: int) -> dict:
    """The target's schedule's own Run now: one run, now, for this target, answering to the schedule's creator (the
    learner's accountable admin), with you recorded as who pressed it. Refused with the reason when it cannot start
    (learning reads off there, the learner disabled or without an operator or entry, a run still open, nothing new)."""
    actor = actor_mod.of(request)
    t = _target_or_404(_pinned(request), target_id)
    if not may_manage_target(actor, t.workspace_id, t.project):
        raise ProblemError(403, f"Only the owners and admins of {t.workspace_id} (or an ancestor's) run it",
                           type_=TYPE_FORBIDDEN)
    try:
        run = targets.run_now(t, by=actor.user)
    except targets.TargetRefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    actor_mod.event(actor, kind="target.run_now", summary=f"ran {t.capability.slug}'s target {targets.label(t)} now",
                    workspace=t.workspace, payload={"target": t.pk, "run": run.pk})
    subject = t.project.handle if t.project_id else "system"
    return {"id": run.pk, "learner": t.capability.slug, "purpose": run.purpose, "subject_type": run.subject_type,
            "subject": subject, "scope": run.scope, "state": run.state, "created_at": run.created_at,
            "finished_at": None, "reads": 0, "sessions": [], "hidden": 0}


@router.patch("/brain/targets/{target_id}/budget", response={200: BrainTargetOut, **refusals(403, 404, 422)},
              summary="Change a target's budget (its owners and admins)")
@gate("brain.manage", setting=True)  # a manager of its workspace or above, or the project's (actor.of)
def set_target_budget(request: HttpRequest, target_id: int, payload: BrainTargetBudgetIn) -> dict:
    """How much one run of this target may read (sessions) and write (outputs), and how many runs, outputs and tokens
    it may spend in a UTC day, how far back its runs read (`lookback_days`: only sessions active that recently) and how
    long an open finding nobody re-sees stays fresh (`fade_days`). A run stops cleanly at a cap: nothing more is read or
    written, and a fire past a daily cap starts nothing and says why. Fields left out keep their value; `clear_tokens`
    removes the token cap."""
    actor = actor_mod.of(request)
    t = _target_or_404(_pinned(request), target_id)
    if not may_manage_target(actor, t.workspace_id, t.project):
        raise ProblemError(403, f"Only the owners and admins of {t.workspace_id} (or an ancestor's) change its budget",
                           type_=TYPE_FORBIDDEN)
    actor_mod.note(request, actor)
    fields = (*budget.FIELDS, *budget.WINDOW_FIELDS)
    changes = {k: v for k, v in payload.model_dump(exclude_unset=True).items() if v is not None and k in fields}
    if payload.clear_tokens:
        changes["max_tokens_per_day"] = None
    before = budget.out(t)
    targets.set_budget(t, **changes)
    after = budget.out(t)
    audit.note_change(scope=f"brain-target:{t.pk}/budget", before={k: before[k] for k in fields},
                      after={k: after[k] for k in fields}, workspace=t.workspace,
                      summary=f"{actor.label} changed the budget of {t.capability.slug}'s target {targets.label(t)}")
    return target_out(t, can_manage=True)


# ---- why a session is not read ------------------------------------------------------------------------------

#: What each reason of `eligibility.explain` means, in words; a reason naming a person says "you" or "someone in it".
WHY = {
    "no_source": "canopy has not looked at this session yet (it does within minutes of it changing)",
    "withdrawn": "its words were deleted, or it was taken out of learning for good",
    "sealed:sensitive": "it is a sensitive conversation: no learning ever reads it",
    "sealed:contact": "it is a conversation with someone outside canopy: no learning reads it",
    "sealed:words_deleted": "its words were deleted",
    "sealed:brain_run": "it is a learning run's own conversation: learning never learns from itself",
    "learning_reads_off": "learning reads are off in its workspace (its owners and admins turn them on here)",
    "capability_not_enabled": "that learner is not enabled",
    "purpose_not_declared": "that learner does not read for this purpose",
    "source_kind_not_declared": "that learner does not read sessions",
    "subject_not_a_participant": "you are not in it, and this learner learns about one person from their own words",
    "contact_in_source": "someone outside canopy is in it, so no shared learning reads it",
    "no_human_participant": "nobody is recorded as in it",
    "excluded": "{who} took it out of learning",
    "no_consent": "{who} has not agreed to canopy learning",
    "class_not_consented": "{who} has not agreed to this kind of learning",
    "outside_workspaces": "{who} agreed to learning only in other workspaces",
    "standing_rule": "a rule {who} set refuses this learner",
    "not_a_member": "{who} is not a member of its workspace",
}


def _said(reason: str, user) -> tuple[str, str]:
    """`reason` for the person asking: their own id as `you`, anyone else's as `someone`, never a name."""
    code, _, who = reason.partition(":")
    if code in ("sealed",):
        return reason, WHY.get(reason, reason)
    if who:
        mine = who == str(user.pk)
        words = WHY.get(code, code).format(who="you" if mine else "someone in it")
        return f"{code}:{'you' if mine else 'someone'}", words[0].upper() + words[1:]
    words = WHY.get(code, code)
    return code, words[0].upper() + words[1:]


@router.get("/brain/why/{session_id}", response={200: BrainWhyOut, **refusals(403, 404, 422)},
            summary="Why a learner may or may not read a session (yours, or one you read)")
@gate("session-acl")  # a session you are in or read yourself (an admin agent: one its login reads); names only you
def why_not_read(request: HttpRequest, session_id: uuid.UUID, learner: str = readers.PROJECT_READERS) -> dict:
    """Whether `learner` (default: a project's readers) may read this session now, and if not, why, by the same rule
    every learning read asks (`eligibility.explain`). Only for a session in this workspace or below that you are in or
    read yourself (an admin agent here: one its own login reads). A reason about a person says whether it is you or
    "someone in it", never who."""
    actor = actor_mod.of(request)
    user = actor.user
    ws = _pinned(request)
    if actor.is_agent and not actor.may_manage(ws):
        raise ProblemError(403, f"Only {ws}'s owners and admins (or an ancestor's) ask this as an agent",
                           type_=TYPE_FORBIDDEN)
    session = Session.objects.filter(pk=session_id, workspace_id__in=_subtree(ws)).first()
    in_it = session is not None and any(u == user.pk for _h, u, _c in participants.people_of(session))
    if session is None or not (in_it or access.readable_ids(user, [session.pk])):
        raise ProblemError(404, "No such session here", type_=TYPE_NOT_FOUND)
    cap = BrainCapability.objects.filter(slug=learner).first()
    if cap is None:
        raise ProblemError(404, "No such learner", type_=TYPE_NOT_FOUND)
    source = sources.existing(session)
    subject = user.pk if cap.purpose_class in ABOUT_A_PERSON else None
    verdict = (eligibility.explain(source, cap, (cap.purposes or [""])[0], subject) if source is not None
               else eligibility.Eligibility(False, "no_source"))
    reason, words = _said(verdict.reason, user) if not verdict.ok else ("", "")
    return {"session_id": str(session.pk), "learner": cap.slug, "eligible": verdict.ok, "reason": reason,
            "explanation": words}


# ---- consent, on an admin's word ---------------------------------------------------------------------------------

def _members(ws: str) -> dict[int, object]:
    """The people (never an agent's login, never a guest) who are members of `ws` or a workspace below it."""
    ids = (WorkspaceMembership.objects.filter(workspace_id__in=_subtree(ws))
           .exclude(role=WorkspaceMembership.GUEST).values_list("user_id", flat=True))
    agents = set(Agent.objects.exclude(user_id=None).values_list("user_id", flat=True))
    return {u.pk: u for u in get_user_model().objects.filter(pk__in=set(ids) - agents, is_active=True)}


def _managing(request: HttpRequest):
    actor = actor_mod.of(request)
    ws = _pinned(request)
    if not actor.may_manage(ws):
        raise ProblemError(403, f"Only {ws}'s owners and admins (or an ancestor's) opt its people in",
                           type_=TYPE_FORBIDDEN)
    return actor, ws


@router.get("/brain/consent", response={200: list[ConsentPersonOut], **refusals(403, 422)},
            summary="Who here has agreed to which learning (its owners and admins)")
@gate("brain.manage")  # an owner or admin here or above, or a canopy admin; a person or an admin agent
def list_consent(request: HttpRequest) -> list[dict]:
    """Every member of the pinned workspace and those below it who has agreed to any learning, with their classes
    and the workspaces it covers (empty: all of theirs)."""
    _actor, ws = _managing(request)
    people = _members(ws)
    rows = consent.LearningConsent.objects.filter(user_id__in=people, revoked_at__isnull=True)
    return [{"email": people[r.user_id].email, "name": people[r.user_id].get_full_name(),
             "classes": list(r.classes), "workspaces": list(r.workspaces or [])}
            for r in rows.order_by("user__email")]


@router.post("/brain/consent", response={200: list[OptInPersonOut], **refusals(403, 422)},
             summary="Opt people in to learning (its owners and admins)")
@gate("brain.manage", setting=True)  # an owner or admin here or above, or a canopy admin; a person or an admin agent
def opt_people_in(request: HttpRequest, payload: LearningOptInIn) -> list[dict]:
    """Add learning classes to the consent of members of the pinned workspace or below: those named (`people`, by
    email) and, with `already_agreed`, everyone there who agreed to any class already. It only adds: taking a class
    away, narrowing and withdrawing a session stay each person's own, in Settings → Learning, where the change shows
    as made by you. A person with no consent gets one covering only their workspaces you manage; one whose consent
    reaches a workspace you do not manage is refused (`refused`). Each person's change is on their own audit log."""
    actor, ws = _managing(request)
    people = _members(ws)
    by_email = {u.email.lower(): u for u in people.values()}
    unknown = sorted({e.strip().lower() for e in payload.people} - set(by_email))
    if unknown:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                           detail=f"not a member of {ws} or below: {', '.join(unknown)}")
    chosen = {by_email[e.strip().lower()].pk for e in payload.people}
    if payload.already_agreed:
        chosen |= set(consent.LearningConsent.objects.filter(user_id__in=people, revoked_at__isnull=True)
                      .values_list("user_id", flat=True))
    if not chosen:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail="name someone to opt in")
    managed = actor.managed(wsvc.descendant_slugs({ws}) | {ws} | {s for u in chosen
                                                                  for s in wsvc.user_workspace_slugs(people[u])})
    out = []
    for pk in sorted(chosen, key=lambda k: people[k].email):
        u = people[pk]
        try:
            row, added = consent.admin_opt_in(u, classes=payload.classes, managed=managed, by=actor.label)
        except consent.RefusedError as e:
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
        except consent.OptInRefusedError as e:
            live = consent.live(u)
            out.append({"email": u.email, "added": [], "classes": list(live.classes) if live else [],
                        "refused": str(e)})
            continue
        out.append({"email": u.email, "added": added, "classes": list(row.classes)})
    actor_mod.note(request, actor)
    workspace = Workspace.objects.get(slug=ws)
    audit.note_change(scope=f"workspace:{ws}/learning_consent", before={}, after={"people": out},
                      workspace=workspace,
                      summary=f"{actor.label} opted {sum(1 for o in out if o['added'])} people in to "
                              f"{', '.join(sorted(set(payload.classes)))} learning in {ws}")
    return out

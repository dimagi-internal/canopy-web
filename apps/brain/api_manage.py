"""Managing canopy learning in a workspace, from its brain page (`/w/<ws>/brain`): "Learning reads" and its learners.

Each route is PINNED (`/api/w/<ws>/brain/…`) and is the workspace's owners' and admins' (`perms.BRAIN_MANAGE`; an
ancestor's owner or admin holds it below, never a sub-workspace's or a sibling's), or a canopy admin's, as a PERSON
only (`api_state.the_person`: never an agent's login, a turn's or caller token, a runner or an MCP call), and none is
an MCP tool. Each is a setting (`@gate(..., setting=True)`): every change is a `SettingChange`, from one plumbing.

A learner (`BrainCapability`) is canopy-wide. Configuring one here is bounded by where its operator agent lives: the
learner's operator, if it has one, must be an agent of this workspace or one below it, and so must the one it is
given (`refusal`). A canopy admin registers learners and changes what people agreed to (its class, purposes,
audience) through `/api/brain/capabilities/`; this route changes only whether it runs and how (`enabled`,
`operator`, `entry`, `schedule`, and `scope`, which may only narrow), and only a project or system learner: a
per-person learner (coaching, memory) and the built-in project readers are canopy-wide, never a workspace's."""
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

from . import learning_reads, readers, registry
from .api_state import the_person
from .models import ABOUT_A_PERSON, BrainCapability
from .schemas import CapabilityOut, LearnerConfigIn, LearningReadsIn, LearningReadsOut

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


def run_place_refusal(cap: BrainCapability, ws: str) -> str:
    """Why a run of `cap` cannot be started for `ws` because of where its operator lives, or "": the operator must be
    an agent of `ws` or of a workspace above it, so a run's conversation never holds a workspace's words in a sibling's
    or a sub-workspace's agent."""
    if cap.operator_id is None:
        return ""
    home = Workspace.objects.filter(slug=ws).first()
    above = {ws, *(home.ancestor_slugs() if home is not None else [])}
    if cap.operator.workspace_id not in above:
        return f"{cap.slug}'s operator lives outside {ws} and the workspaces above it; it cannot read {ws}"
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
    """Enable or disable a learner, name its operator agent and the plugin command a run starts with, its schedule,
    or narrow how much of a session it reads, from the workspace the request is pinned to. Only a learner whose
    operator (if it has one) is an agent of this workspace or below, and only to such an agent. Enabling one that
    nobody active answers for makes you its accountable admin: its scheduled runs act for you. What it may learn
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
    changes = {k: v for k, v in changes.items() if v is not None or k == "operator"}
    try:
        cap = registry.update(cap, by=user, **changes)
    except registry.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return _cap_out(cap)

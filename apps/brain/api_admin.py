"""`/api/brain/…`: who learns (capabilities, registered by a canopy admin), who manages whom (set by a
workspace owner), and what was produced (read by the audience its class allows)."""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.http import HttpRequest
from ninja import Router, Status

from apps.agents.models import Agent, AgentProject
from apps.agents.services.projects import may_read_project
from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.harness import acting_person
from apps.tokens import kinds
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.gates import membership_or_404
from apps.workspaces.gates import require as require_role

from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session
from apps.common import redact

from . import cascade, outputs, registry, reporting, runs, schedule
from .api_state import the_person
from .models import PROJECT, SYSTEM, BrainCapability, OutputLineage, SourceLink
from .schemas import (
    BrainOutputOut,
    CapabilityIn,
    CapabilityOut,
    CapabilityPatch,
    BrainRunState,
    ProjectSessionOut,
    ReportingLineIn,
    ReportingLineOut,
    RunNowIn,
)

router = Router(auth=session_auth, tags=["brain"])


def _cap_out(c: BrainCapability) -> dict:
    return {"slug": c.slug, "name": c.name, "description": c.description, "purpose_class": c.purpose_class,
            "subject_type": c.subject_type, "purposes": c.purposes, "source_kinds": c.source_kinds,
            "output_kinds": c.output_kinds, "audience": c.audience,
            "operator": c.operator.slug if c.operator_id else "", "entry": c.entry, "schedule": c.schedule,
            "enabled": c.enabled, "scope": c.scope}


def _superuser(request: HttpRequest) -> None:
    if not getattr(request.user, "is_superuser", False):
        raise ProblemError(403, "Only a canopy admin may register what learns", type_=TYPE_FORBIDDEN)


def _operator(slug: str):
    if not slug:
        return None
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=f"no agent {slug!r}")
    return agent


@router.get("/brain/capabilities/", response=list[CapabilityOut], summary="What learns from canopy's sessions")
@gate("authenticated")
def list_capabilities(request: HttpRequest) -> list[dict]:
    """Every registered capability: its class, the purposes it reads for and who sees what it produces.
    What a person agrees to when they agree to a class."""
    return [_cap_out(c) for c in BrainCapability.objects.select_related("operator").order_by("slug")]


@router.post("/brain/capabilities/", response={200: CapabilityOut, **refusals(403, 422)},
             summary="Register a learner (canopy admin)")
@gate("superuser", setting=True)
def register_capability(request: HttpRequest, payload: CapabilityIn) -> dict:
    """A canopy admin registers something that learns: one class, the purposes it declares and an audience
    no wider than the class allows (a wider one is a 422). It reads nothing until it is enabled, and then
    only what people agreed to."""
    _superuser(request)
    try:
        cap = registry.register(**{**payload.model_dump(), "operator": _operator(payload.operator),
                                   "registered_by": request.user})
    except registry.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return _cap_out(cap)


@router.patch("/brain/capabilities/{slug}/", response={200: CapabilityOut, **refusals(403, 404, 422)},
              summary="Change a learner (canopy admin)")
@gate("superuser", setting=True)
def patch_capability(request: HttpRequest, slug: str, payload: CapabilityPatch) -> dict:
    """Narrow its audience or its scope, change its purposes, enable or disable it. Its class never changes. Enabling one that
    nobody active is accountable for (a seeded learner) makes you its accountable admin: its runs act for you."""
    _superuser(request)
    cap = BrainCapability.objects.filter(slug=slug).first()
    if cap is None:
        raise ProblemError(404, "No such learner", type_=TYPE_NOT_FOUND)
    changes = payload.model_dump(exclude_unset=True)
    if "operator" in changes:
        changes["operator"] = _operator(changes["operator"] or "")
    try:
        cap = registry.update(cap, by=request.user, **changes)
    except registry.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return _cap_out(cap)


@router.post("/brain/capabilities/{slug}/runs/", response={200: BrainRunState, **refusals(403, 404, 422)},
             summary="Run a learner now (canopy admin)")
@gate("superuser", kind="turn")  # starts one confined learning run, accountable to the learner's admin
def run_brain_capability(request: HttpRequest, slug: str, payload: RunNowIn) -> dict:
    """Starts one run of the learner now, as its schedule would: a `system` learner over every session eligible for
    `system`, a `project` learner for the workspace project named by `project` (a handle in its operator's workspace
    tree). A canopy admin as a person only, never through an agent or MCP. Refused, with the reason, when learning reads
    are switched off, the learner is disabled or has no operator or entry, nobody active answers for it, or a run for
    the subject is still open. What it reads and writes is then on `GET /api/brain/state/runs`."""
    the_person(request)
    _superuser(request)
    cap = BrainCapability.objects.filter(slug=slug).select_related("operator").first()
    if cap is None:
        raise ProblemError(404, "No such learner", type_=TYPE_NOT_FOUND)
    subject = None
    if cap.purpose_class == PROJECT:
        if cap.operator_id is None:
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=f"{cap.slug} has no operator agent")
        subject = schedule.projects_of(cap).filter(handle=payload.project.strip().lower()).first()
        if subject is None:
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                               detail="name an active workspace project of the learner's operator workspace")
    elif cap.purpose_class != SYSTEM:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION,
                           detail=f"{cap.slug} learns about one person; its runs are not started here")
    why = runs.refusal(cap, subject)
    if why:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=why)
    try:
        run = runs.start(cap, subject=subject)
    except runs.StartRefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return {"id": run.pk, "learner": cap.slug, "purpose": run.purpose, "subject_type": run.subject_type,
            "subject": subject.handle if subject is not None else "system", "state": run.state,
            "created_at": run.created_at, "finished_at": None, "reads": 0, "sessions": [], "hidden": 0}


# ---- who manages whom -----------------------------------------------------------------------------
# PERSON-ONLY, like `/api/me/learning/`: who manages whom decides who reads a person's learning outputs, so
# only a person as themselves sets, ends or lists it (`acting_person.refusal`), and none is an MCP tool
# (`apps/mcp/api_tools.py` EXCLUDED).

def _person_only(request: HttpRequest) -> None:
    if acting_person.refusal(request) is not None:
        raise ProblemError(403, "Only a person, as themselves, may do this", type_=TYPE_FORBIDDEN)


def _line_out(line) -> dict:
    name = lambda u: u.get_full_name() or u.email or u.username  # noqa: E731
    return {"person_id": line.person_id, "person": name(line.person), "manager_id": line.manager_id,
            "manager": name(line.manager), "since": line.created_at}


@router.get("/brain/workspaces/{slug}/reporting/", response={200: list[ReportingLineOut], **refusals(403, 404)},
            summary="Who manages whom in this workspace")
@gate("member")
def list_reporting(request: HttpRequest, slug: str) -> list[dict]:
    """A workspace's owners see every line; anyone else sees the lines they are in, as the person or as
    the manager: so everyone knows who sees their learning outputs."""
    _person_only(request)
    m = membership_or_404(request.user, slug)
    lines = reporting.live(m.workspace)
    if not perms.role_allows(m.role, perms.OWN):
        lines = [x for x in lines if request.user.pk in (x.person_id, x.manager_id)]
    return [_line_out(x) for x in lines]


@router.put("/brain/workspaces/{slug}/reporting/", response={200: ReportingLineOut, **refusals(403, 404, 422)},
            summary="Set who manages someone (workspace owner)")
@gate("own", setting=True)
def set_reporting(request: HttpRequest, slug: str, payload: ReportingLineIn) -> dict:
    """Owner-only, by hand. The manager then reads the person's `individual` learning outputs (never their
    transcripts). Both must be members; anyone else, with an account or without, is the same "no such member"."""
    _person_only(request)
    m = require_role(request.user, slug, perms.OWN)
    users = get_user_model().objects
    person, manager = (users.filter(email__iexact=payload.person.strip()).first(),
                       users.filter(email__iexact=payload.manager.strip()).first())
    if any(u is None or not wsvc.is_member(u, m.workspace.slug) for u in (person, manager)):
        raise ProblemError(404, "No such member", type_=TYPE_NOT_FOUND)
    try:
        line = reporting.set_manager(m.workspace, person=person, manager=manager, by=request.user)
    except reporting.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return _line_out(line)


@router.delete("/brain/workspaces/{slug}/reporting/{person_id}/", response={204: None, **refusals(403, 404)},
               summary="End someone's manager line (workspace owner)")
@gate("own", setting=True)
def end_reporting(request: HttpRequest, slug: str, person_id: int):
    _person_only(request)
    m = require_role(request.user, slug, perms.OWN)
    person = get_user_model().objects.filter(pk=person_id).first()
    if person is None or not reporting.end(m.workspace, person=person, by=request.user):
        raise ProblemError(404, "No such line", type_=TYPE_NOT_FOUND)
    return Status(204, None)


# ---- the sessions a project is tied to --------------------------------------------------------------

@router.get("/brain/workspaces/{slug}/projects/{handle}/sessions/",
            response={200: list[ProjectSessionOut], **refusals(404)},
            summary="The sessions tied to a project that you may read")
@gate("member")
def list_project_sessions(request: HttpRequest, slug: str, handle: str, tiers: str = "core,adjacent",
                          limit: int = 200) -> list[dict]:
    """Which sessions a project's history is made of: the ones tied to it (`tiers`: core, adjacent, candidate,
    comma separated) that YOU may read, by the same rule as every session read: your own, and a teammate's through
    the project leg once everyone in it agreed. Ids, titles and how each tie was made, never a word of a transcript.
    A project you may not read is not found."""
    m = membership_or_404(request.user, slug)
    project = AgentProject.objects.filter(workspace=m.workspace, handle=handle.lower()).first()
    if project is None or not may_read_project(request.user, project):
        raise ProblemError(404, "No such project", type_=TYPE_NOT_FOUND)
    wanted = [t for t in tiers.split(",") if t in ("core", "adjacent", "candidate")] or ["core", "adjacent"]
    links = (SourceLink.objects.filter(subject_type=SourceLink.PROJECT_SUBJECT, subject=str(project.pk),
                                       tier__in=wanted, source__session__isnull=False)
             .select_related("source__session").order_by("-source__session__created_at"))
    readable = access.readable_ids(request.user, [lnk.source.session_id for lnk in links[:2000]], brain=True)
    out = []
    for link in links[:2000]:
        s = link.source.session
        if s.pk in readable:
            out.append({"session_id": s.pk, "title": redact.redact_text(s.title or "")[0], "workspace": s.workspace_id,
                        "tier": link.tier, "how": link.how, "confidence": link.confidence, "started_at": s.created_at})
        if len(out) >= max(1, min(limit, 500)):
            break
    return out


# ---- what was produced ----------------------------------------------------------------------------

#: The credentials the manager leg opens for: the manager's own browser session or a PAT they minted by hand,
#: as HCP's governance acts (`contacts.hcp_api.GOVERNANCE_KINDS`). Never an OAuth sign-in (an AI client or
#: connector acting for them), a host site's or MCP host grant's delegated token, an act-as or caller token.
MANAGER_KINDS = frozenset({kinds.SESSION, kinds.PAT})


def manager_leg_open(request: HttpRequest) -> bool:
    """Is this request the manager themselves, so `outputs.visible_to` may open the manager leg? A person
    (`acting_person.is_person`), on one of `MANAGER_KINDS`, and not an MCP tool call: managers see a report's
    outputs as themselves, never through an AI client. Every other leg is unchanged."""
    kind = kinds.of_request(request)
    return (kind is not None and kind.name in MANAGER_KINDS and not getattr(request, "via_mcp", False)
            and not kinds.speaks_for_a_turn(request) and acting_person.is_person(request))


@router.get("/brain/outputs/", response=list[BrainOutputOut], summary="What learning produced, for you")
@gate("authenticated")  # visible_to: its class's audience, never wider
def list_outputs(request: HttpRequest, subject_type: str = "", subject: str = "", limit: int = 100) -> list[dict]:
    """The learning outputs you may read: about you, about people you coach or manage (outputs, never
    transcripts; the manager's only as yourself, never through an agent or a turn), about projects you may read, and
    canopy's system findings if you administer. Never a withdrawn or stale one. Newest first."""
    qs = (outputs.visible_to(request.user, as_person=manager_leg_open(request)).select_related("capability")
          .order_by("-created_at", "-pk"))
    if subject_type:
        qs = qs.filter(subject_type=subject_type)
    if subject:
        qs = qs.filter(subject=subject)
    rows = list(qs[:max(1, min(limit, 500))])
    cited, named = _cited(request.user, rows)
    return [{"id": o.pk, "learner": o.capability.slug, "kind": o.kind, "workspace": o.workspace_id,
             "subject_type": o.subject_type, "subject": o.subject, "title": o.title, "body": o.body,
             "state": o.state, "created_at": o.created_at,
             "sessions": [named[s] for s in cited.get(str(o.pk), []) if s in named],
             "hidden_sessions": sum(1 for s in cited.get(str(o.pk), []) if s not in named)} for o in rows]


def _cited(user, rows) -> tuple[dict, dict]:
    """Each output's cited sessions (`OutputLineage`), and of those the ones `user` reads through legs 1 to 6, by id
    with a scrubbed title. Three reads for a whole page."""
    cited: dict[str, list] = {}
    for oid, sid in (OutputLineage.objects.filter(output_kind=cascade.OUTPUT_KIND, output_id__in=[str(o.pk) for o in rows],
                                                  source__session_id__isnull=False)
                     .values_list("output_id", "source__session_id")):
        cited.setdefault(oid, []).append(sid)
    every = {s for ids in cited.values() for s in ids}
    readable = access.readable_ids(user, every)
    named = {s.pk: {"session_id": s.pk, "workspace": s.workspace_id, "title": redact.redact_text(s.title or "")[0]}
             for s in Session.objects.filter(pk__in=readable).only("pk", "workspace_id", "title")}
    return cited, named

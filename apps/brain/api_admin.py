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
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session
from apps.common import redact
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.gates import membership_or_404
from apps.workspaces.gates import require as require_role

from . import actor as actor_mod
from . import cascade, findings, outputs, person, registry, reporting, scope, targets
from .models import BrainCapability, BrainOutput, OutputLineage, SourceLink
from .schemas import (
    BrainOutputOut,
    BrainOutputStatusIn,
    CapabilityIn,
    CapabilityOut,
    CapabilityPatch,
    ProjectSessionOut,
    ReportingLineIn,
    ReportingLineOut,
)

router = Router(auth=session_auth, tags=["brain"])


def _cap_out(c: BrainCapability, *, shown: bool = True) -> dict:
    """A learner as the API shows it. `shown` False (`registry.shown_to`: another tenant's) leaves out who operates
    it and the command its runs start with."""
    return {"slug": c.slug, "name": c.name, "description": c.description, "purpose_class": c.purpose_class,
            "subject_type": c.subject_type, "purposes": c.purposes, "source_kinds": c.source_kinds,
            "output_kinds": c.output_kinds, "audience": c.audience,
            "operator": c.operator.slug if c.operator_id and shown else "", "entry": c.entry if shown else "",
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
    What a person agrees to when they agree to a class. Which agent operates one, and the command its runs start
    with, only inside the operator's own organisation (`registry.shown_to`)."""
    caps = list(BrainCapability.objects.select_related("operator").order_by("slug"))
    shown = registry.shown_to(request.user, caps)
    return [_cap_out(c, shown=c.pk in shown) for c in caps]


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
        why = targets.operator_refusal(cap, changes["operator"])
        if why:
            raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=why)
    try:
        cap = registry.update(cap, by=request.user, **changes)
    except registry.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    targets.follow(cap)
    return _cap_out(cap)


# ---- who manages whom -----------------------------------------------------------------------------
# PERSON-ONLY, like `/api/me/learning/`: who manages whom decides who reads a person's learning outputs, so
# only a person as themselves sets, ends or lists it (`person.refusal`), and none is an MCP tool
# (`apps/mcp/api_tools.py` EXCLUDED).

def _person_only(request: HttpRequest) -> None:
    person.the_person(request)


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

def manager_leg_open(request: HttpRequest) -> bool:
    """Is this request the manager themselves, so `outputs.visible_to` may open the manager leg? The one person
    question (`person.is_person`): their browser session or a PAT they minted by hand, never an MCP call, an OAuth
    client, a site's token, an agent or a turn. Every other leg is unchanged."""
    return person.is_person(request)


@router.get("/brain/outputs/", response=list[BrainOutputOut], summary="What learning produced, for you")
@gate("authenticated")  # visible_to: its class's audience, never wider
def list_outputs(request: HttpRequest, subject_type: str = "", subject: str = "", status: str = "",
                 limit: int = 100) -> list[dict]:
    """The learning outputs you may read: about you, about people you coach or manage (outputs, never
    transcripts; the manager's only as yourself, never through an agent or a turn), about projects you may read, and
    canopy's system findings where you own or administer the workspace a run was started for (or one above it),
    as yourself only. Pinned to a workspace (`/api/w/<ws>/…`), only that workspace's and the ones below it;
    unpinned, your one organisation's (the read rule every list follows). Never a withdrawn or stale one. `status`
    narrows to open, accepted, dismissed or done. Newest first."""
    as_person = manager_leg_open(request)
    acting = None if as_person else actor_mod.resolve(request)[0]
    acting = acting if acting is not None and acting.is_agent else None
    qs = (outputs.visible_to(request.user, as_person=as_person, acting=acting).select_related("capability", "status_by")
          .filter(workspace_id__in=scope.of_request(request)).order_by("-created_at", "-pk"))
    if subject_type:
        qs = qs.filter(subject_type=subject_type)
    if subject:
        qs = qs.filter(subject=subject)
    if status:
        qs = qs.filter(status=status)
    return _outputs_out(request.user, list(qs[:max(1, min(limit, 500))]), as_person=as_person, acting=acting)


def _outputs_out(user, rows, *, as_person: bool, acting=None) -> list[dict]:
    cited, named = _cited(user, rows)
    memo: dict = {}

    def may_set(o) -> bool:
        if acting is not None:
            return findings.actor_may_set(acting, o)
        return as_person and findings.acts_for(user, o, memo)

    return [{"id": o.pk, "learner": o.capability.slug, "kind": o.kind, "workspace": o.workspace_id,
             "subject_type": o.subject_type, "subject": o.subject, "title": o.title, "body": o.body,
             "state": o.state, "status": o.status, "status_at": o.status_at,
             "status_by": getattr(o.status_by, "email", "") or "", "key": o.key, "seen": o.seen,
             "last_seen_at": o.last_seen_at, "created_at": o.created_at,
             "may_set_status": may_set(o),
             "sessions": [named[s] for s in cited.get(str(o.pk), []) if s in named],
             "hidden_sessions": sum(1 for s in cited.get(str(o.pk), []) if s not in named)} for o in rows]


@router.post("/brain/outputs/{output_id}/status", response={200: BrainOutputOut, **refusals(403, 404, 422)},
             summary="Say where a finding stands: open, accepted, dismissed or done")
@gate("brain.manage", "self", kind="action")  # its audience's actors (findings.actor_may_set, actor.of)
def set_output_status(request: HttpRequest, output_id: int, payload: BrainOutputStatusIn) -> dict:
    """Your call on something learning produced: `accepted` (someone takes it up), `dismissed` (not worth acting on),
    `done` (acted on) or back to `open`. Yours only if you see it and act for its audience: a system finding's
    workspace owners and admins (or an ancestor's), a project output's members who may read the project, an output
    about you; an admin agent in its own turn, a system or project finding where it and its person manage learning.
    A repeat later folds into it while it is open or accepted. Every change is in its workspace's log."""
    actor = actor_mod.of(request)
    user = actor.user
    acting = actor if actor.is_agent else None
    out = (BrainOutput.objects.select_related("capability", "status_by")
           .filter(pk=output_id, state=BrainOutput.ACTIVE, workspace_id__in=wsvc.request_member_slugs(request))
           .first())
    if out is None or not outputs.visible_to(user, as_person=not actor.is_agent, acting=acting).filter(
            pk=out.pk).exists():
        raise ProblemError(404, "No such output", type_=TYPE_NOT_FOUND)
    if not findings.actor_may_set(actor, out):
        raise ProblemError(403, "Only the people who act for its audience set its status", type_=TYPE_FORBIDDEN)
    try:
        out = findings.set_status(out, payload.status, by=user, acting=actor.record() if actor.is_agent else None)
    except ValueError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return _outputs_out(user, [out], as_person=not actor.is_agent, acting=acting)[0]


def _cited(user, rows) -> tuple[dict, dict]:
    """Each output's cited sessions (`OutputLineage`), and of those the ones `user` reads through legs 1 to 6, by id
    with a scrubbed title. Three reads for a whole page."""
    cited: dict[str, list] = {}
    for oid, sid in (OutputLineage.objects.filter(output_kind=cascade.OUTPUT_KIND, output_id__in=[str(o.pk) for o in rows],
                                                  source__session_id__isnull=False, cited=True)
                     .values_list("output_id", "source__session_id")):
        cited.setdefault(oid, []).append(sid)
    every = {s for ids in cited.values() for s in ids}
    readable = access.readable_ids(user, every)
    named = {s.pk: {"session_id": s.pk, "workspace": s.workspace_id, "title": redact.redact_text(s.title or "")[0]}
             for s in Session.objects.filter(pk__in=readable).only("pk", "workspace_id", "title")}
    return cited, named

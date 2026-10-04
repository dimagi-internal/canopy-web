"""Run documents — a run of a KIND of work on an agent's PROJECT, with its state.

Mounted at ``/api/agent-runs/``. DDD is the first kind.

Why
---
A DDD run's state (iteration, findings, progress, gate decisions) lived only on
the disk of the runner that started it: no other machine could resume it, two
machines could mint the same run id (walkthroughs and reviews are grouped by that
string, so two packages merged), and a human's answer to a review reached one
disk. The generic home for "work toward an outcome" is the agent's PROJECT
(``agents.AgentProject``), and the generic record of one execution is
``AgentRun`` — so a DDD run is an ``AgentRun`` with ``kind="ddd"``, ``ext_id`` =
its run id, ``subject`` = its narrative, ``project`` = the project it serves, and
its state document versioned by ``state_version``.

The routes are cross-agent on purpose: a runner resuming ``supply-…-001`` knows
the run id, not which agent owns it; and "which project is this narrative for?"
is answered by the newest run with that ``subject``, across every agent the
caller can see.

Each accepted state write also mirrors the iteration onto the lifecycle model —
one ``iter-<n>`` step per iteration (the current one running until the run ends)
and a judge verdict carrying the iteration's score — so the existing run views
read a DDD run like any other run.

This is framework tier: it may not import a product app. Legacy run ids minted on
a runner's disk (which may already name walkthroughs) are kept clear of by the
client's ``min_seq`` hint, not by reading those tables here.
"""
from __future__ import annotations

import datetime as dt

from django.db import IntegrityError, transaction
from django.http import HttpRequest
from django.utils import timezone
from ninja import Router, Status
from ninja.errors import HttpError
from pydantic import Field

from apps.api.auth import session_auth
from apps.common.schemas import StrictModel

from .models import AgentRun, AgentRunStep, AgentRunVerdict

router = Router(auth=session_auth, tags=["agent-runs"])

RUNNING = "running"
_SLUG = r"^[a-z0-9][a-z0-9-]*$"


# ---- schemas ----------------------------------------------------------------


class RunDocCreateIn(StrictModel):
    agent: str = Field(min_length=1, max_length=100)
    # The agent project's ext_id ("P3"). Omit for a run that serves no project.
    project: str | None = Field(default=None, max_length=64)
    kind: str = Field(min_length=1, max_length=40, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    subject: str = Field(default="", max_length=200, pattern=r"^$|" + _SLUG[1:])
    label: str = Field(default="", max_length=300)
    # Adopt a run minted elsewhere under its own id (409 if taken). Omit to mint
    # ``<subject>-YYYY-MM-DD-NNN``.
    ext_id: str | None = Field(default=None, max_length=255, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    # Mint no lower than this sequence for today — the client's highest LOCAL id
    # + 1, so a server-minted id never collides with a run minted on disk before
    # runs lived here.
    min_seq: int = Field(default=1, ge=1)
    status: str = Field(default=RUNNING, max_length=40)
    summary: dict = Field(default_factory=dict)
    state: dict = Field(default_factory=dict)
    holder: str = Field(default="", max_length=200)
    session_link: str = Field(default="", max_length=500)


class RunDocStateIn(StrictModel):
    state: dict
    # The state_version the writer last read; a mismatch is a 409 (another runner
    # advanced the run) unless ``force``.
    base_version: int | None = None
    force: bool = False
    status: str | None = Field(default=None, max_length=40)
    current_step: str | None = Field(default=None, max_length=120)
    iteration: int | None = Field(default=None, ge=0)
    score: float | None = None
    summary: dict | None = None
    holder: str = Field(default="", max_length=200)


class RunDocProjectOut(StrictModel):
    ext_id: str
    name: str
    repo_slug: str
    status: str


class RunDocOut(StrictModel):
    ext_id: str
    id: str
    agent_slug: str
    project: RunDocProjectOut | None = None
    kind: str
    subject: str
    label: str
    status: str
    current_step: str
    summary: dict
    state_version: int
    holder: str
    holder_at: dt.datetime | None = None
    created_at: dt.datetime
    completed_at: dt.datetime | None = None


class RunDocDetailOut(RunDocOut):
    state: dict


class AgentProjectRefOut(StrictModel):
    agent_slug: str
    ext_id: str
    name: str
    outcome: str
    repo_slug: str
    status: str


# ---- helpers ----------------------------------------------------------------


def _visible_agents(request):
    from apps.agents.models import Agent
    from apps.workspaces import services as wsvc

    return Agent.objects.filter(workspace_id__in=wsvc.request_workspace_slugs(request))


def _agent_for_write(request, slug: str):
    """404 for an agent the caller cannot see; 403 below the AGENT_WORK tier
    (or the agent itself under its own login) — same gate as the run lifecycle."""
    from apps.workspaces import permissions as perms

    agent = _visible_agents(request).filter(slug=slug).first()
    if agent is None:
        raise HttpError(404, f"agent '{slug}' not found")
    if not (agent.user_id is not None and agent.user_id == request.user.pk) and not perms.can(
        request.user, agent.workspace_id, perms.AGENT_WORK
    ):
        raise HttpError(403, "writing an agent's runs requires the editor role or above")
    return agent


def _doc_or_404(request, ext_id: str) -> AgentRun:
    run = (
        AgentRun.objects.select_related("agent", "project")
        .filter(ext_id=ext_id, agent__in=_visible_agents(request))
        .first()
    )
    if run is None:
        raise HttpError(404, f"run '{ext_id}' not found")
    return run


def _project_out(p) -> RunDocProjectOut | None:
    if p is None:
        return None
    return RunDocProjectOut(ext_id=p.ext_id, name=p.name, repo_slug=p.repo_slug, status=p.status)


def _out(run: AgentRun, *, detail: bool = False):
    data = {
        "ext_id": run.ext_id or "",
        "id": str(run.pk),
        "agent_slug": run.agent.slug,
        "project": _project_out(run.project),
        "kind": run.kind,
        "subject": run.subject,
        "label": run.label,
        "status": run.status or RUNNING,
        "current_step": run.current_step,
        "summary": run.summary or {},
        "state_version": run.state_version,
        "holder": run.holder,
        "holder_at": run.holder_at,
        "created_at": run.created_at,
        "completed_at": run.completed_at,
    }
    return RunDocDetailOut(**data, state=run.state or {}) if detail else RunDocOut(**data)


def next_ext_id(subject: str, *, min_seq: int = 1, today: dt.date | None = None) -> str:
    """``<subject>-YYYY-MM-DD-NNN`` past every run id already minted for that day."""
    day = (today or timezone.now().date()).strftime("%Y-%m-%d")
    prefix = f"{subject or 'run'}-{day}-"
    nums = [
        int(i[len(prefix):])
        for i in AgentRun.objects.filter(ext_id__startswith=prefix).values_list("ext_id", flat=True)
        if i and i[len(prefix):].isdigit()
    ]
    return f"{prefix}{max([min_seq - 1, *nums]) + 1:03d}"


def _mirror_iteration(run: AgentRun, iteration: int | None, score: float | None) -> None:
    """One ``iter-<n>`` step per iteration; the current one running until the run
    ends; its judge verdict carries the iteration's score."""
    if iteration is None:
        return
    now = timezone.now()
    live = (run.status or RUNNING) == RUNNING
    for n in range(iteration + 1):
        step, created = AgentRunStep.objects.get_or_create(
            run=run, key=f"iter-{n}",
            defaults={"ordinal": n, "title": f"Iteration {n}", "started_at": now},
        )
        want = AgentRunStep.RUNNING if (n == iteration and live) else AgentRunStep.COMPLETE
        if step.status != want:
            step.status = want
            if want == AgentRunStep.COMPLETE and step.completed_at is None:
                step.completed_at = now
            step.save(update_fields=["status", "completed_at"])
    if score is not None:
        step = AgentRunStep.objects.get(run=run, key=f"iter-{iteration}")
        AgentRunVerdict.objects.filter(step=step, kind=AgentRunVerdict.JUDGE).delete()
        AgentRunVerdict.objects.create(
            step=step, kind=AgentRunVerdict.JUDGE, score=score,
            criteria=(run.summary or {}).get("progress") or {}, evaluated_at=now,
        )
    run.current_step = f"iter-{iteration}"


def _stamp_terminal(run: AgentRun) -> None:
    if (run.status or RUNNING) != RUNNING:
        run.completed_at = run.completed_at or timezone.now()
    else:
        run.completed_at = None


# ---- routes -----------------------------------------------------------------


@router.get("/", response=list[RunDocOut], summary="List run documents across agents")
def list_run_docs(
    request: HttpRequest,
    kind: str | None = None,
    subject: str | None = None,
    agent: str | None = None,
    repo_slug: str | None = None,
    active: bool | None = None,
    limit: int = 50,
) -> list[RunDocOut]:
    """Newest first, across every agent the caller can see. ``active=true`` =
    still running — what a runner resumes. ``repo_slug`` filters by the run's
    project's repo."""
    qs = AgentRun.objects.select_related("agent", "project").filter(
        agent__in=_visible_agents(request)
    ).exclude(ext_id__isnull=True)
    if kind:
        qs = qs.filter(kind=kind)
    if subject:
        qs = qs.filter(subject=subject)
    if agent:
        qs = qs.filter(agent__slug=agent)
    if repo_slug:
        qs = qs.filter(project__repo_slug=repo_slug)
    if active is True:
        qs = qs.filter(status__in=[RUNNING, ""])
    elif active is False:
        qs = qs.exclude(status__in=[RUNNING, ""])
    return [_out(r) for r in qs.order_by("-created_at")[: max(1, min(limit, 500))]]


@router.get("/projects/", response=list[AgentProjectRefOut], summary="Agent projects across agents")
def list_agent_projects(
    request: HttpRequest, repo_slug: str | None = None, status: str = "active"
) -> list[AgentProjectRefOut]:
    """Every visible agent's projects (optionally touching ``repo_slug``) — the
    choices a runner offers when a narrative is not yet bound to a project."""
    from apps.agents.models import AgentProject

    qs = AgentProject.objects.select_related("agent").filter(agent__in=_visible_agents(request))
    if repo_slug:
        qs = qs.filter(repo_slug=repo_slug)
    if status:
        qs = qs.filter(status=status)
    return [
        AgentProjectRefOut(
            agent_slug=p.agent.slug, ext_id=p.ext_id, name=p.name,
            outcome=p.outcome, repo_slug=p.repo_slug, status=p.status,
        )
        for p in qs.order_by("agent__slug", "-updated_at")
    ]


@router.post("/", response={201: RunDocDetailOut}, summary="Start (or adopt) a run document")
def create_run_doc(request: HttpRequest, payload: RunDocCreateIn) -> Status:
    from apps.agents.models import AgentProject

    agent = _agent_for_write(request, payload.agent)
    project = None
    if payload.project:
        project = AgentProject.objects.filter(agent=agent, ext_id=payload.project).first()
        if project is None:
            raise HttpError(404, f"project '{payload.project}' not found for agent '{agent.slug}'")
    for _attempt in range(5):
        ext_id = payload.ext_id or next_ext_id(payload.subject, min_seq=payload.min_seq)
        try:
            with transaction.atomic():
                run = AgentRun(
                    agent=agent, project=project, kind=payload.kind, ext_id=ext_id,
                    subject=payload.subject, label=payload.label or payload.subject,
                    mode=AgentRun.AUTO, status=payload.status or RUNNING,
                    summary=payload.summary, state=payload.state,
                    state_version=1 if payload.state else 0,
                    holder=payload.holder, holder_at=timezone.now() if payload.holder else None,
                    session_link=payload.session_link,
                )
                _stamp_terminal(run)
                run.save()
                _mirror_iteration(run, int(payload.state.get("iteration") or 0), None)
                run.save(update_fields=["current_step"])
        except IntegrityError:
            if payload.ext_id:
                raise HttpError(409, f"run '{payload.ext_id}' already exists")
            continue  # raced another minter for the same sequence
        return Status(201, _out(run, detail=True))
    raise HttpError(409, "could not mint a run id after five attempts; retry")


@router.get("/{ext_id}/", response=RunDocDetailOut, summary="A run document with its state")
def get_run_doc(request: HttpRequest, ext_id: str) -> RunDocDetailOut:
    return _out(_doc_or_404(request, ext_id), detail=True)


@router.put("/{ext_id}/state/", response=RunDocDetailOut, summary="Write a run document's state")
def put_run_doc_state(request: HttpRequest, ext_id: str, payload: RunDocStateIn) -> RunDocDetailOut:
    run = _doc_or_404(request, ext_id)
    _agent_for_write(request, run.agent.slug)
    with transaction.atomic():
        run = AgentRun.objects.select_for_update().select_related("agent", "project").get(pk=run.pk)
        if not payload.force and payload.base_version is not None and payload.base_version != run.state_version:
            raise HttpError(
                409,
                f"run '{ext_id}' is at state_version {run.state_version} (last written by "
                f"{run.holder or 'unknown'}), not {payload.base_version}",
            )
        run.state = payload.state
        run.state_version += 1
        if payload.status is not None:
            run.status = payload.status
        if payload.summary is not None:
            run.summary = payload.summary
        if payload.holder:
            run.holder = payload.holder
            run.holder_at = timezone.now()
        _stamp_terminal(run)
        _mirror_iteration(run, payload.iteration, payload.score)
        if payload.current_step is not None:
            run.current_step = payload.current_step
        run.save()
    return _out(run, detail=True)

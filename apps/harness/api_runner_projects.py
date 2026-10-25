"""/api/harness/runners/{id}/projects — add a repo to a laptop runner as an emdash
project, from canopy-web, and watch it land (`runner_projects`).

Registered on the harness router (`api.router`), like api_runners."""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Status
from ninja.errors import HttpError

from apps.agents.gates import agent_for_read
from apps.api.route_gates import gate

from . import runner_projects
from . import runners as harness_runners
from .api import router, runner_admin_or_404, runner_or_404, runner_read_q
from .models import Runner, RunnerProjectRequest
from .schemas import (
    RunnerProjectRequestIn,
    RunnerProjectRequestOut,
    RunnerProjectResultIn,
    RunnerProjectsOut,
)

#: Published operationIds keep the harness label (an operationId is an MCP tool name).
OPERATION_LABEL = "harness"


def _readable_runner(request: HttpRequest, runner_id: uuid.UUID) -> Runner:
    runner = (Runner.objects.exclude(status=Runner.RETIRED).filter(runner_read_q(request))
              .filter(pk=runner_id).first())
    if runner is None:
        raise HttpError(404, "runner not found")
    return runner


def _out(request: HttpRequest, runner: Runner) -> dict:
    # A dict of rows, validated once by the response schema: its resolvers read
    # the ORM rows, not an already-built schema.
    rows = (RunnerProjectRequest.objects.filter(runner=runner)
            .select_related("agent", "requested_by").order_by("name"))
    return {
        "projects": sorted(runner.observed_projects()),
        "requests": list(rows),
        "can_add": runner_projects.can_add_projects(runner)
        and harness_runners.can_administer_runner(request.user, runner),
    }


@router.get("/runners/{runner_id}/projects", response=RunnerProjectsOut,
            summary="The emdash projects a laptop runner holds, and repos asked of it")
@gate("member")  # runner_read_q: anyone who can see the runner
def list_runner_projects(request: HttpRequest, runner_id: uuid.UUID):
    """The repos this runner reports as emdash projects, every ask to add one (with
    `pending`/`added`/`failed` and the box's reason), and whether you may ask."""
    return _out(request, _readable_runner(request, runner_id))


@router.post("/runners/{runner_id}/projects", response={201: RunnerProjectRequestOut},
             summary="Ask a laptop runner to add a repo as an emdash project")
@gate("runner-admin", kind="action")
def add_runner_project(request: HttpRequest, runner_id: uuid.UUID, payload: RunnerProjectRequestIn):
    """Ask this laptop to clone a repo and add it to emdash, on its own: name an
    `agent` (its repo, as the emdash project named after it — what routing needs
    before the laptop takes that agent's work) or give `repo_url` and an optional
    `name`. The box reads the ask off its next heartbeat, clones the repo under
    its own account, opens it in emdash's Add Project dialog, checks emdash holds
    it, and reports back; poll `GET …/projects` for the outcome. Asking again for
    the same name re-arms the ask. Its owner or a runner administrator only, and
    only a laptop (emdash) runner: a cloud box clones its agents itself."""
    runner = runner_admin_or_404(request, runner_id)
    agent = None
    repo_url, name = payload.repo_url, payload.name
    if payload.agent:
        agent = agent_for_read(request, payload.agent.strip())
        if not agent.repo_url:
            raise HttpError(422, f"{agent.name} has no repo set; set it on the agent's Settings first")
        repo_url, name = agent.repo_url, agent.slug
    try:
        row = runner_projects.request_project(runner, repo_url=repo_url, name=name, agent=agent,
                                              user=request.user)
    except runner_projects.ProjectRequestError as exc:
        raise HttpError(422, str(exc)) from exc
    return Status(201, row)


@router.delete("/runners/{runner_id}/projects/{request_id}", response={204: None},
               summary="Withdraw or clear an ask to add a repo")
@gate("runner-admin", kind="action")
def delete_runner_project_request(request: HttpRequest, runner_id: uuid.UUID, request_id: uuid.UUID):
    """Forget one ask — a pending one is withdrawn (the box stops being asked), a
    finished one is cleared from the list. Never touches emdash: a project the
    box already added stays."""
    runner = runner_admin_or_404(request, runner_id)
    deleted, _ = RunnerProjectRequest.objects.filter(runner=runner, pk=request_id).delete()
    if not deleted:
        raise HttpError(404, "no such request on this runner")
    return Status(204, None)


@router.post("/runners/{runner_id}/projects/{request_id}/result", response=RunnerProjectRequestOut,
             summary="The runner reports how adding a repo went")
@gate("runner", kind="runner-protocol")
def report_runner_project_result(request: HttpRequest, runner_id: uuid.UUID, request_id: uuid.UUID,
                                 payload: RunnerProjectResultIn):
    """The box's word on one ask (`ok`, and its reason in `detail`). A success
    counts once emdash's project shows in the box's own report; a failure marks
    only this ask failed, never the box."""
    runner = runner_or_404(request, runner_id)
    row = runner_projects.report_result(runner, request_id, ok=payload.ok, detail=payload.detail)
    if row is None:
        raise HttpError(404, "no such request on this runner")
    return RunnerProjectRequest.objects.select_related("agent", "requested_by").get(pk=row.pk)

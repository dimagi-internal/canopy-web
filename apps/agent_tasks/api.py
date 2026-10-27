"""`/api/agent-tasks/` — call an agent inline and get a structured result back.

A caller (a page's backend, a script on a PAT) creates a task naming an agent, a
task name, its input and the JSON Schema of the answer it needs; one turn is
queued for the agent, and the caller long-polls the task until it is done,
failed or cancelled. The agent reads the task and submits its result here too.
Every route is also an MCP tool, which is how the agent reaches them.

Who may call (docs/architecture/agent-tasks.md):

- create — whoever may enqueue a turn for the agent (`agent.work`: the same gates
  `POST /api/harness/turns/` runs, in the same order);
- read — its creator, the agent's admins, and the agent's own login (its PAT or
  the login canopy issued for one turn); anyone else gets a 404;
- result — the agent's own login only, while the task is open;
- cancel — its creator or the agent's admins.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Query, Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.errors import TYPE_CONFLICT, TYPE_PAYLOAD_TOO_LARGE, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.harness import clis, turn_gates
from apps.harness.schemas import TurnIn
from apps.workspaces import permissions as perms

from . import services
from .models import AgentTask
from .schemas import AgentTaskCallIn, AgentTaskCallOut, AgentTaskResultIn

router = Router(auth=session_auth, tags=["agent-tasks"])

AGENT, ADMIN, CREATOR = "agent", "admin", "creator"


def _role(request: HttpRequest, task: AgentTask) -> str | None:
    user = request.user
    agent = task.agent
    if agent.is_own_login(user):
        return AGENT
    if agent.is_admin(user):
        return ADMIN
    if (task.created_by_id is not None and task.created_by_id == getattr(user, "pk", None)
            and perms.can(user, agent.workspace_id, perms.READ)):
        return CREATOR
    return None


def _task_or_404(request: HttpRequest, task_id: uuid.UUID) -> tuple[AgentTask, str]:
    task = (AgentTask.objects.select_related("agent", "agent__workspace", "turn")
            .filter(pk=task_id).first())
    role = _role(request, task) if task is not None else None
    if role is None:
        raise HttpError(404, f"agent task {task_id} not found")
    return task, role


def _out(task: AgentTask, role: str) -> AgentTaskCallOut:
    return AgentTaskCallOut(
        id=str(task.pk), agent=task.agent.slug, name=task.name, status=task.status,
        result=task.result, error=task.error, nudges=task.nudges,
        turn_id=str(task.turn_id) if task.turn_id else None, timeout_s=task.timeout_s,
        deadline_at=task.deadline_at, created_at=task.created_at, finished_at=task.finished_at,
        instructions=task.instructions, output_schema=task.output_schema,
        input=task.input if role == AGENT else None,
    )


def _too_large(what: str, limit: int) -> ProblemError:
    return ProblemError(413, "Payload too large", type_=TYPE_PAYLOAD_TOO_LARGE,
                        detail=f"{what} is larger than {limit} bytes as JSON")


@router.post("/", response={201: AgentTaskCallOut}, summary="Give an agent a task and get a structured result")
@gate("agent.work", kind="turn")  # the same gates as enqueue_turn
def create_agent_task(request: HttpRequest, payload: AgentTaskCallIn):
    """Ask an agent to do one task and answer with JSON that validates against
    `output_schema`. Queues one turn for the agent; poll `get_agent_task` (with
    `wait_s`) until `status` is done (read `result`), failed or cancelled (read
    `error`). The task is done ONLY when the agent submits a valid result — a turn
    that ends without one is followed up, and the task fails after `timeout_s`
    (30–3600, default 900). `name` (e.g. `gmail-triage.review`) tells the agent which
    of its skills to use. Read-and-answer only: the agent is told to take no
    outbound action. `output_schema` must be a valid JSON Schema (422); `input` is at
    most 1 MiB as JSON (413). Who may create one: whoever may enqueue a turn for
    the agent."""
    # A TurnIn for the gates enqueue_turn runs, so this door can never be wider.
    turn_in = TurnIn(agent_slug=payload.agent, origin="api", idempotency_key="agent-task",
                     routing=payload.routing, parent=payload.parent)
    turn_gates.check_shape(turn_in)
    agent, workspace = turn_gates.resolve_target(request, turn_in)
    if why := services.schema_problem(payload.output_schema):
        raise ProblemError(422, "Invalid output_schema", type_=TYPE_VALIDATION, detail=why)
    if services.json_size(payload.output_schema) > services.MAX_SCHEMA_BYTES:
        raise _too_large("output_schema", services.MAX_SCHEMA_BYTES)
    if services.json_size(payload.input) > services.MAX_INPUT_BYTES:
        raise _too_large("input", services.MAX_INPUT_BYTES)
    initiator = turn_gates.check_asker(request, turn_in, agent)
    if (refused := clis.refusal(agent, "")) is not None:
        raise HttpError(422, refused)
    task = services.create(
        agent=agent, workspace=workspace, request=request, name=payload.name,
        instructions=payload.instructions, input=payload.input, output_schema=payload.output_schema,
        routing=payload.routing, timeout_s=payload.timeout_s, parent=payload.parent, initiator=initiator,
    )
    return Status(201, _out(task, CREATOR))


@router.get("/{task_id}", response=AgentTaskCallOut, summary="Read an agent task (long-poll with wait_s)")
@gate("self", "agent-admin", "agent-login")  # its creator, the agent's admins, the agent itself
def get_agent_task(request: HttpRequest, task_id: uuid.UUID, wait_s: int = Query(0, ge=0, le=20)):
    """An agent task's status, its `result` once done and `error` once failed or
    cancelled. `wait_s` (0–20) long-polls: the request returns as soon as the status
    changes from the one it had, else after `wait_s`.

    Called by the agent itself (its own login), this is how it READS its task:
    `instructions`, `input` and `output_schema`, and the task becomes `running`. It
    then answers with `submit_agent_task_result`. Readable by its creator, the
    agent's admins and the agent's own login; 404 for anyone else."""
    task, role = _task_or_404(request, task_id)
    if role == AGENT:
        services.expire(task)
        services.mark_running(task)
    return _out(services.wait(task, wait_s), role)


@router.post("/{task_id}/result", response=AgentTaskCallOut, summary="Submit an agent task's result")
@gate("agent-login", kind="turn")
def submit_agent_task_result(request: HttpRequest, task_id: uuid.UUID, payload: AgentTaskResultIn):
    """The agent's answer to its task — the ONLY way a task completes. `result` must
    validate against the task's `output_schema`: if it does not, 422 with `errors`, a
    list of `{path, message}` (`path` a JSON Pointer into `result`, "" for the whole
    of it), and the task stays open — fix every path and submit again. Valid → the
    task is `done`. Only the agent's own login, while the task is queued or running
    (409 once it has ended); at most 1 MiB as JSON (413)."""
    task, role = _task_or_404(request, task_id)
    if role != AGENT:
        raise HttpError(403, f"only {task.agent.slug} itself (its own login) submits this task's result")
    services.expire(task)
    if task.status not in AgentTask.OPEN:
        raise ProblemError(409, "Agent task has ended", type_=TYPE_CONFLICT,
                           detail=f"task is {task.status}" + (f": {task.error}" if task.error else ""))
    if services.json_size(payload.result) > services.MAX_RESULT_BYTES:
        raise _too_large("result", services.MAX_RESULT_BYTES)
    errors = services.submit(task, payload.result)
    if errors:
        listed = "; ".join(f"{e['path'] or '(root)'}: {e['message']}" for e in errors[:5])
        raise ProblemError(
            422, "Result does not match output_schema", type_=TYPE_VALIDATION,
            detail=f"{len(errors)} problem(s), the task is still open — fix each and submit again: {listed}",
            members={"errors": errors})
    if task.status != AgentTask.DONE:
        raise ProblemError(409, "Agent task has ended", type_=TYPE_CONFLICT,
                           detail=f"task is {task.status}" + (f": {task.error}" if task.error else ""))
    return _out(task, role)


@router.post("/{task_id}/cancel", response=AgentTaskCallOut, summary="Cancel an agent task")
@gate("self", "agent-admin", kind="action")  # its creator or the agent's admins
def cancel_agent_task(request: HttpRequest, task_id: uuid.UUID):
    """Stop waiting for an agent task: an open task becomes `cancelled` and its turn,
    if no runner has started it, is cancelled too (a running one finishes on its own;
    its result is then refused). Idempotent: an ended task is returned as it is. For
    its creator or the agent's admins."""
    task, role = _task_or_404(request, task_id)
    if role == AGENT:
        raise HttpError(403, "an agent does not cancel its own task; submit a result instead")
    return _out(services.cancel(task) if task.status in AgentTask.OPEN else task, role)

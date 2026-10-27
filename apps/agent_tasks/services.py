"""What an agent task does: its turn's prompt, checking a result, and every way it ends.

The routes (`api.py`) decide WHO may call; this module decides what happens. The
completion rule lives here once: `submit` is the only path to DONE, and only for a
result that validates against the task's `output_schema`.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import time
import uuid

import jsonschema
from django.db import transaction
from django.utils import timezone

from apps.agents.models import AgentSkill
from apps.harness import initiator as who
from apps.harness import services as harness
from apps.harness import turn_agent_tokens
from apps.harness.models import Turn

from .models import AgentTask

logger = logging.getLogger(__name__)

#: The largest `input` (and `result`), as JSON.
MAX_INPUT_BYTES = 1024 * 1024
MAX_RESULT_BYTES = 1024 * 1024
MAX_SCHEMA_BYTES = 64 * 1024
#: Follow-up turns for a task whose turn ended without a result, before it fails.
MAX_NUDGES = 2
#: How many schema errors a refused result lists.
MAX_ERRORS = 50
#: The long-poll's ceiling and how often it re-reads the row.
MAX_WAIT_S = 20
POLL_S = 1.0
#: How often the off-request clock fails tasks past their deadline.
EXPIRE_EVERY_S = 60

#: The `idempotency_key` of a task's turns: the first, then each nudge.
KEY_PREFIX = "agent-task:"

ENDED_WITHOUT_RESULT = "the agent ended without a result"

_sleep = time.sleep
_monotonic = time.monotonic


def json_size(value) -> int:
    return len(json.dumps(value, separators=(",", ":"), default=str).encode())


def schema_problem(schema) -> str:
    """Why `schema` is not a usable JSON Schema, or "" when it is."""
    if not isinstance(schema, dict):
        return "output_schema must be a JSON Schema object"
    try:
        jsonschema.validators.validator_for(schema).check_schema(schema)
    except jsonschema.SchemaError as exc:
        where = "/".join(str(p) for p in exc.absolute_path)
        return f"output_schema is not a valid JSON Schema: {exc.message}" + (f" (at /{where})" if where else "")
    return ""


def result_errors(task: AgentTask, result) -> list[dict]:
    """Every place `result` breaks the task's schema, as `{path, message}` — `path` a
    JSON Pointer into the result ("" for the whole of it). Empty when it validates."""
    cls = jsonschema.validators.validator_for(task.output_schema)
    validator = cls(task.output_schema, format_checker=cls.FORMAT_CHECKER)
    errors = sorted(validator.iter_errors(result), key=lambda e: [str(p) for p in e.absolute_path])
    return [{"path": "".join(f"/{p}" for p in e.absolute_path), "message": e.message}
            for e in errors[:MAX_ERRORS]]


def skill_for(agent, name: str) -> str:
    """The agent's skill this task names (`gmail-triage.review` → `gmail-triage`), or ""."""
    names = {name, name.split(".", 1)[0]}
    skill = AgentSkill.objects.filter(agent=agent, name__in=names).order_by("name").first()
    return skill.name if skill is not None else ""


def prompt(task: AgentTask, skill: str = "") -> str:
    """The first turn's prompt: short and fixed. The task itself is read with the tool,
    never pasted here — the input can be a megabyte, and the prompt is a transcript row."""
    how = f", using your `{skill}` skill" if skill else ""
    return (
        f"Agent task {task.pk} ({task.name}) is waiting for your answer: a caller asked for it "
        "inline and is waiting for a structured result.\n\n"
        f"1. Read it: call `get_agent_task` with task_id {task.pk} (its instructions, input and "
        "output_schema).\n"
        f"2. Do it{how}.\n"
        f"3. Finish ONLY by calling `submit_agent_task_result` with task_id {task.pk} and a `result` "
        "that validates against output_schema. A refusal (422) lists each path that is wrong: fix "
        "them and submit again. Ending your turn without a submitted result does not complete the "
        "task.\n\n"
        "This task is read-and-answer only: take no outbound action (send no mail or message, "
        "publish nothing, change nothing in another system)."
    )


def nudge_prompt(task: AgentTask) -> str:
    """A follow-up turn's prompt. A fresh turn starts a fresh session, so it carries
    the task's own instructions again after the nudge."""
    return (f"You ended without submitting the result for agent task {task.pk}. "
            "Call submit_agent_task_result now.\n\n"
            + prompt(task, skill_for(task.agent, task.name)))


def create(*, agent, workspace, request, name: str, instructions: str, input, output_schema: dict,
           routing: str, timeout_s: int, parent, initiator) -> AgentTask:
    """The task and its one turn, in one transaction. The caller has already passed the
    turn gates (`api.create_agent_task`). A turn the agent's rules refuse outright (written
    CANCELLED by `enqueue_turn`) fails the task with the turn's own reason."""
    now = timezone.now()
    with transaction.atomic():
        task = AgentTask.objects.create(
            agent=agent, name=name, instructions=instructions, input=input, output_schema=output_schema,
            routing=routing, timeout_s=timeout_s, deadline_at=now + dt.timedelta(seconds=timeout_s),
            created_by=request.user if getattr(request.user, "is_authenticated", False) else None,
        )
        turn, _created = harness.enqueue_turn(
            agent=agent, workspace=workspace, origin=Turn.ORIGIN_API,
            idempotency_key=f"{KEY_PREFIX}{task.pk}",
            prompt=prompt(task, skill_for(agent, name)),
            origin_ref={"agent_task": str(task.pk)},
            routing=routing,
            enqueued_by=request.user,
            initiator=initiator,
            parent=parent,
        )
        task.turn = turn
        fields = ["turn", "updated_at"]
        if turn.status in Turn.TERMINAL:
            task.status, task.finished_at = AgentTask.FAILED, now
            task.error = f"its turn was not run: {turn.result_note or turn.status}"[:2000]
            fields += ["status", "finished_at", "error"]
        task.save(update_fields=fields)
    return task


def _end(task: AgentTask, status: str, *, error: str = "", result=None) -> bool:
    """Move an OPEN task to a terminal status; False when something else ended it first."""
    now = timezone.now()
    fields = {"status": status, "finished_at": now, "updated_at": now, "error": error[:2000]}
    if status == AgentTask.DONE:
        fields["result"] = result
    moved = AgentTask.objects.filter(pk=task.pk, status__in=AgentTask.OPEN).update(**fields)
    task.refresh_from_db()
    return bool(moved)


def _drop_queued_turn(task: AgentTask) -> None:
    """A task that ended no longer needs a turn nobody has started."""
    if task.turn_id and task.turn.status == Turn.QUEUED:
        try:
            harness.cancel_queued_turn(task.turn)
        except Exception:  # noqa: BLE001 — the task's own end must still stand
            logger.exception("agent task %s: could not cancel its queued turn", task.pk)


def submit(task: AgentTask, result) -> list[dict]:
    """Accept `result` if it validates: the task is DONE. Otherwise the errors, and the
    task is unchanged (still open, so the agent can fix them and submit again)."""
    errors = result_errors(task, result)
    if errors:
        return errors
    _end(task, AgentTask.DONE, result=result)
    return []


def cancel(task: AgentTask) -> AgentTask:
    if _end(task, AgentTask.CANCELLED, error="cancelled"):
        _drop_queued_turn(task)
    return task


def expire(task: AgentTask, *, now=None) -> bool:
    """Fail an open task past its deadline (lazily on every read, and from the tick)."""
    now = now or timezone.now()
    if task.status not in AgentTask.OPEN or task.deadline_at > now:
        return False
    if _end(task, AgentTask.FAILED, error=f"timed out after {task.timeout_s}s without a result"):
        _drop_queued_turn(task)
        return True
    return False


def expire_overdue() -> int:
    """The tick: every open task past its deadline fails."""
    now = timezone.now()
    n = 0
    for task in (AgentTask.objects.select_related("turn")
                 .filter(status__in=AgentTask.OPEN, deadline_at__lte=now)[:500]):
        n += expire(task, now=now)
    return n


def mark_running(task: AgentTask) -> None:
    """The agent read its task: it is being worked."""
    if task.status == AgentTask.QUEUED:
        AgentTask.objects.filter(pk=task.pk, status=AgentTask.QUEUED).update(
            status=AgentTask.RUNNING, updated_at=timezone.now())
        task.refresh_from_db()


def wait(task: AgentTask, wait_s: int) -> AgentTask:
    """The long-poll: returns as soon as the task leaves the status it had on
    arrival, else after `wait_s` (≤ MAX_WAIT_S), re-reading the row about once a second."""
    expire(task)
    wait_s = max(0, min(int(wait_s or 0), MAX_WAIT_S))
    if not wait_s or task.status not in AgentTask.OPEN:
        return task
    entered = task.status
    deadline = _monotonic() + wait_s
    while task.status == entered and _monotonic() < deadline:
        _sleep(POLL_S)
        task.refresh_from_db()
        expire(task)
    return task


# --- the backstop: a turn that held a task ended -----------------------------------


def on_turn_ended(turn) -> None:
    """A task's latest turn reached a terminal status without the task being done.

    The agent often ends its turn before submitting (it "responds too soon"), so
    this is never completion: a CANCELLED turn (someone stopped it) fails the task;
    any other end sends a follow-up turn, up to MAX_NUDGES, then fails it. On a laptop
    runner the turn finishes as soon as its session exists and the agent works on in
    that session, so nothing happens while that session is still live there (the
    deadline still holds, and the canopy plugin's Stop hook keeps the agent from
    ending without a result)."""
    try:
        task_id = uuid.UUID(str((turn.origin_ref or {}).get("agent_task") or ""))
    except (ValueError, AttributeError):
        return
    task = (AgentTask.objects.select_related("agent", "created_by")
            .filter(pk=task_id, turn_id=turn.pk, status__in=AgentTask.OPEN).first())
    if task is None or expire(task):
        return
    if turn.status == Turn.CANCELLED:
        _end(task, AgentTask.FAILED, error=f"its turn was cancelled: {turn.result_note}".rstrip(": "))
        return
    if _session_lives_on(turn):
        return
    if task.nudges >= MAX_NUDGES:
        _end(task, AgentTask.FAILED, error=ENDED_WITHOUT_RESULT)
        return
    _nudge(task, turn)


def _session_lives_on(turn) -> bool:
    runner = turn.claimed_by
    if runner is None or runner.kind != runner.EMDASH:
        return False
    return turn_agent_tokens.live_session(turn, runner) is not None


def _nudge(task: AgentTask, ended) -> None:
    """A fresh turn for the same agent and task. Not a send into the ended turn's
    session: that path is a person's chat door (its ACL, sealing, authorship), and a
    nudge has no person behind it — so it is canopy's own turn, accountable to the
    task's creator, carrying the task's instructions again."""
    n = task.nudges + 1
    try:
        turn, _created = harness.enqueue_turn(
            agent=task.agent, origin=Turn.ORIGIN_API,
            idempotency_key=f"{KEY_PREFIX}{task.pk}:nudge:{n}",
            prompt=nudge_prompt(task),
            origin_ref={"agent_task": str(task.pk), "agent_task_nudge": n},
            routing=task.routing,
            enqueued_by=task.created_by,
            initiator=who.system(via="agent-task", accountable=task.created_by),
            parent={"turn": ended},
        )
    except Exception:  # noqa: BLE001 — a nudge that cannot be sent fails the task, loudly
        logger.exception("agent task %s: could not send nudge %s", task.pk, n)
        _end(task, AgentTask.FAILED, error=f"{ENDED_WITHOUT_RESULT}, and a follow-up turn could not be sent")
        return
    moved = AgentTask.objects.filter(pk=task.pk, status__in=AgentTask.OPEN, turn_id=ended.pk).update(
        nudges=n, turn=turn, updated_at=timezone.now())
    if moved and turn.status in Turn.TERMINAL:
        task.refresh_from_db()
        _end(task, AgentTask.FAILED, error=f"its follow-up turn was not run: {turn.result_note or turn.status}")

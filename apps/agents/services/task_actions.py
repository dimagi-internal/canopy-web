from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.harness.dispatch import dispatch, enqueue_task_turn
from apps.harness.models import Turn

from .. import work
from ..models import Agent, AgentTask, AgentTaskAction
from .tasks import get_task


class ClosedAskError(Exception):
    """The task is not in a state this action applies to — the ask is already
    closed, the task is finished, or (nudge) it is not in progress. Acting
    anyway would dispatch twice or start unapproved work."""


#: action -> (closes the ask?, new status or None, needs agent follow-up?)
_EFFECT = {
    AgentTaskAction.APPROVE: (True, AgentTask.IN_PROGRESS, True),
    AgentTaskAction.DECLINE: (True, AgentTask.DECLINED, False),
    AgentTaskAction.REPLY: (None, None, True),  # closes only a question
    AgentTaskAction.NUDGE: (False, None, True),
    AgentTaskAction.DONE: (True, AgentTask.DONE, False),
    # Triage: decide it without starting it. A review ask ("should I do this?")
    # is answered — yes, later — so it closes; a question stays open.
    AgentTaskAction.ACCEPT: (None, AgentTask.TODO, False),
    AgentTaskAction.BACKLOG: (None, AgentTask.BACKLOG, False),
    AgentTaskAction.SNOOZE: (False, None, False),
    AgentTaskAction.DUPLICATE: (True, AgentTask.DECLINED, False),
}


#: How long a snooze lasts when the comment names no date.
SNOOZE_DEFAULT = dt.timedelta(days=7)


def _snooze_until(comment: str):
    """A snooze's end: an ISO date (or datetime) in the comment, else a week."""
    text = (comment or "").strip()
    if text:
        try:
            when = dt.datetime.fromisoformat(text)
        except ValueError:
            raise ValueError(f"snooze takes a date (YYYY-MM-DD) or nothing, got {text!r}") from None
        if when.tzinfo is None:
            when = when.replace(tzinfo=dt.UTC)
        if when <= timezone.now():
            raise ValueError("snooze until a date in the future")
        return when
    return timezone.now() + SNOOZE_DEFAULT


@transaction.atomic
def act(task: AgentTask, *, action: str, comment: str = "", by: str, by_user=None,
        actor_workspace_ids: set, may_start_turns: bool = False,
        by_agent_itself: bool = False,
        ) -> tuple[AgentTask, AgentTaskAction, list[Turn]]:
    """Do one of the actions to `task`; returns (task, action row, turns).

    `accept`/`backlog` decide a task without starting it (→ todo / backlog);
    `snooze` takes it out of Triage until the date in `comment` (default a
    week); `duplicate` cancels it as a duplicate of the task `comment` names.
    Only approve, nudge and an answer start an agent's turn, and only on a
    task an agent is assigned.

    `may_start_turns` is whether the actor holds the tier that may start an
    agent turn (editor — the QuickTurn gate). It decides only whether a REPLY
    wakes the agent: an approve is the decision a viewer exists to make, and it
    starts the work whoever makes it (as `on_approve` always has); a nudge is
    editor-gated at the route.

    `by_agent_itself` is the task's own agent acting on its own card. It decides
    whether ANSWERING a question wakes the agent: an answer is the response the
    agent explicitly asked for, so anyone allowed to answer (the viewer tier)
    starts the turn that carries it — except the agent answering itself, whose
    turn would only wake itself.

    Atomic, and that is the whole ballgame: `dispatch()` raises on a bad
    `on_approve` spec, and committing the action first would leave the ask
    closed and its work never run — permanently, since approving a closed ask is
    refused. Rolling back leaves it open and retryable, with no action row.

    The task row is re-read under a lock before the ask is checked. Two tabs
    holding the same open card would otherwise both pass the check on their own
    stale copies and dispatch the work twice; locked, the second one waits,
    then sees the ask closed and gets `ClosedAskError`.
    """

    from .. import proposals  # cycle: agents.proposals -> agents.services  # noqa: PLC0415

    if action not in _EFFECT:
        raise ValueError(f"action must be one of {'|'.join(_EFFECT)}, got {action!r}")
    comment = (comment or "").strip()
    if action == AgentTaskAction.REPLY and not comment:
        raise ValueError("a reply needs words — comment must not be empty")

    task = (AgentTask.objects.select_for_update(of=("self",))
            .select_related("agent", "project").get(pk=task.pk))
    before = work.snapshot(task)
    name = task.key
    deciding = (AgentTaskAction.APPROVE, AgentTaskAction.DECLINE, AgentTaskAction.ACCEPT,
                AgentTaskAction.BACKLOG, AgentTaskAction.DUPLICATE)
    if action in deciding:
        if task.ask_kind and not task.ask_is_open and action in (AgentTaskAction.APPROVE,
                                                                  AgentTaskAction.DECLINE):
            raise ClosedAskError(f"{name} has no open ask")
        # A plain task with nothing to ask is decidable only while it is live —
        # approving a finished one would quietly re-open it.
        if task.status not in LIVE_STATUSES and (not task.ask_kind or action not in (
                AgentTaskAction.APPROVE, AgentTaskAction.DECLINE)):
            raise ClosedAskError(f"{name} is already {task.status}")
        # ...and approvable only once: approving starts a turn, so approving a
        # plain task that is already in progress would be a nudge any viewer
        # could send. Nudge is the action for that, and it is an editor's.
        if (action == AgentTaskAction.APPROVE and not task.ask_kind
                and task.status == AgentTask.IN_PROGRESS):
            raise ClosedAskError(f"{name} is already in progress — nudge it instead")
    duplicate_of = None
    if action == AgentTaskAction.DUPLICATE:
        duplicate_of = work.task_by_key(comment) if comment else None
        if duplicate_of is None and comment and task.created_by_agent_id:
            duplicate_of = get_task(task.created_by_agent, comment)
        if (duplicate_of is None or duplicate_of.workspace_id != task.workspace_id or duplicate_of.pk == task.pk
                or work.crosses_personal(task, duplicate_of)):
            raise ValueError("duplicate needs the key of the task this one duplicates, in the comment")
    snooze_until = _snooze_until(comment) if action == AgentTaskAction.SNOOZE else None
    # A proposed project's decision card: who may start or decline it is narrower
    # than who may act on a card (decision 4(b)) — `proposals.before_act`.
    proposals.before_act(task, action, by_user)
    if action in (AgentTaskAction.APPROVE, AgentTaskAction.NUDGE) and task.job_schedule_id is not None:
        # A failed job's review has nothing to approve: with no `on_approve` an approve
        # starts a model turn on the agent, and a job runs no model. Refused before
        # anything is written, so the ask stays open for the other decisions.
        raise ValueError("A job does not start from a review. Press Run now on its schedule "
                         "to run it again, or decline this once it no longer matters.")
    if action == AgentTaskAction.NUDGE and task.status != AgentTask.IN_PROGRESS:
        # A suggested task is started by approving it; nudging it would start
        # work nobody approved. A finished one has nothing to nudge.
        raise ClosedAskError(f"{name} is {task.status}, not in progress — nothing to nudge")
    if action == AgentTaskAction.NUDGE and task.agent_id is None:
        raise ClosedAskError(f"{name} is not assigned to an agent — there is nobody to nudge")

    closes, status, follow_up = _EFFECT[action]
    if action == AgentTaskAction.REPLY:
        # A reply on a question IS the answer; on anything else it is a note.
        closes = task.ask_kind == AgentTask.ASK_QUESTION and task.ask_is_open
    elif action in (AgentTaskAction.ACCEPT, AgentTaskAction.BACKLOG):
        closes = task.ask_kind == AgentTask.ASK_REVIEW and task.ask_is_open
    if task.agent_id is None:
        # Nobody's queue to put it on, and no agent to start: the action is the
        # whole of it. Approving a person's task moves it to In progress.
        follow_up = False

    row = AgentTaskAction(
        agent=task.agent, task=task, action=action, comment=comment, by=by,
        by_user=by_user if getattr(by_user, "is_authenticated", False) else None,
    )
    turns: list[Turn] = []
    # Answering a question runs `on_approve` when the card has one; without one,
    # the card's own turn carries the answer (below) — the agent asked for it.
    runs_on_approve = action == AgentTaskAction.APPROVE or (action == AgentTaskAction.REPLY and closes)
    # A failed job's review never starts a turn, whatever the action: a reply is a note on it,
    # recorded with no turn (the job runs no model, so a card turn would be an AI doing nothing useful).
    job_review = task.job_schedule_id is not None
    starts_card_turn = task.agent_id is not None and not job_review and (
        action in (AgentTaskAction.APPROVE, AgentTaskAction.NUDGE)
        or (action == AgentTaskAction.REPLY and closes and not by_agent_itself)
        or (action == AgentTaskAction.REPLY and not closes and may_start_turns))
    if runs_on_approve and task.on_approve:
        turns = dispatch(task, action=row, actor_workspace_ids=actor_workspace_ids)
        task.dispatched_at = timezone.now()
        follow_up = False  # the dispatched turn IS the follow-up
    elif starts_card_turn:
        # No spec of its own: the card's own turn. The row is saved first because
        # its pk keys the turn (one enqueue per click; a replay is the same turn).
        # An answer to a question is its own kind of board turn ("ANSWERED BY").
        row.status = AgentTaskAction.APPLIED
        row.applied_at = timezone.now()
        row.save()
        answers = action == AgentTaskAction.REPLY and closes
        turn, _created = enqueue_task_turn(task, action=row, kind="answer" if answers else None)
        turns = [turn]
        task.dispatched_at = timezone.now()
        follow_up = False
    if closes and task.ask_is_open:
        task.ask_closed_at = timezone.now()
        # Answered: nobody is waiting on a person any more.
        task.waiting_on_user = None
    if status:
        task.status = status
    elif runs_on_approve and turns:
        task.status = AgentTask.IN_PROGRESS  # the agent has the ball now
    # Anything that happens to a snoozed task wakes it; a snooze sets it.
    task.snoozed_until = snooze_until
    task.save()
    proposals.after_act(task, action, comment)  # a decision card starts or archives its project

    row.status = AgentTaskAction.PENDING if follow_up else AgentTaskAction.APPLIED
    row.applied_at = None if follow_up else (row.applied_at or timezone.now())
    row.save()
    actor = by_user if getattr(by_user, "is_authenticated", False) else None
    by_agent = task.agent if by_agent_itself else None
    work.record_action(task, row, by_user=actor, by_agent=by_agent)
    work.record_changes(task, before, by_user=actor, by_agent=by_agent)
    if duplicate_of is not None:
        work.add_relation(task, "duplicate", duplicate_of, by_user=actor, by_agent=by_agent)
    return task, row, turns


def pending_actions(agent: Agent):
    """The agent's queue: actions it still has to carry out, oldest first —
    on the tasks it was assigned when each was done."""
    return (agent.task_actions.filter(status=AgentTaskAction.PENDING)
            .select_related("agent", "task", "by_user")
            .order_by("created_at", "id"))


def mark_applied(action_row: AgentTaskAction, result_note: str = "") -> AgentTaskAction:
    action_row.status = AgentTaskAction.APPLIED
    action_row.applied_at = timezone.now()
    if result_note:
        action_row.result_note = result_note
    action_row.save(update_fields=["status", "applied_at", "result_note"])
    return action_row


#: Statuses a task is still LIVE in. A done or declined card waits on nobody.
LIVE_STATUSES = list(AgentTask.LIVE_STATUSES)


def waiting_q(prefix: str = ""):
    """"Somebody has to do something" — as ONE predicate.

    Two shapes count, and both are real:
      * an open ASK (a review or question nobody has answered), and
      * a live task PARKED ON A PERSON, which is most of what the fleet's
        boards actually hold — "waiting on Andrea for the numbers" is a wait
        even though nothing is being asked.

    One definition because several consumers read it (the inbox, the waiting
    badge, push, the per-project waiting count), and this codebase has already
    paid for the same predicate written three times. `prefix` (e.g. "tasks__")
    lets a query on a related model — a project annotating its tasks — reuse it.
    """
    p = prefix
    return Q(**{f"{p}status__in": LIVE_STATUSES}) & (
        (~Q(**{f"{p}ask_kind": ""}) & Q(**{f"{p}ask_closed_at__isnull": True}))
        | Q(**{f"{p}waiting_on_user__isnull": False})
    )


def filter_tasks(qs, *, user=None, project: str = "", status: str = "", waiting: str = "",
                 ask: str = "", batch: str = "", parent: str = "", assignee: str = "",
                 created_by: str = "", label: str = "", priority: str = "", milestone: str = "",
                 category: str = "", kind: str = "", due_before=None, due_after=None, q: str = "",
                 snoozed: str = ""):
    """The task list's filters, shared by the per-agent and workspace routes.

    `waiting=me` is what lands in a person's inbox: tasks parked on them, plus
    open asks routed to nobody — an unrouted ask waits on whoever looks at it.
    `assignee` / `created_by`: `me`, an email, `agent:<slug>` (or a bare slug),
    or `none`. `label`, `priority`, `category`, `status` take comma lists.
    `project`: an alias ("P3"), a handle, an address, or `none`. `snoozed=hide`
    leaves out tasks snoozed into the future; `only` keeps just those.
    """
    def _split(value: str) -> list[str]:
        return [v.strip() for v in (value or "").split(",") if v.strip()]

    def _who(value: str, *, agent_field: str, user_field: str) -> Q:
        value = (value or "").strip()
        if value == "none":
            return Q(**{f"{agent_field}__isnull": True, f"{user_field}__isnull": True})
        if value == "me":
            return Q(**{user_field: user}) if getattr(user, "is_authenticated", False) else Q(pk__in=[])
        if "@" in value:
            return Q(**{f"{user_field}__email__iexact": value})
        return Q(**{f"{agent_field}__slug": value.removeprefix("agent:")})

    if project == "none":
        qs = qs.filter(project__isnull=True)
    elif project:
        ws, _, handle = project.rpartition("/")
        pq = Q(project__ext_id__iexact=project) | Q(project__handle__iexact=handle or project)
        if ws:
            pq = Q(project__workspace_id=ws, project__handle__iexact=handle)
        qs = qs.filter(pq)
    if status:
        qs = qs.filter(status__in=_split(status))
    if category:
        cats = set(_split(category))
        qs = qs.filter(status__in=[st for st, c in AgentTask.CATEGORY.items() if c in cats])
    if waiting == "me":
        unrouted_ask = (~Q(ask_kind="") & Q(ask_closed_at__isnull=True)
                        & Q(waiting_on_user__isnull=True))
        mine = Q(waiting_on_user=user) if getattr(user, "is_authenticated", False) else Q(pk__in=[])
        qs = qs.filter(waiting_q()).filter(mine | unrouted_ask)
    if ask == "open":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=True)
    elif ask == "closed":
        qs = qs.exclude(ask_kind="").filter(ask_closed_at__isnull=False)
    if batch:
        qs = qs.filter(batch_key=batch)
    if parent == "none":
        qs = qs.filter(parent__isnull=True)
    elif parent:
        qs = qs.filter(Q(parent__key__iexact=parent) | Q(parent__ext_id__iexact=parent))
    if assignee:
        qs = qs.filter(_who(assignee, agent_field="agent", user_field="assignee_user"))
    if created_by:
        qs = qs.filter(_who(created_by, agent_field="created_by_agent", user_field="created_by_user"))
    if label:
        for name in _split(label):
            qs = qs.filter(labels__name__iexact=name)
    if priority:
        # A number no column can hold matches nothing; it is not the database's to refuse.
        qs = qs.filter(priority__in=[int(p) for p in _split(priority) if p.isdecimal() and int(p) < 2**31])
    if milestone == "none":
        qs = qs.filter(milestone__isnull=True)
    elif milestone:
        qs = qs.filter(Q(milestone__name__iexact=milestone) | Q(milestone_id=int(milestone))
                       if milestone.isdecimal() and int(milestone) < 2**63
                       else Q(milestone__name__iexact=milestone))
    if kind:
        qs = qs.filter(kind=kind)
    if due_before:
        qs = qs.filter(due__lte=due_before)
    if due_after:
        qs = qs.filter(due__gte=due_after)
    if q:
        text = q.strip()
        qs = qs.filter(Q(title__icontains=text) | Q(key__iexact=text) | Q(next_action__icontains=text)
                       | Q(ext_id__iexact=text))
    if snoozed == "hide":
        qs = qs.filter(Q(snoozed_until__isnull=True) | Q(snoozed_until__lte=timezone.now()))
    elif snoozed == "only":
        qs = qs.filter(snoozed_until__gt=timezone.now())
    if label:
        qs = qs.distinct()
    return qs


# ---- Agent credentials (per-agent secret store, encrypted at rest) ----------
# Spec: docs/superpowers/specs/2026-09-05-agent-credentials-design.md

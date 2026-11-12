from __future__ import annotations

import re

from django.db import IntegrityError, transaction
from django.db.models import F, Q

from apps.contacts import people
from apps.contacts.models import Person
from apps.harness.models import Turn

from .. import work
from ..models import MAX_TASK_DEPTH, Agent, AgentProject, AgentTask, AgentTaskHumanWhy
from ._base import _aware
from .projects import (
    _link_participants,
    _workspace_member,
    agent_is_on_project,
    get_project,
    get_workspace_project,
    may_hold_workspace_project,
    may_join_workspace_project,
    may_read_project,
    may_use_project,
    resolve_project_owner_in,
)

_VALID_TASK_STATUS = set(AgentTask.CATEGORY)


# ---- tasks ----
def _norm_status(s: str) -> str:
    return s if s in _VALID_TASK_STATUS else AgentTask.SUGGESTED


_TASK_FIELDS = ("title", "next_action", "status", "owner", "assigned", "confidence",
                "score", "review", "rationale", "source_url", "plan", "due", "notes",
                "drive_folder_id", "drive_folder_url", "priority")


def _norm_confidence(value) -> str:
    value = (value or "").strip().lower()
    return value if value in AgentTask.CONFIDENCE_VALUES else ""


def _norm_priority(value) -> int:
    try:
        value = int(value or 0)
    except (TypeError, ValueError):
        raise ValueError(f"priority is 0 (none) to 4 (low), got {value!r}") from None
    if not 0 <= value <= 4:
        raise ValueError(f"priority is 0 (none) to 4 (low), got {value!r}")
    return value


def _norm_field(f: str, value):
    if f == "status":
        return _norm_status(value)
    if f == "confidence":
        return _norm_confidence(value)
    if f == "priority":
        return _norm_priority(value)
    return value


def agent_tasks_q(agent: Agent, prefix: str = ""):
    """The tasks an agent's board shows: the ones it is assigned, and the ones
    it filed that no agent is assigned (a suggestion for a person, say)."""
    p = prefix
    return Q(**{f"{p}agent": agent}) | Q(**{f"{p}created_by_agent": agent, f"{p}agent__isnull": True})


class TaskTreeError(ValueError):
    """A parent that would break the task tree: another agent's task, another
    project's, the task itself or one of its own subtasks (a cycle), or a
    chain deeper than `MAX_TASK_DEPTH`. A ValueError, so every route that
    already maps those to 422 refuses it the same way."""


def _depth_above(task: AgentTask) -> int:
    """How many levels sit above `task` (0 for a top-level task)."""
    depth, node, seen = 0, task, set()
    while node.parent_id is not None and node.pk not in seen:
        seen.add(node.pk)
        node = node.parent
        depth += 1
    return depth


def _height_below(task: AgentTask) -> int:
    """How many levels of subtasks hang under `task` (0 for a leaf). One
    query per level, and the tree is at most `MAX_TASK_DEPTH` deep."""
    if task.pk is None:
        return 0
    height, level = 0, [task.pk]
    while level:
        level = list(AgentTask.objects.filter(parent_id__in=level).values_list("pk", flat=True))
        if level:
            height += 1
        if height > MAX_TASK_DEPTH:  # a corrupt tree; refuse rather than spin
            break
    return height


def resolve_parent(task: AgentTask, ref: str, agent: Agent | None = None, *, user=None) -> AgentTask | None:
    """The task `ref` names as `task`'s parent, checked — or None to clear.

    Refused (TaskTreeError) when the parent is unknown, is the task itself or
    one of its descendants, sits in a different project, or would make the
    chain deeper than MAX_TASK_DEPTH. `task` is the CHILD as it will be saved:
    its `project` must already be the one it will have. A task in a personal
    project `user` may not read is as unknown as one that does not exist.
    """
    ref = (ref or "").strip()
    if not ref:
        return None
    namer = agent or task.created_by_agent or task.agent
    parent = get_task(namer, ref) if namer is not None else work.task_by_key(ref)
    if (parent is None or parent.workspace_id != task.workspace_id
            or (parent.project_id and not may_use_project(parent.project, user=user, agent=namer))):
        raise TaskTreeError(f"parent {ref} is not a task of {namer.slug if namer else task.workspace_id}")
    if task.pk is not None:
        node, seen = parent, set()
        while node is not None and node.pk not in seen:
            if node.pk == task.pk:
                raise TaskTreeError(f"{parent.ref} is {task.ref} or one of its subtasks "
                                    f"— a task cannot be its own ancestor")
            seen.add(node.pk)
            node = node.parent
    if parent.project_id != task.project_id:
        where = parent.project.address if parent.project_id else "no project"
        raise TaskTreeError(f"parent {parent.ref} is in {where}; a subtask lives in its "
                            f"parent's project")
    levels = _depth_above(parent) + 1 + 1 + _height_below(task)
    if levels > MAX_TASK_DEPTH:
        raise TaskTreeError(f"making {parent.ref} the parent would nest tasks {levels} deep "
                            f"(at most {MAX_TASK_DEPTH}) — file the deeper work as its own task")
    return parent


def _kind(value) -> str:
    kind = (value or AgentTask.WORK).strip().lower()
    if kind not in (AgentTask.WORK, AgentTask.IDEA):
        raise ValueError(f"kind must be work|idea, got {value!r}")
    return kind


def _require_words(human_why) -> None:
    """An idea is a PERSON's want: it carries their own words, or it is not one."""
    if not any((w or {}).get("quote", "").strip() for w in (human_why or [])):
        raise ValueError("an idea (kind=idea) carries a person's own words — give at least one "
                         "human_why quote; the agent's own work is kind=work")


def _resolve_who(workspace_slug: str, who: dict):
    """(person, email, name) for one quote's speaker.

    A Person is linked only when canopy knows them in the task's workspace
    (`people.known_in`) — the same gate as every per-person read, so a quote
    cannot be used to learn that canopy knows someone another tenant deals
    with. An email canopy does not know is kept as text: the quote still says
    who. A `person_id` that is not known here is refused — it was explicit."""

    pid = who.get("person_id")
    email = (who.get("email") or "").strip().lower()
    name = (who.get("name") or "").strip()
    person = None
    if pid is not None:
        person = Person.objects.filter(pk=pid).first()
        if person is None or not people.known_in(person, workspace_slug):
            raise ValueError(f"person {pid} is not someone {workspace_slug} knows")
    elif email:
        found = people.by_email(email)
        if found is not None and people.known_in(found, workspace_slug):
            person = found
    if person is not None:
        email = email or people.email_of(person)
        name = name or people.display_name(person)
    return person, email, name


def set_human_why(task: AgentTask, items: list[dict]) -> None:
    """Replace the task's human-why quotes with `items` (T68), in order."""
    rows = []
    for i, item in enumerate(items or []):
        quote = (item.get("quote") or "").strip()
        if not quote:
            raise ValueError("a human_why entry needs the person's quote")
        person, email, name = _resolve_who(task.workspace_id, item.get("who") or {})
        rows.append(AgentTaskHumanWhy(
            task=task, position=i, quote=quote, person=person, who_email=email,
            who_name=name, source_url=(item.get("source_url") or "").strip(),
            said_at=_aware(item.get("said_at")),
        ))
    task.human_whys.all().delete()
    AgentTaskHumanWhy.objects.bulk_create(rows)
    if hasattr(task, "_prefetched_objects_cache"):
        task._prefetched_objects_cache.pop("human_whys", None)


def _descendants(task: AgentTask) -> list[AgentTask]:
    out, level = [], [task.pk]
    for _ in range(MAX_TASK_DEPTH):
        rows = list(AgentTask.objects.filter(parent_id__in=level))
        if not rows:
            break
        out.extend(rows)
        level = [r.pk for r in rows]
    return out


#: What a create may carry beyond `_TASK_FIELDS`, copied straight onto the row.
_TASK_CREATE_EXTRAS = ("ask_body", "on_approve", "batch_key", "origin", "origin_ref", "source")


_ASK_KINDS = {AgentTask.ASK_NONE, AgentTask.ASK_REVIEW, AgentTask.ASK_QUESTION}


def _plain_links(links) -> list:
    return [link.model_dump() if hasattr(link, "model_dump") else dict(link) for link in links or []]


class AssigneeError(ValueError):
    """An assignee canopy cannot give the task to: an unknown person or agent,
    or one outside the task's workspace."""


def resolve_assignee(workspace_slug: str, value: str,
                     project: AgentProject | None = None) -> tuple[Agent | None, object | None]:
    """`(agent, user)` from an assignee string: `""` (nobody), a member's email,
    or an agent's slug (`agent:<slug>` or bare). An agent must belong to the
    workspace or one above it, or — for a task in `project` — be one the project
    is shared with; a person must be a member."""
    value = (value or "").strip()
    if not value:
        return None, None
    if "@" in value:
        return None, _workspace_member(workspace_slug, value, instead="assigned")
    slug = value.removeprefix("agent:").strip()
    agent = Agent.objects.filter(slug=slug).first()
    if agent is None or not (may_hold_workspace_project(agent, workspace_slug)
                             or (project is not None and may_join_workspace_project(agent, project))):
        raise AssigneeError(f"no agent '{slug}' can be assigned work in {workspace_slug}")
    return agent, None


def _refuse_outsiders(project, *, agent: Agent | None = None, people=()) -> None:
    """Work in a personal project goes only to someone who can read it: an agent
    on it, or its lead. A wait or an assignment on anyone else would be one they
    could never see, and approving it would hand an agent the project in a prompt."""
    if project is None or not project.is_personal:
        return
    if agent is not None and not agent_is_on_project(agent, project):
        raise AssigneeError(f"'{agent.slug}' is not on {project.address}, which is personal: add it to the project first")
    if any(p is not None and not may_read_project(p, project) for p in people):
        raise AssigneeError(f"{project.address} is personal: its work goes only to its lead and its agents")


def _task_workspace(agent: Agent | None, project, workspace: str) -> str:
    if project is not None:
        return project.workspace_id
    if workspace:
        return workspace
    if agent is not None:
        return agent.workspace_id
    raise ValueError("a task needs a workspace")


@transaction.atomic
def create_tasks(agent: Agent | None, payloads: list[dict], *, workspace: str = "",
                 by_user=None) -> list[AgentTask]:
    """Create a batch of tasks, idempotent per `idempotency_key`.

    Through an agent's route (`agent` given, `workspace` not): the agent filed
    them, is their assignee unless the payload names one (`assignee`), and each
    carries its `T<n>` alias. Otherwise they are made in `workspace` by
    `by_user`, assigned as the payload says.

    ONE outer transaction, so a batch that cannot route somebody's wait
    (`UnknownPersonError`) leaves nothing behind; each row keeps its own
    SAVEPOINT so a key two producers raced on replays instead of rolling the
    batch back. A key already used by ANOTHER agent is refused rather than
    replayed — handing back somebody else's task would cross a tenant line.
    """
    via_agent = agent is not None and not workspace
    out = []
    for p in payloads:
        key = (p.get("idempotency_key") or "").strip()
        existing = AgentTask.objects.filter(idempotency_key=key).first() if key else None
        if existing is not None:
            if (existing.created_by_agent_id != (agent.pk if agent else None)
                    or (existing.project_id and not may_use_project(existing.project, user=by_user, agent=agent))):
                raise ValueError(f"idempotency_key {key!r} is already used by another agent")
            out.append(existing)
            continue

        fields = {f: _norm_field(f, p[f]) for f in _TASK_FIELDS if p.get(f) is not None}
        fields.update({f: p[f] for f in _TASK_CREATE_EXTRAS if p.get(f) is not None})
        fields["status"] = _norm_status(fields.get("status", AgentTask.SUGGESTED))
        rec = _clean_recurrence(p.get("recurrence"))
        if rec is not None:
            fields["status"] = AgentTask.BACKLOG  # a template waits in backlog; its instances are the work
        ask_kind = p.get("ask_kind") or AgentTask.ASK_NONE
        if ask_kind not in _ASK_KINDS:
            raise ValueError(f"ask_kind must be review|question, got {ask_kind!r}")
        if p.get("links"):
            fields["links"] = _plain_links(p["links"])
        # An unknown project reference files the task nowhere rather than failing
        # the create: the task is the thing worth keeping, and a typo in "P7" must
        # not cost the agent the work it just recorded. The response carries
        # `project_ext_id: null`, so the miss is visible.
        ref = str(p.get("project") or "").strip()
        raised_by = p.get("raised_by") or None
        if raised_by and agent is not None and not Turn.objects.filter(pk=raised_by, agent=agent).exists():
            raise ValueError(f"raised_by {raised_by} is not a turn of {agent.slug}")
        # A person filing (no agent) may still name a turn; it must exist, or the
        # insert fails on the foreign key and the caller's typo becomes a 500.
        if raised_by and agent is None and not Turn.objects.filter(pk=raised_by).exists():
            raise ValueError(f"raised_by {raised_by} is not a turn")
        project = _project_ref(agent, ref, workspace, user=by_user) if ref else None
        parent_ref = str(p.get("parent") or "").strip()
        parent = None
        ws = _task_workspace(agent, project, workspace)
        if parent_ref:
            # A subtask lives in its parent's project: one named with no project
            # inherits it, so `{"parent": "T66"}` alone files it correctly.
            if not ref:
                found = get_task(agent, parent_ref) if agent is not None else work.task_by_key(parent_ref)
                if found is not None and found.project_id and not may_use_project(found.project, user=by_user, agent=agent):
                    found = None  # resolve_parent refuses it, as a parent it cannot find
                project = found.project if found is not None else None
                ws = _task_workspace(agent, project, found.workspace_id if found is not None else workspace)
            parent = resolve_parent(AgentTask(agent=agent, created_by_agent=agent, project=project,
                                              workspace_id=ws), parent_ref, agent, user=by_user)
        waiting_on = _workspace_member(ws, p["waiting_on_email"], instead="assigned") \
            if p.get("waiting_on_email") else None
        if "assignee" in p and p["assignee"] is not None:
            assignee_agent, assignee_user = resolve_assignee(ws, p["assignee"], project)
        else:
            assignee_agent, assignee_user = (agent if via_agent else None), None
        _refuse_outsiders(project, agent=assignee_agent, people=(assignee_user, waiting_on))
        kind = _kind(p.get("kind"))
        if kind == AgentTask.IDEA:
            _require_words(p.get("human_why"))
        ext_id = _claim_ext_id(agent, (p.get("ext_id") or "").strip()) if via_agent else ""
        labels = work.resolve_labels(ws, p.get("labels") or [], create=True) if p.get("labels") else []
        milestone = work.resolve_milestone(project, p.get("milestone")) if p.get("milestone") else None
        rank = float(p["position"]) if p.get("position") is not None else work.top_rank(ws)
        number, task_key = work.allocate_number(ws)
        try:
            with transaction.atomic():  # savepoint
                task = AgentTask.objects.create(
                    workspace_id=ws,
                    number=number,
                    key=task_key,
                    agent=assignee_agent,
                    assignee_user=assignee_user,
                    created_by_agent=agent,
                    created_by_user=by_user if getattr(by_user, "is_authenticated", False) else None,
                    ext_id=ext_id,
                    project=project,
                    parent=parent,
                    kind=kind,
                    ask_kind=ask_kind,
                    idempotency_key=key or None,
                    raised_by_id=raised_by,
                    waiting_on_user=waiting_on,
                    milestone=milestone,
                    rank=rank,
                    recurrence=rec,
                    **fields,
                )
                if p.get("human_why"):
                    set_human_why(task, p["human_why"])
                if labels:
                    task.labels.set(labels)
                work.record_created(task, by_user=by_user, by_agent=agent)
            out.append(task)
            _link_participants(task)
        except IntegrityError:
            replay = (AgentTask.objects.filter(created_by_agent=agent, idempotency_key=key).first()
                      if key and agent is not None else None)
            if replay is not None:
                out.append(replay)
                continue
            if agent is not None and ext_id and _ext_id_taken(agent, ext_id):  # a concurrent create took it
                raise DuplicateTaskError(f"{agent.slug} already has a task {ext_id}") from None
            raise
    return out


def _clean_recurrence(value):
    from .recurrence import clean  # noqa: PLC0415 — recurrence imports this module's writers

    return clean(value)


def _project_ref(agent: Agent | None, ref: str, workspace: str, *, user=None):
    """A project named by an agent (alias, handle or address), else by address
    or handle in `workspace`. A personal project `user` may not read is not found."""
    if agent is not None:
        project = get_project(agent, ref)
    else:
        ws, _, handle = ref.rpartition("/")
        project = get_workspace_project(ws or workspace, handle)
    return project if project is None or may_use_project(project, user=user, agent=agent) else None


class DuplicateTaskError(Exception):
    """An explicit `ext_id` the agent already uses (in any letter case)."""


def _ext_id_taken(agent: Agent, ext_id: str) -> bool:
    return AgentTask.objects.filter(created_by_agent=agent, ext_id__iexact=ext_id).exists()


def _claim_ext_id(agent: Agent, explicit: str) -> str:
    """The alias a new task filed through an agent's route gets.

    Explicit: refused if the agent already has it in ANY case — lookups are
    case-insensitive (`get_task`), so "t1" beside "T1" would make one of them
    unreachable. An explicit `T<n>` also moves the counter past n.

    Auto: the next counter value that is free. An explicit id may already sit
    ahead of the counter, and handing it out again would 409 every auto create
    from then on (the counter bump rolls back with the failed batch).
    """
    if explicit:
        if "/" in explicit:
            # A task is addressed as `/tasks/{ext_id}/` — a slash cannot be routed.
            raise ValueError(f"ext_id {explicit!r} may not contain '/'")
        if work.looks_like_key(explicit) and not re.fullmatch(r"[Tt]\d+", explicit):
            raise ValueError(f"ext_id {explicit!r} looks like a task key; an alias may not")
        if _ext_id_taken(agent, explicit):
            raise DuplicateTaskError(f"{agent.slug} already has a task {explicit}")
        m = re.fullmatch(r"[Tt](\d+)", explicit)
        if m:
            Agent.objects.filter(pk=agent.pk, task_seq__lt=int(m.group(1))).update(
                task_seq=int(m.group(1)))
        return explicit
    while True:
        ext_id = next_task_ext_id(agent)
        if not _ext_id_taken(agent, ext_id):
            return ext_id


def resolve_waiting_on(agent_or_ws, email: str):
    """The member of the workspace (an agent's, or a slug) with that email, or
    None to clear.

    Scoped to the workspace on purpose: "waiting on you" is a claim on somebody
    canopy can notify AND who can see the work, so routing it to a stranger
    would create a wait nobody will ever answer.
    """
    ws = agent_or_ws if isinstance(agent_or_ws, str) else agent_or_ws.workspace_id
    return _workspace_member(ws, email, instead="assigned")


def resolve_project_owner(agent: Agent, email: str):
    """A project's lead as a real person, or None to clear — the same
    workspace rule as `resolve_waiting_on`, because the lead is who an idea
    added on the team view emails (`ideas.notify`)."""
    return resolve_project_owner_in(agent.workspace_id, email)


@transaction.atomic
def patch_task(task: AgentTask, data, *, by_user=None, by_agent=None) -> AgentTask:
    """Partial update — only fields present in `data` (a dict) are written, and
    each tracked field that changes is a `TaskEvent`. Atomic: a refused quote,
    assignee or tree move leaves the task exactly as it was.

    `assignee`: `""` (nobody), a member's email, or an agent's slug. `labels`
    REPLACES the list (names; unknown ones are made). `position` is the old
    integer order and sets `rank`; `before`/`after` (keys) place it between two
    tasks. Any change but a snooze wakes a snoozed task."""
    before = work.snapshot(task)
    for f in _TASK_FIELDS:
        if f in data:
            setattr(task, f, _norm_field(f, data[f]))
    if "links" in data:
        task.links = data["links"]
    if "waiting_on_email" in data:
        task.waiting_on_user = _workspace_member(task.workspace_id, data["waiting_on_email"],
                                                 instead="assigned")
    if "assignee" in data and data["assignee"] is not None:
        task.agent, task.assignee_user = resolve_assignee(task.workspace_id, data["assignee"], task.project)
    if data.get("position") is not None:
        task.rank = float(data["position"])
    if data.get("rank") is not None:
        task.rank = float(data["rank"])
    if "snoozed_until" in data:
        task.snoozed_until = data["snoozed_until"]
    elif task.snoozed_until is not None:
        task.snoozed_until = None
    moved = False
    if "project" in data:
        # Present-and-empty means "take it out of its project"; absent means
        # "leave it where it is" (the schema keeps the two apart).
        ref = (data["project"] or "").strip()
        namer = by_agent or task.created_by_agent or task.agent
        new_project = (_project_ref(namer, ref, task.workspace_id, user=by_user) if ref else None)
        if ref and new_project is None:
            raise ValueError(f"no project {ref!r}")
        moved = (new_project.pk if new_project else None) != task.project_id
        task.project = new_project
        if moved and task.milestone_id and "milestone" not in data:
            task.milestone = None  # a milestone is its project's
    if "parent" in data:
        task.parent = resolve_parent(task, data["parent"] or "", by_agent, user=by_user)
    elif moved and task.parent_id is not None:
        # Moving a subtask out of its parent's project, without naming a new
        # parent, would leave a child filed somewhere its parent is not.
        raise TaskTreeError(f"{task.ref} is a subtask of {task.parent.ref}; move the parent, "
                            f"or send \"parent\": \"\" to detach it first")
    if "milestone" in data:
        task.milestone = work.resolve_milestone(task.project, data["milestone"])
    if data.get("kind") is not None:
        task.kind = _kind(data["kind"])
    if "recurrence" in data:
        rec = _clean_recurrence(data["recurrence"])
        if rec is not None and task.template_id is not None:
            raise ValueError(f"{task.ref} is an instance of {task.template.key}; an instance does not recur")
        task.recurrence = rec
        if rec is not None and "status" not in data:
            task.status = AgentTask.BACKLOG
    if task.recurrence is not None and task.status != AgentTask.BACKLOG:
        raise ValueError(f"{task.ref} recurs: a recurring task stays in backlog and its instances are the work "
                         f"(send \"recurrence\": null first to finish it)")
    if moved or "assignee" in data or "waiting_on_email" in data:
        _refuse_outsiders(task.project, agent=task.agent, people=(task.assignee_user, task.waiting_on_user))
    if moved:
        for child in _descendants(task):
            _refuse_outsiders(task.project, agent=child.agent, people=(child.assignee_user, child.waiting_on_user))
    task.save()
    if data.get("before") is not None or data.get("after") is not None:
        work.move_task(task, before_key=data.get("before") or "", after_key=data.get("after") or "")
    if "labels" in data and data["labels"] is not None:
        task.labels.set(work.resolve_labels(task.workspace_id, data["labels"], create=True))
    if moved and task.project_id and task.project.workspace_id != task.workspace_id:
        work.rehome(task, task.project.workspace_id, by_user=by_user, by_agent=by_agent)
    if moved:
        # A subtree moves as one: a parent's subtasks follow it to its new project.
        for child in _descendants(task):
            child.project = task.project
            child.save(update_fields=["project", "updated_at"])
            if child.workspace_id != task.workspace_id:
                work.rehome(child, task.workspace_id, by_user=by_user, by_agent=by_agent)
            _link_participants(child)
    if "human_why" in data:
        set_human_why(task, data["human_why"] or [])
    if task.kind == AgentTask.IDEA and not task.human_whys.exists():
        # Atomic: refusing here leaves the task exactly as it was.
        raise ValueError(f"{task.ref} is an idea, and an idea carries a person's own words — "
                         f"give it at least one human_why quote")
    if "project" in data:
        _link_participants(task)
    work.record_changes(task, before, by_user=by_user, by_agent=by_agent)
    return task


def get_task(agent: Agent, ref: str) -> AgentTask | None:
    """A task an agent names: by key (`DIM-142`), or by alias ("T3") — its own
    first, then one it is assigned."""
    ref = str(ref or "").strip()
    if not ref:
        return None
    if work.looks_like_key(ref):
        found = work.task_by_key(ref)
        if found is not None:
            return found
    base = AgentTask.objects.select_related("agent", "project", "workspace", "assignee_user", "created_by_agent")
    return (base.filter(created_by_agent=agent, ext_id__iexact=ref).first()
            or base.filter(agent=agent, ext_id__iexact=ref).first())


def list_tasks(agent: Agent) -> list[AgentTask]:
    return list(AgentTask.objects.filter(agent_tasks_q(agent)).select_related("agent"))


# ---- actions: everything a person does TO a task ------------------------
#
# Five of them — approve, decline, reply, nudge, done — through one function.
# Each is recorded as an `AgentTaskAction` row, which is both the task's history
# ("who approved this and why" is the closing row) and, while `pending`, the
# agent's to-do: it drains pending rows on its next turn and marks each applied.
#
# An action that hands the agent work STARTS a turn rather than leaving a row
# for whenever the agent next runs: approve always does,
# nudge always does, answering an open question does (whoever answers), and any
# other reply does when an editor writes it. The task's own `on_approve` specs
# run when it has them; otherwise canopy-web writes the one turn from the card
# (`apps.harness.dispatch.enqueue_task_turn`). A row whose turn was enqueued is
# `applied` — the turn IS the follow-up.


def next_task_ext_id(agent: Agent) -> str:
    """T1, T2, … from a counter that only goes up — the same rule projects use,
    and for the same reason: a reused id makes an old link point at new work."""
    Agent.objects.filter(pk=agent.pk).update(task_seq=F("task_seq") + 1)
    agent.refresh_from_db(fields=["task_seq"])
    return f"T{agent.task_seq}"

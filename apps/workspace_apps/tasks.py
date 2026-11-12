"""An app's pages acting on canopy tasks of ONE declared project, as the viewer.

The manifest's `tasks` (`manifest.py::_validate_tasks`) names a text SETTING whose value is a
project's handle in the app's workspace (bound by an admin: `overlay.is_risky`), the app role that
reads that project's tasks, and named actions, each one `do` over a fixed field allowlist.

The rules this module is:

* **One project.** Every read and write is of a task whose project IS the bound project, in the
  app's workspace, and that the caller may read (`task_read_q`): a personal project stays its
  lead's whatever the app declares. Anything else — another project, an undeclared action, an
  unbound setting, a switched-off app — is a 404.
* **The app role replaces the workspace task gates, for exactly that.** The admin who installed
  the app consented to its declaration, so a person holding the action's app role runs it AS
  THEMSELVES through the task services (`services.create_tasks` / `patch_task`, `task_actions.act`,
  `recurrence.spawn`), recorded under their name, whatever their workspace role. Nothing else
  about a task opens: no other field, no other action, no other project.
* **Never a turn.** `done` and `comment` go through `task_actions.act`, whose only turn-starting
  actions (approve, reply, nudge) no declaration can name; `reopen` and `patch` are a field write;
  `create` files the task with NO agent assignee; `spawn` copies a template. Status moves only
  through `done` / `reopen`.
* **Live work only.** A draft's test copy has sample records, never tasks: the routes refuse a
  draft (409), and a scenario can name only the app's own actions (`manifest._validate_scenarios`).
* **An agent acting for a person** (`on_behalf_by_agent`): the person's own app role decides, and
  history records "<agent> for <person>". For a confined turn's verified asker; nothing here
  verifies the asker, its caller does.
"""
from __future__ import annotations

import datetime as dt

from django.contrib.auth import get_user_model
from django.db import transaction

from apps.agents import services as task_services
from apps.agents import work
from apps.agents.models import Agent, AgentProject, AgentTask, TaskEvent
from apps.agents.services import recurrence
from apps.workspaces import services as wsvc

from . import access
from .models import AppInstallation
from .services import Refused

MAX_QUERY = 200
#: Statuses `reopen` brings back from (the task is finished or dropped).
_CLOSED = (AgentTask.DONE, AgentTask.DECLINED)


def declared(manifest: dict) -> dict | None:
    """What a page is told about the declaration (`api._schema`): roles, actions, fields."""
    t = manifest.get("tasks")
    return None if not t else {"read": t["read"], "actions": t["actions"]}


def project_of(inst: AppInstallation) -> AgentProject | None:
    """The project the live app is bound to, or None (no declaration, setting unset, no such project)."""
    decl = inst.manifest.get("tasks")
    if not decl:
        return None
    handle = str(((inst.manifest.get("settings") or {}).get(decl["project_setting"]) or {}).get("value") or "")
    handle = handle.strip().rpartition("/")[2]
    return task_services.get_workspace_project(inst.workspace_id, handle) if handle else None


def _scope(inst: AppInstallation, user) -> tuple[dict, AgentProject]:
    decl = inst.manifest.get("tasks")
    project = project_of(inst) if inst.enabled else None
    if decl is None or project is None or not task_services.may_read_project(user, project):
        raise Refused(f"the app '{inst.key}' acts on no tasks", status=404)
    return decl, project


def _tasks_q(inst: AppInstallation, project: AgentProject, user):
    return (AgentTask.objects.filter(task_services.task_read_q(user))
            .filter(project=project, workspace_id=inst.workspace_id))


def task_out(t: AgentTask) -> dict:
    """A task as an app page sees it: the work, never the machinery (`on_approve`, asks, links)."""
    return {
        "key": t.key, "title": t.title, "notes": t.notes, "status": t.status, "category": t.category,
        "due": t.due.isoformat() if t.due else None, "priority": t.priority,
        "labels": sorted(t.labels.values_list("name", flat=True)),
        "assignee": (f"agent:{t.agent.slug}" if t.agent_id else
                     (t.assignee_user.email if t.assignee_user_id else None)),
        "recurrence": t.recurrence, "template": t.template.key if t.template_id else None,
        "last_done": t.last_done.isoformat() if getattr(t, "last_done", None) else None,
        "url": t.url, "created_at": t.created_at.isoformat(), "updated_at": t.updated_at.isoformat(),
    }


def query(inst: AppInstallation, user, *, status: str = "", category: str = "", due_after=None, due_before=None,
          template: str = "", recurring: str = "", q: str = "", limit: int = 100) -> list[dict]:
    """The bound project's tasks the caller may read, for a holder of the declaration's `read` role."""
    decl, project = _scope(inst, user)
    if decl["read"] not in access.roles_for(user, inst):
        raise Refused(f"reading this app's tasks needs the '{decl['read']}' role")
    qs = task_services.filter_tasks(work.task_rows(_tasks_q(inst, project, user)), user=user, status=status,
                                    category=category, due_after=due_after, due_before=due_before, q=q,
                                    recurring=recurring, template=template)
    rows = recurrence.annotate_last_done(qs.order_by("due", "rank", "id")[:max(1, min(int(limit), MAX_QUERY))])
    return [task_out(t) for t in rows]


# --- acting -----------------------------------------------------------------------------------


def _text(name: str, value, *, required: bool = False, limit: int = 20_000) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    value = value.strip()
    if required and not value:
        raise ValueError(f"{name} is required")
    if len(value) > limit:
        raise ValueError(f"{name} is too long (at most {limit} characters)")
    return value


def _fields(inst: AppInstallation, decl: dict, spec: dict, input: dict) -> dict:
    """The declared fields of `input`, checked, as the task services take them."""
    out: dict = {}
    for f, v in input.items():
        if f == "title":
            out["title"] = _text("title", v, required=True, limit=300)
        elif f == "notes":
            out["notes"] = _text("notes", v)
        elif f == "due":
            try:
                out["due"] = dt.date.fromisoformat(v) if v else None
            except (TypeError, ValueError):
                raise ValueError("due is a date, YYYY-MM-DD") from None
        elif f == "priority":
            if isinstance(v, bool) or not isinstance(v, int):
                raise ValueError("priority is 0 (none) to 4 (low)")
            out["priority"] = v
        elif f == "labels":
            # Only the workspace's labels: an app files work under them, it never makes new ones.
            if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
                raise ValueError("labels is a list of the workspace's label names")
            out["labels"] = [lb.name for lb in work.resolve_labels(inst.workspace_id, v, create=False)]
        elif f == "recurrence":
            out["recurrence"] = recurrence.clean(v)
        elif f == "assignee_email":
            out["assignee"] = _assignee(inst, decl, v)
    return out


def _assignee(inst: AppInstallation, decl: dict, value) -> str:
    """A person who reads this app's tasks (holds its `read` role), by email; "" for nobody.
    Never an agent: an app files work for people, and an agent's work is started by people."""
    email = _text("assignee_email", value, limit=254).lower()
    if not email:
        return ""
    user = get_user_model().objects.filter(email__iexact=email).first()
    if user is None or Agent.objects.filter(user=user).exists() or not wsvc.is_member(user, inst.workspace_id) \
            or decl["read"] not in access.roles_for(user, inst):
        raise ValueError(f"{email} does not read this app's tasks, so the task cannot be theirs")
    return user.email


def act_task(inst: AppInstallation, user, name: str, key: str | None, input: dict | None, *,
             on_behalf_by_agent: Agent | None = None) -> tuple[dict, bool]:
    """Run the declared task action `name` as `user` (the module docstring's rules). Returns
    (the task as a page sees it, made: whether a task was made). Raises `Refused` (404 for
    anything outside the declaration, 403 for a role the caller lacks, 409 for a task in the wrong
    state) or ValueError (422: input the declaration does not take)."""
    decl, project = _scope(inst, user)
    spec = decl["actions"].get(name)
    if spec is None:
        raise Refused(f"no task action '{name}'", status=404)
    if spec["role"] not in access.roles_for(user, inst):
        raise Refused(f"'{name}' needs the '{spec['role']}' role")
    input = input or {}
    if not isinstance(input, dict):
        raise ValueError("input must be an object")
    do = spec["do"]
    takes = {"comment": {"body"}, "spawn": {"due"}}.get(do, set(spec["fields"]))
    unknown = sorted(set(input) - takes)
    if unknown:
        raise ValueError(f"'{name}' does not take: {', '.join(unknown)}")
    agent = on_behalf_by_agent
    by = f"{agent.slug} for {user.email}" if agent is not None else (user.email or user.get_username())
    if do == "create":
        if key:
            raise ValueError(f"'{name}' makes a task; it takes no key")
        return task_out(_create(inst, project, user, decl, spec, input, agent)), True
    task = _tasks_q(inst, project, user).filter(key__iexact=str(key or "").strip()).first() if key else None
    if task is None:
        raise Refused(f"no task {key or '(none named)'} in this app", status=404)
    try:
        if do == "patch":
            task = task_services.patch_task(task, _fields(inst, decl, spec, input), by_user=user, by_agent=agent)
        elif do == "reopen":
            if task.status not in _CLOSED:
                raise Refused(f"{task.key} is {task.status}, not finished — nothing to reopen", status=409)
            task = task_services.patch_task(task, {"status": AgentTask.TODO}, by_user=user, by_agent=agent)
        elif do in ("done", "comment"):
            action = "done" if do == "done" else "comment"
            body = _text("body", input.get("body"), required=True) if do == "comment" else ""
            task, _row, turns = task_services.act(task, action=action, comment=body, by=by, by_user=user,
                                                  actor_workspace_ids=set(), may_start_turns=False,
                                                  acting_agent=agent)
            if turns:  # pragma: no cover — done and comment start none; a regression must not ship one
                raise RuntimeError("an app task action started a turn")
        elif do == "spawn":
            try:
                due = dt.date.fromisoformat(str(input.get("due") or ""))
            except ValueError:
                raise ValueError("spawn takes due, a date (YYYY-MM-DD)") from None
            instance, made = recurrence.spawn(task, due, by=user, for_agent=agent)
            return task_out(instance), made
    except task_services.ClosedAskError as exc:
        raise Refused(str(exc), status=409) from exc
    except task_services.UnknownPersonError as exc:
        raise ValueError(str(exc)) from exc
    task.refresh_from_db()
    return task_out(task), False


@transaction.atomic
def _create(inst, project, user, decl, spec, input, agent) -> AgentTask:
    data = _fields(inst, decl, spec, input)
    if "title" not in data:
        raise ValueError("title is required")
    data.update(project=project.address, status=AgentTask.TODO)
    data.setdefault("assignee", "")  # nobody — never an agent
    try:
        [task] = task_services.create_tasks(None, [data], workspace=inst.workspace_id, by_user=user)
    except task_services.UnknownPersonError as exc:
        raise ValueError(str(exc)) from exc
    if task.agent_id is not None or task.project_id != project.pk:  # pragma: no cover — guarded above
        raise RuntimeError("an app may only file unassigned work into its own project")
    if agent is not None:
        TaskEvent.objects.filter(task=task, kind=TaskEvent.CREATED).update(by_agent=agent)
    return task

"""The status moves a PROJECT view may make on its own project's tasks: its `task_actions` declaration.

Beside starting a declared agent task (`declared_tasks.py`), this is the one other write a view has:
its code may call `canopy.tasks.act(key, action)` and the canopy page around the frame marks that task
done or reopens it, AS THE VIEWER, through the same writer a workspace app's task actions use
(`agents.services.finish_or_reopen`) — so it never starts a turn and history names the viewer.

* **Declared, and only on a project view.** `task_actions` is a subset of
  `agents.services.SCREEN_STATUS_ACTIONS` (done, reopen), snapshotted on every revision. A workspace
  view declaring one, or a project view moved to the workspace while it declares one, is refused.
* **Checked against its writer**, as declared agent tasks are: declaring one, or new code on a view
  that declares one, needs someone who could move any of the project's tasks themselves — an editor of
  the workspace, the clause of `agents.gates.may_edit_task` that covers every task. So a project lead
  who is only a workspace viewer keeps a view's title but cannot write code that closes work.
* **Run as the viewer** (`act`): the view's CURRENT declaration names the action (a narrowing applies
  at once), the task is the view's project's and readable by the viewer (else 404, the same as no such
  task), and the viewer may edit it by `may_edit_task` — the task page's own gate (403).
"""
from __future__ import annotations

from apps.agents import services as task_services
from apps.agents.gates import may_edit_task
from apps.agents.models import AgentTask
from apps.workspaces import permissions as perms

from .models import DynamicView

ACTIONS = task_services.SCREEN_STATUS_ACTIONS


class Refused(Exception):
    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def may_declare(user, workspace_id: str) -> str | None:
    """None when `user` may write a view that moves its project's tasks, else why not."""
    if getattr(user, "is_authenticated", False) and perms.can(user, workspace_id, perms.AGENT_WORK):
        return None
    return ("declaring task_actions needs the editor role in the view's workspace: the author must be "
            "able to change the project's tasks themselves")


def validate(workspace_id: str, project, declared, user) -> list[str]:
    """Normalise a view's `task_actions` (in ACTIONS' order), or raise ValueError naming what is wrong."""
    if declared in (None, ""):
        return []
    if not isinstance(declared, list) or not all(isinstance(a, str) for a in declared):
        raise ValueError(f"task_actions must be a list of action names: {', '.join(ACTIONS)}")
    unknown = sorted({a for a in declared if a not in ACTIONS})
    if unknown:
        raise ValueError(f"task_actions: no action {', '.join(repr(a) for a in unknown)} "
                         f"(a view may declare only {', '.join(ACTIONS)})")
    if not declared:
        return []
    if project is None:
        raise ValueError("task_actions are only for a project view: a view on the workspace has no "
                         "project whose tasks it could change")
    if why := may_declare(user, workspace_id):
        raise ValueError(why)
    return [a for a in ACTIONS if a in declared]


def act(request, view: DynamicView, key: str, action: str) -> AgentTask:
    """Run one declared status move on one task of the view's project, as the request's user.
    Raises Refused (404 outside the declaration or the project, 403 without edit rights on the task,
    409 for a task in the wrong state)."""
    if action not in (view.task_actions or []):
        raise Refused(f"this view does not declare the task action '{action}' (its task_actions)", 404)
    if view.project_id is None:  # pragma: no cover — validate never lets one be saved
        raise Refused("a workspace view changes no tasks", 404)
    user = request.user
    task = (AgentTask.objects.filter(task_services.task_read_q(user))
            .filter(project_id=view.project_id, workspace_id=view.workspace_id,
                    key__iexact=str(key or "").strip())
            .select_related("agent", "created_by_agent").first()) if key else None
    if task is None:
        raise Refused(f"no task {key or '(none named)'} in this view's project", 404)
    if not may_edit_task(request, task):
        raise Refused("changing a task requires the editor role, being its assignee, or being its agent", 403)
    try:
        task = task_services.finish_or_reopen(task, action, by_user=user)
    except task_services.ClosedAskError as exc:
        raise Refused(str(exc), 409) from exc
    task.refresh_from_db()
    return task

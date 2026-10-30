"""The ONE way a card is decided, whatever door it came through.

The page, a Slack button and the API all land here, which calls the board's own `services.act`
(so approving starts exactly the turns `on_approve` names, atomically, once) and writes down
which door it was. A second click on a decided card is a 409 saying who got there first —
never a second run of the work.
"""
from __future__ import annotations

from django.db import transaction

from apps.agents import services
from apps.agents.models import AgentTask
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from .models import ApprovalDecision

VIAS = ("web", "slack", "email", "push", "api")


class ApprovalError(Exception):
    """A decision canopy refuses; `status` is the HTTP status for the API."""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def decide(task: AgentTask, user, decision: str, *, comment: str = "", via: str = "web") -> AgentTask:
    if decision not in (ApprovalDecision.APPROVE, ApprovalDecision.DECLINE):
        raise ApprovalError("decision must be approve or decline", 422)
    via = via if via in VIAS else "api"
    if user is None or not getattr(user, "is_authenticated", False) or not perms.can(user, task.workspace_id, perms.READ):
        raise ApprovalError(f"task {task.key} not found", 404)
    agent = task.agent
    try:
        with transaction.atomic():
            task, _row, _turns = services.act(
                task, action=decision, comment=comment, by=user.email or user.get_username(), by_user=user,
                actor_workspace_ids=wsvc.user_workspace_slugs(user),
                may_start_turns=bool(agent is not None and perms.can(user, agent.workspace_id, perms.AGENT_WORK)),
                by_agent_itself=False,
            )
            ApprovalDecision.objects.create(task=task, user=user, decision=decision, via=via,
                                            comment=(comment or "").strip())
    except services.ClosedAskError as exc:
        raise ApprovalError(_already(task, str(exc)), 409) from exc
    except PermissionError as exc:
        raise ApprovalError(str(exc), 403) from exc
    except ValueError as exc:
        raise ApprovalError(str(exc), 422) from exc
    return task


def _already(task: AgentTask, fallback: str) -> str:
    from .cards import _decided, outcome_line

    decided = _decided(AgentTask.objects.get(pk=task.pk))
    return f"Already decided. {outcome_line({'decided': decided})}" if decided else fallback

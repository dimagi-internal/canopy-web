"""What leaving a workspace takes with it, for harness: registered into `apps.workspaces.departure`."""
from __future__ import annotations

from apps.workspaces import departure

from .models import RunnerAdmin, RunnerAssignment, RunnerEnrollment, WorkspaceRunnerOrder


def _runner_admins(user, left, by) -> int:
    return RunnerAdmin.objects.filter(user=user, runner__workspace_id__in=left).delete()[0]


def _runner_assignments(user, left, by) -> int:
    # Their box stops serving that tenant's agents. A runner holds an agent's
    # secrets and claimed turns; one owned by an outsider must not, even
    # disabled, sit on the list waiting for its owner to rejoin.
    return RunnerAssignment.objects.filter(runner__owner=user, agent__workspace_id__in=left).delete()[0]


def _workspace_orders(user, left, by) -> int:
    # Likewise out of those workspaces' default orders, which agents follow.
    return WorkspaceRunnerOrder.objects.filter(runner__owner=user, workspace_id__in=left).delete()[0]


def _runner_enrollments(user, left, by) -> int:
    # And their boxes are no longer enrolled there: rejoining must not quietly
    # put a box back on that tenant's work — re-enrolling is a decision.
    return RunnerEnrollment.objects.filter(runner__owner=user, workspace_id__in=left).delete()[0]


def register() -> None:
    departure.register("runner_admins", _runner_admins, order=20)
    departure.register("runner_assignments", _runner_assignments, order=50)
    departure.register("workspace_orders", _workspace_orders, order=60)
    departure.register("runner_enrollments", _runner_enrollments, order=70)

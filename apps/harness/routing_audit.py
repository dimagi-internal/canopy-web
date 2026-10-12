"""Before/after for routing writes, reported to the settings audit.

Every routing write (an agent's default runner order, its per-source and
per-person rules, a workspace's default order) deletes the affected rows and
re-creates them, and `RunnerAssignment` / `WorkspaceRunnerOrder` carry no author
or timestamp. The settings audit (`apps/events/audit.py`) records who made each
routing change and through what, the same as every other setting; this module
adds what only the handler knows — the rows before and after — via
`audit.note_change`. A save that changes nothing (re-sending a rule unchanged)
notes a no-op, and the audit then records nothing.

Origin, 2026-10-12: ACE carried a strict rule sending every API turn ada@
requested to one cloud box, and nobody could say who had set it.
"""
from __future__ import annotations


def _row(r) -> dict:
    return {
        "source": getattr(r, "source", ""),
        "actor": getattr(r, "actor", ""),
        "runner": r.runner.name,
        "runner_id": str(r.runner_id),
        "rank": r.rank,
        "enabled": r.enabled,
        "strict": getattr(r, "strict", False),
        "turn_mode": getattr(r, "turn_mode", ""),
    }


def agent_rows(agent, **scope) -> list[dict]:
    """The agent's RunnerAssignment rows in `scope`, as plain dicts in a stable
    order. Scope keywords are Django filter kwargs, plus `rules_only=True` for
    every non-default-order row (the wholesale rules replace's scope)."""
    from apps.harness.models import RunnerAssignment

    rules_only = scope.pop("rules_only", False)
    qs = RunnerAssignment.objects.filter(agent=agent, **scope)
    if rules_only:
        qs = qs.exclude(source="")
    return [_row(r) for r in qs.select_related("runner").order_by("source", "actor", "rank")]


def workspace_rows(workspace) -> list[dict]:
    from apps.harness.models import WorkspaceRunnerOrder

    return [
        _row(r) for r in WorkspaceRunnerOrder.objects.filter(workspace=workspace)
        .select_related("runner").order_by("rank")
    ]


def record_routing_change(request, *, workspace, target: str, scope: str,
                          before: list[dict], after: list[dict]) -> bool:
    """Report one routing change to the settings audit. Returns whether
    anything changed."""
    from apps.events import audit

    if not after:
        verb = "removed"
    elif not before:
        verb = "set"
    else:
        verb = "changed"
    who = getattr(getattr(request, "user", None), "email", "") or "someone"
    runners = ", ".join(dict.fromkeys(r["runner"] for r in after))
    summary = f"{who} {verb} {target} {scope}" + (f" → {runners}" if after else "")
    return audit.note_change(scope=f"{target} {scope}", before=before, after=after,
                             workspace=workspace, summary=summary)

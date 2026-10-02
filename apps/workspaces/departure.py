"""What leaving a workspace takes with it.

Removing a membership row used to be the whole of removal. Everything the
person had been granted *because* they were a member stayed behind — agent
admin rows, runner admin rows, chat participant rows, the GitHub token they
lent an agent, the agents they owned, and their own box's place on those
agents' runner lists. Most of those legs re-check membership at read time, so
the leftovers were dormant rather than live. But dormant is the problem:
`dimagi` is self-join, so a removed person clicks Join, lands as an editor,
and every one of those grants wakes up again. Removal you can undo yourself
is not removal.

So a departure is swept here, in one list, rather than by a receiver per app:
the point of the list is that it can be read in one place and checked against
what membership carries. Add to it when you add a grant that hangs off being
in a workspace.

Scope is every workspace the person is NO LONGER in after the change — the one
they left, and any below it they only reached as an inherited owner (removing,
or demoting, an org owner takes the divisions with it). A workspace they are
still in through another path keeps their grants.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


def workspaces_left(user, slug: str) -> list[str]:
    """`slug` and its descendants that `user` no longer belongs to."""
    from . import services

    candidates = [slug, *sorted(services.descendant_slugs({slug}))]
    return [s for s in candidates if not services.is_member(user, s)]


def sweep(user, slug: str, *, by=None) -> dict:
    """Revoke what `user` held in every workspace they have just left.

    Returns counts per kind, for tests and the Event. Runs inside the caller's
    transaction; the socket close is published on commit, and is best-effort.
    """
    from django.db import transaction

    from apps.agents.models import Agent, AgentAdmin, AgentDelegation
    from apps.canopy_sessions.models import SessionParticipant
    from apps.harness.models import RunnerAdmin, RunnerAssignment

    left = workspaces_left(user, slug)
    counts = {"agent_admins": 0, "runner_admins": 0, "session_participants": 0,
              "delegations": 0, "agents_unowned": 0, "runner_assignments": 0}
    if not left:
        return counts
    counts["agent_admins"] = AgentAdmin.objects.filter(
        user=user, agent__workspace_id__in=left).delete()[0]
    counts["runner_admins"] = RunnerAdmin.objects.filter(
        user=user, runner__workspace_id__in=left).delete()[0]
    counts["session_participants"] = SessionParticipant.objects.filter(
        user=user, session__workspace_id__in=left).delete()[0]
    counts["delegations"] = AgentDelegation.objects.filter(
        user=user, agent__workspace_id__in=left).delete()[0]
    # Their box stops serving that tenant's agents. A runner holds an agent's
    # secrets and claimed turns; one paired by an outsider must not, even
    # disabled, sit on the list waiting for its pairer to rejoin.
    counts["runner_assignments"] = RunnerAssignment.objects.filter(
        runner__paired_by=user, agent__workspace_id__in=left).delete()[0]
    orphaned = list(Agent.objects.filter(owner=user, workspace_id__in=left))
    for agent in orphaned:
        agent.owner = None
        agent.save(update_fields=["owner", "updated_at"])
    counts["agents_unowned"] = len(orphaned)
    if orphaned:
        _record_unowned(user, orphaned, by=by)
    transaction.on_commit(lambda: _close_sockets(user.pk, left))
    return counts


def _record_unowned(user, agents, *, by) -> None:
    """An agent losing its owner changes who it pushes and whose GitHub it uses,
    so it is said somewhere a workspace owner will look, not just done."""
    try:
        from apps.events import services as events

        actor = getattr(by, "email", None) or "a workspace owner"
        for agent in agents:
            events.record([{
                "source": "workspaces.members", "kind": "agent.owner_cleared", "level": "warn",
                "summary": (f"{agent.slug} has no owner: {user.email} left the workspace "
                            f"(removed by {actor}). Transfer it from the agent's Settings."),
                "payload": {"agent": agent.slug, "former_owner": user.email,
                            "by": getattr(by, "email", None)},
            }], workspace=agent.workspace)
    except Exception:  # noqa: BLE001 — bookkeeping must never fail the removal
        log.exception("could not record an agent losing its owner")


def _close_sockets(user_id: int, slugs: list[str]) -> None:
    """Ask every chat socket this person holds to re-check its access, so a
    session in a workspace they just left closes now rather than when they next
    act in it. Best-effort: a live socket also re-checks on every acting frame."""
    from apps.realtime import groups

    groups.publish(groups.chat_user_group(user_id),
                   {"type": "access.recheck", "workspaces": list(slugs)})

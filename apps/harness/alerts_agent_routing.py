"""Alert source `agent_cant_run`: an agent configured so that nothing can run it.

Configuration only, never liveness. Two conditions, both read from
`topology.build` so the alert agrees with what the Topology page shows:

- the agent has no runner to run on at all — no enabled route of its own and none
  inherited from its workspace's order;
- every runner its workspace order lists is dropped for it because it does not
  declare the agent's repo (`follows.missing_repo`), so nothing is left.

A routed runner that is merely offline, stale or down is NOT a configuration
problem and raises nothing; the runner's own alerts and the unclaimable-turns
source cover what happens when work actually waits.

One alert per agent (`agent:<slug>:routing`). Its fingerprint is the computed
reason (which of the two, and the runners involved), so it changes only when the
reason does. Fixing the routing makes the next reconcile drop the condition, which
resolves the alert; that is the close path, so the only closing action is the
shared dismiss. The fix itself is a link to the agent's Settings tab, offered to
whoever may edit its routing (the `agent.work` gate its routing routes use).
"""
from __future__ import annotations

import hashlib
from datetime import timedelta

from apps.agents.models import Agent
from apps.attention.alerts import DISMISS, Action, Raise
from apps.workspaces import permissions as perms
from apps.workspaces.models import Workspace

from . import topology

KIND = "agent_cant_run"
SEVERITY = "setup"
CONFIRM_WITHIN: timedelta | None = None
CLOSING_ACTION_IDS = ("dismiss",)


def settings_path(workspace: str, slug: str) -> str:
    """Where an agent's routing is edited (its Settings tab). App-relative."""
    return f"/w/{workspace}/agents/{slug}/settings"


def _join(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _reason(agent: dict, runners: dict) -> tuple[str, str] | None:
    """`(title, fingerprint)` if this agent cannot run, else None."""
    effective = [r for r in agent["routes"]
                 if r["enabled"] and not runners.get(r["runner_id"], {}).get("directed")]
    if effective:
        return None
    missing = ((agent["follows"] or {}).get("missing_repo")) or []
    if missing:
        names = sorted(runners[pk]["name"] for pk in missing if pk in runners)
        if names:
            verb = "doesn't" if len(names) == 1 else "don't"
            ids = ",".join(str(pk) for pk in sorted(missing))
            return (f"{agent['name']}: {_join(names)} {verb} have its repo", f"missing-repo:{ids}")
    return f"{agent['name']} has no runner to run on", "no-runner"


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    for root in Workspace.objects.filter(parent__isnull=True).order_by("slug"):
        topo = topology.build(root)
        runners = {r["id"]: r for r in topo["runners"]}
        for ws in topo["workspaces"]:
            for agent in ws["agents"]:
                found = _reason(agent, runners)
                if found is None:
                    continue
                title, reason = found
                slug = agent["slug"]
                out.append(Raise(
                    subject_key=f"agent:{slug}:routing",
                    workspace=ws["slug"],
                    title=title,
                    url=settings_path(ws["slug"], slug),
                    scopes=(f"agent:{slug}", f"workspace:{ws['slug']}"),
                    fingerprint=hashlib.sha256(reason.encode()).hexdigest(),
                ))
    return out


def _agent_of(alert):
    parts = alert.subject_key.split(":")
    if len(parts) != 3 or parts[0] != "agent" or parts[2] != "routing":
        return None
    return Agent.objects.filter(slug=parts[1], workspace_id=alert.workspace_id).first()


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    agent = _agent_of(alert)
    if agent is None or not perms.can(user, agent.workspace_id, perms.AGENT_WORK):
        return []
    return [Action("routing", "Fix routing", False, href=settings_path(agent.workspace_id, agent.slug)),
            DISMISS]


def perform(alert, user, action_id) -> None:
    # `routing` is a link and `dismiss` is the framework's: nothing runs here.
    raise ValueError(f"agent_cant_run has no handler for {action_id!r}")

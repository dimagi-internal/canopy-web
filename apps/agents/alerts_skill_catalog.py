"""Alert source `skill_catalog_empty`: an agent has a repository but no skill catalog.

The catalog is filled by the agent publishing it, or by canopy-web from what a runner
that may hold the agent reports (`skill_catalog`). An agent whose catalog is still
empty a day after it was created has neither: no runner has its plugin installed and
reporting, and it never published. Its admins are told, because the Skills tab, the
phone composer's commands and the default read profile's `answer-caller` entry all read
that catalog.

Resolves when the catalog has a skill (a runner reported, or the agent published).
"""
from __future__ import annotations

from datetime import timedelta

from apps.attention.alerts import DISMISS, Action, Raise

from .models import Agent

KIND = "skill_catalog_empty"
SEVERITY = "setup"
CONFIRM_WITHIN: timedelta | None = None
CLOSING_ACTION_IDS = ("open", "dismiss")
GRACE = timedelta(days=1)


def agent_path(workspace: str, slug: str) -> str:
    return f"/w/{workspace}/agents/{slug}"


def reconcile(now) -> list[Raise]:
    out: list[Raise] = []
    cutoff = now - GRACE
    agents = (Agent.objects.exclude(repo_url="").filter(created_at__lte=cutoff)
              .filter(skills__isnull=True).order_by("slug"))
    for agent in agents:
        out.append(Raise(
            subject_key=f"agent:{agent.slug}:skill-catalog",
            workspace=agent.workspace_id,
            title=f"{agent.name or agent.slug} has no skill catalog",
            url=agent_path(agent.workspace_id, agent.slug),
            scopes=(f"agent:{agent.slug}", f"workspace:{agent.workspace_id}"),
            body=("No runner that may hold this agent reports its plugin's skills, and the agent has "
                  "not published a catalog. Install its plugin on a runner it is routed to (the runner "
                  "reports it within minutes), or run `canopy agent skills` from its repo."),
            fingerprint="empty",
        ))
    return out


def _agent_of(alert):
    parts = alert.subject_key.split(":")
    if len(parts) != 3 or parts[0] != "agent" or parts[2] != "skill-catalog":
        return None
    return Agent.objects.filter(slug=parts[1], workspace_id=alert.workspace_id).first()


def actions(alert, user) -> list[Action]:
    if not getattr(user, "is_authenticated", False):
        return []
    agent = _agent_of(alert)
    if agent is None or not agent.is_admin(user):
        return []
    return [Action("open", "Open the agent", True, href=agent_path(agent.workspace_id, agent.slug)), DISMISS]


def perform(alert, user, action_id) -> None:
    raise ValueError(f"skill_catalog_empty has no handler for {action_id!r}")

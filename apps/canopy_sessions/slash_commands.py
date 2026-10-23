"""The slash commands the chat composer offers for one session.

Two sources, merged by name:

* what the session's RUNNER reported it can run (`RunnerSlashCommands`, sent by
  `canopy_runner.slash_commands` when its list changes) — the account's skills
  and commands, its enabled plugins', and what ACP announced;
* the agent's own LAUNCHABLE skills (`AgentSkill.launchable`), named as Claude
  Code names a plugin's skill (`<slug>:<name>`). These are offered even before
  a runner holds the session, and on a runner that has never reported.

Ordered for the composer, which keeps the order of whatever matches what was
typed: the agent's launchable skills, then the rest of its namespace, then
everything else by name.
"""
from __future__ import annotations

from apps.harness.models import RunnerSlashCommands

from .models import Session

AGENT, RUNNER = "agent", "runner"


def for_session(session: Session) -> list[dict]:
    binding = getattr(session, "runner_binding", None)
    runner_id = binding.runner_id if binding is not None else None
    reported = []
    if runner_id:
        reported = (RunnerSlashCommands.objects.filter(runner_id=runner_id)
                    .values_list("commands", flat=True).first()) or []
    by_name: dict[str, dict] = {}
    for c in reported:
        if isinstance(c, dict) and c.get("name"):
            by_name[c["name"]] = {
                "name": c["name"],
                "description": c.get("description") or "",
                "args_hint": c.get("args_hint") or "",
                "source": c.get("source") or RUNNER,
            }

    slug = session.agent.slug if session.agent_id else ""
    launchable: list[str] = []
    if slug:
        for skill in session.agent.skills.filter(launchable=True):
            name = f"{slug}:{skill.name}"
            row = by_name.get(name) or {"name": name, "description": skill.description}
            by_name[name] = {**row, "description": row.get("description") or skill.description,
                             "args_hint": skill.args_hint or row.get("args_hint") or "",
                             "source": AGENT}
            launchable.append(name)

    first = set(launchable)

    def rank(c: dict) -> tuple:
        if c["name"] in first:
            return (0, c["name"])
        if slug and c["name"].startswith(f"{slug}:"):
            return (1, c["name"])
        return (2, c["name"])

    return sorted(by_name.values(), key=rank)

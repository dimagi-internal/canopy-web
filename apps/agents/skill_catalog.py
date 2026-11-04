"""Fill an agent's skill catalog from what its runners report, so it never depends
on the agent remembering to publish it.

The catalog (`AgentSkill`) used to be written only by the agent itself
(`canopy agent skills`, `PUT /api/agents/{slug}/skills/`), best-effort at the end of
a manager-sync or turn. An agent that never ran that step (Trellis: 157 turns, an
empty catalog) had no catalog, and nothing said so.

Every runner already reports the slash commands its sessions can run
(`RunnerSlashCommands`, from the enabled plugins' `skills/*/SKILL.md`, named
`<plugin>:<skill>`), every few minutes and when the list changes. That is the
agent's own repo as a box installed it. This module turns those entries into
catalog rows, from the runners the agent may run on (`runner_may_hold_agent`).

A catalog the agent PUBLISHED keeps its rows: it carries notes and flags the report
does not. A catalog this module wrote is replaced as the reports change (a skill
added, renamed or removed in the repo follows within minutes of a box picking the
commit up). Rows keep their `launchable` / note by name.

`internal` (`user-invocable: false` in the file) is the one thing only a report
knows, so it is kept current on both kinds of catalog. An internal skill stays a
catalog row; only the `/` menu leaves it out (`canopy_sessions.slash_commands`).
"""
from __future__ import annotations

import logging
import re

from django.db import transaction

from apps.harness.models import RunnerSlashCommands

from .holding import runner_may_hold_agent
from .models import Agent, AgentSkill

logger = logging.getLogger(__name__)


#: A box reports a plugin's skill as `plugin` when it read the file, and as `acp` once a session on it has
#: announced that name, which wins the clash (`slash_commands.scan`). The `<plugin>:` prefix is what says whose.
REPORTED_FROM_A_PLUGIN = ("plugin", "acp")


def plugin_names(agent: Agent) -> list[str]:
    """The namespaces a box would report this agent's skills under: its slug, and
    its repo's name (a plugin is usually named after either)."""
    names = [agent.slug]
    m = re.match(r"^https://github\.com/[^/]+/([^/]+?)(?:\.git)?/?$", agent.repo_url or "")
    if m and m.group(1) not in names:
        names.append(m.group(1))
    return names


def reported_skills(agent: Agent) -> list[dict] | None:
    """The agent's skills as the freshest runner that may hold it reports them, or
    None when no such runner reports any."""
    prefixes = tuple(f"{n}:" for n in plugin_names(agent))
    rows = RunnerSlashCommands.objects.select_related("runner").order_by("-updated_at")
    for row in rows:
        found = [c for c in row.commands
                 if isinstance(c, dict) and c.get("source") in REPORTED_FROM_A_PLUGIN
                 and str(c.get("name", "")).startswith(prefixes)]
        if not found or not runner_may_hold_agent(row.runner, agent):
            continue
        return [{"name": c["name"].split(":", 1)[1], "description": c.get("description") or "",
                 "args_hint": c.get("args_hint") or "", "internal": bool(c.get("internal"))} for c in found]
    return None


def refresh(agent: Agent) -> int | None:
    """Bring the catalog in line with what runners report. Returns the new count,
    or None when the rows stand (nothing reported, or the agent's own published
    catalog, where only `internal` follows the report)."""
    items = reported_skills(agent)
    if items is None:
        return None
    with transaction.atomic():
        existing = {s.name: s for s in agent.skills.select_for_update()}
        wanted = {i["name"]: i for i in items}
        if any(s.source != AgentSkill.FROM_RUNNER for s in existing.values()):
            for name, i in wanted.items():
                row = existing.get(name)
                if row is not None and row.internal != i["internal"]:
                    row.internal = i["internal"]
                    row.save(update_fields=["internal", "updated_at"])
            return None
        agent.skills.exclude(name__in=wanted).delete()
        for name, i in wanted.items():
            row = existing.get(name)
            if row is None:
                AgentSkill.objects.create(agent=agent, name=name, description=i["description"],
                                          args_hint=i["args_hint"], internal=i["internal"],
                                          source=AgentSkill.FROM_RUNNER)
            elif (row.description, row.args_hint, row.internal) != (i["description"], i["args_hint"], i["internal"]):
                row.description, row.args_hint, row.internal = i["description"], i["args_hint"], i["internal"]
                row.save(update_fields=["description", "args_hint", "internal", "updated_at"])
        return len(wanted)


def refresh_all() -> dict[str, int]:
    """One pass over every agent; the off-request clock's job. Never raises."""
    done: dict[str, int] = {}
    for agent in Agent.objects.all():
        try:
            n = refresh(agent)
        except Exception:  # noqa: BLE001 — one agent must not stop the rest
            logger.exception("skill catalog refresh failed for %s", agent.slug)
            continue
        if n is not None:
            done[agent.slug] = n
    return done

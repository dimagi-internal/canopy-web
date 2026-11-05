"""Keep an agent's skill catalog in step with what its runners report, so it never
depends on the agent remembering to publish it.

The catalog (`AgentSkill`) is also written by the agent itself (`canopy agent
skills`, `PUT /api/agents/{slug}/skills/`), best-effort at the end of a manager-sync
or turn. An agent that never ran that step had no catalog, and one that stopped
running it kept an old one, and nothing said so.

Every runner already reports the slash commands its sessions can run
(`RunnerSlashCommands`, from the enabled plugins' `skills/*/SKILL.md`, named
`<plugin>:<skill>`), every few minutes and when the list changes. That is the
agent's own repo as a box installed it. This module merges those entries into the
catalog, from the runners the agent may run on (`runner_may_hold_agent`).

The merge is one rule for every catalog, with rows of two kinds:

* A row someone PUBLISHED keeps what only a publish can say: its note, `launchable`,
  link and args hint. Its description mirrors the skill's file, so it follows the
  report, unless the report's copy was cut at the box's limit.
* A row this module wrote (`source="runner"`) follows the report.

A skill the report lists and the catalog does not is added, and one the report no
longer lists is removed. `internal` (`user-invocable: false` in the file) is known
only to a report, so it always follows the report. An internal skill stays a catalog
row; only the `/` menu leaves it out (`canopy_sessions.slash_commands`).

What a report is not trusted to do:

* A box that is gone is not asked (`Runner.is_reachable`), nor one whose report does
  not name the agent at all, which is how a box without the plugin looks.
* A box holding an older copy of the plugin loses to one with a newer copy: the report
  with the newest install wins (`plugin_updated_at`, when Claude Code last updated
  that plugin on that box). `updated_at` says nothing here, since a box re-sends an
  unchanged list on every restart.
* A published catalog only takes a report that says when its copy was installed, and
  only if that is after the publish. Anything else could be older than what was
  published, so it can mark `internal` and nothing more.
* A report cut at its size limit removes nothing (the tail is missing, not gone), and
  neither does one that would remove most of a catalog of any size, which looks more
  like a broken install than a repo change. A publish, or the report recovering, ends it.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime

from django.db import transaction
from django.utils.dateparse import parse_datetime

from apps.harness.models import RunnerSlashCommands

from .holding import runner_may_hold_agent
from .models import Agent, AgentSkill

logger = logging.getLogger(__name__)


#: A box reports a plugin's skill as `plugin` when it read the file, and as `acp` once a session on it has
#: announced that name, which wins the clash (`slash_commands.scan`). The `<plugin>:` prefix is what says whose.
REPORTED_FROM_A_PLUGIN = ("plugin", "acp")

#: `SlashCommandsIn.commands` and the runner's `MAX_COMMANDS`: a report this long may have lost its tail.
REPORT_LIMIT = 1000
#: The runner's `MAX_DESCRIPTION`: a description this long was cut, so a longer published one is better.
DESCRIPTION_LIMIT = 300
#: Below this, one rename can be most of a catalog, and that is an ordinary change.
SMALL_CATALOG = 5


@dataclass
class Report:
    """One box's report, narrowed to one agent."""

    items: list[dict]
    #: When the box last updated the plugin. None from an older runner.
    installed_at: datetime | None
    complete: bool


def plugin_names(agent: Agent) -> list[str]:
    """The namespaces a box would report this agent's skills under: its slug, and
    its repo's name (a plugin is usually named after either)."""
    names = [agent.slug]
    m = re.match(r"^https://github\.com/[^/]+/([^/]+?)(?:\.git)?/?$", agent.repo_url or "")
    if m and m.group(1) not in names:
        names.append(m.group(1))
    return names


def _installed_at(entries: list[dict]) -> datetime | None:
    stamps = []
    for c in entries:
        try:
            stamps.append(parse_datetime(str(c.get("plugin_updated_at") or "")))
        except ValueError:
            continue
    return max((s for s in stamps if s is not None and s.tzinfo is not None), default=None)


def _description(entry: dict) -> str:
    """What the file says. ACP announces a plugin's skill as `(plugin) words`."""
    plugin = entry["name"].split(":", 1)[0]
    return re.sub(rf"^\({re.escape(plugin)}\)\s+", "", entry.get("description") or "")


def _reports() -> list[RunnerSlashCommands]:
    return list(RunnerSlashCommands.objects.select_related("runner", "runner__owner").order_by("-updated_at"))


def best_report(agent: Agent, reports: list[RunnerSlashCommands] | None = None) -> Report | None:
    """The agent's skills from the reachable runner that may hold it and installed
    its plugin last (a tie or no dates: the latest report), or None if there is none."""
    prefixes = tuple(f"{n}:" for n in plugin_names(agent))
    best: Report | None = None
    for row in (_reports() if reports is None else reports):
        found = [c for c in row.commands
                 if isinstance(c, dict) and c.get("source") in REPORTED_FROM_A_PLUGIN
                 and str(c.get("name", "")).startswith(prefixes)]
        if not found or not row.runner.is_reachable or not runner_may_hold_agent(row.runner, agent):
            continue
        installed_at = _installed_at(found)
        if best is not None and (installed_at is None or (best.installed_at and installed_at <= best.installed_at)):
            continue
        # In a dated report, an entry with no date is ACP's memory of a file now gone.
        on_disk = found if installed_at is None else [c for c in found if c.get("plugin_updated_at")]
        best = Report(
            items=[{"name": c["name"].split(":", 1)[1], "description": _description(c),
                    "args_hint": c.get("args_hint") or "", "internal": bool(c.get("internal"))} for c in on_disk],
            installed_at=installed_at, complete=len(row.commands) < REPORT_LIMIT)
    return best


def _may_change_rows(report: Report, published: list[AgentSkill]) -> bool:
    """A published catalog is only changed by a copy installed after the publish."""
    if not published:
        return True
    if report.installed_at is None:
        return False
    return report.installed_at >= max(s.created_at for s in published)


def _may_remove(agent: Agent, report: Report, existing: int, gone: int) -> bool:
    if not gone or not report.complete:
        return False
    if existing > SMALL_CATALOG and gone * 2 > existing:
        logger.warning("skill catalog: %s kept %d of %d rows a report no longer lists", agent.slug, gone, existing)
        return False
    return True


def _merge_row(row: AgentSkill, item: dict) -> None:
    if row.source == AgentSkill.FROM_RUNNER:
        description, args_hint = item["description"], item["args_hint"]
    else:
        reported = item["description"]
        description = reported if reported and len(reported) < DESCRIPTION_LIMIT else row.description
        args_hint = row.args_hint or item["args_hint"]
    if (row.description, row.args_hint, row.internal) != (description, args_hint, item["internal"]):
        row.description, row.args_hint, row.internal = description, args_hint, item["internal"]
        row.save(update_fields=["description", "args_hint", "internal", "updated_at"])


def refresh(agent: Agent, reports: list[RunnerSlashCommands] | None = None) -> int | None:
    """Bring the catalog in line with what runners report. Returns the new count, or
    None when its rows stand (nothing reported, or a report too old to change a
    published catalog, where only `internal` follows it)."""
    report = best_report(agent, reports)
    if report is None:
        return None
    wanted = {i["name"]: i for i in report.items}
    with transaction.atomic():
        existing = {s.name: s for s in agent.skills.select_for_update()}
        published = [s for s in existing.values() if s.source != AgentSkill.FROM_RUNNER]
        if not _may_change_rows(report, published):
            for name, i in wanted.items():
                row = existing.get(name)
                if row is not None and row.internal != i["internal"]:
                    row.internal = i["internal"]
                    row.save(update_fields=["internal", "updated_at"])
            return None
        gone = [name for name in existing if name not in wanted]
        if _may_remove(agent, report, len(existing), len(gone)):
            agent.skills.filter(name__in=gone).delete()
        for name, i in wanted.items():
            row = existing.get(name)
            if row is None:
                AgentSkill.objects.create(agent=agent, name=name, description=i["description"],
                                          args_hint=i["args_hint"], internal=i["internal"],
                                          source=AgentSkill.FROM_RUNNER)
            else:
                _merge_row(row, i)
        return agent.skills.count()


def refresh_all() -> dict[str, int]:
    """One pass over every agent; the off-request clock's job. Never raises."""
    done: dict[str, int] = {}
    reports = _reports()  # ~120 KB each: read once, not once per agent
    for agent in Agent.objects.all():
        try:
            n = refresh(agent, reports)
        except Exception:  # noqa: BLE001 — one agent must not stop the rest
            logger.exception("skill catalog refresh failed for %s", agent.slug)
            continue
        if n is not None:
            done[agent.slug] = n
    return done

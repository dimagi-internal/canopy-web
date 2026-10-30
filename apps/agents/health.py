"""The health of a workspace and every workspace below it: what is not set up right.

Composes the answers that already exist rather than keeping a second opinion:
each agent's row IS its setup checklist (`setup.setup_steps`) plus its vault
verdict (`vault_health.vault_health`). So the dashboard, the agent's Settings checklist
and the `agent_setup` MCP tool always agree.

Facts and states only — no "urgent" ranking (objective data, no verdicts). Every
problem carries its fix: the step's link when the viewer may follow it
(`can_fix`: the agent's owner or one of its admins, the bar its secrets and
routing are held to), otherwise the owner to send it to.
"""
from __future__ import annotations

from collections.abc import Callable

from django.db.models import Count

from apps.canopy_sessions import access, staleness
from apps.canopy_sessions import transcript_read as session_transcript_read
from apps.canopy_sessions.models import Session
from apps.events import analytics
from apps.harness import device_keys, topology
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import access as access_rule
from . import interface as iface_mod
from .credentials.vault_health import vault_health
from .models import Agent, AgentProject, AgentTask
from .services import waiting_q
from .setup import DONE, OPTIONAL, setup_steps
from .tree import agents_in_tree


def setup_row(agent: Agent, viewer, standings: access_rule.Standings, facts) -> dict:
    """One agent's checklist and vault verdict: the costly part of its row (a few
    dozen reads each), which the page asks for after the table is up
    (`setup_rows`). `standings` holds the viewer and `facts`
    (`device_keys.serving_facts`) the fleet, each read once for every agent."""
    vault = vault_health(agent, facts)
    steps = setup_steps(agent, viewer_is_admin=standings.is_admin(viewer, agent), vault_state=vault, facts=facts)
    return {"slug": agent.slug, "vault": vault, "steps": steps,
            "left": sum(1 for s in steps if s["status"] not in (DONE, OPTIONAL))}


def setup_rows(agents: list[Agent], viewer, standings: access_rule.Standings | None = None) -> list[dict]:
    standings = standings or access_rule.Standings([viewer], agents)
    facts = device_keys.serving_facts(agents)
    return [setup_row(a, viewer, standings, facts) for a in agents]


def setup_of(root: Workspace, viewer, visible: Callable[[str], bool], slugs: set[str]) -> list[dict]:
    """`setup_rows` of the named agents of the tree; any other slug is left out."""
    named = [a for a in agents_in_tree(wsvc.subtree(root, visible), "owner", "workspace") if a.slug in slugs]
    return setup_rows(named, viewer)


def agent_row(agent: Agent, viewer, last_turns: dict, counts: dict, sessions: dict,
              standings: access_rule.Standings, setup: dict | None) -> dict:
    """One agent's row; `setup` is its `setup_row`, or None to leave the
    checklist for a second read."""
    can_fix = standings.is_admin(viewer, agent)
    last = last_turns.get(agent.slug) or {}
    c = counts.get(agent.pk) or {}
    return {
        "slug": agent.slug,
        "name": agent.name,
        "workspace": agent.workspace_id,
        "owner_email": agent.owner.email if agent.owner_id else "",
        "can_fix": can_fix,
        "email": agent.email,
        "repo_url": agent.repo_url,
        "turn_mode": agent.turn_mode,
        "has_interface": iface_mod.published(agent.interface or {}),
        "capabilities": len((agent.interface or {}).get("capabilities") or {}),
        "full_access": [str(c) for c in ((agent.interface or {}).get("full") or [])],
        "skills": c.get("skills", 0),
        "schedules": c.get("schedules", 0),
        "projects": c.get("projects", 0),
        "open_tasks": c.get("open_tasks", 0),
        "waiting_tasks": c.get("waiting_tasks", 0),
        "active_sessions": sessions.get(agent.pk, {}).get("active", 0),
        "last_session_at": sessions.get(agent.pk, {}).get("last_at"),
        "recent_sessions": sessions.get(agent.pk, {}).get("recent", []),
        "created_at": agent.created_at,
        "last_turn_at": last.get("last_at"),
        "turns_30d": last.get("n", 0),
        "done_30d": last.get("done", 0),
        "failed_30d": last.get("failed", 0),
        "people_30d": last.get("people", 0),
        "ran_on": last.get("runners", []),
        "vault": setup["vault"] if setup else None,
        "steps": setup["steps"] if setup else None,
        "left": setup["left"] if setup else None,
    }


def _counts(agents: list[Agent]) -> dict[int, dict]:
    """Per agent, in one query: skills, schedules, active projects, open tasks
    (`AgentTask.LIVE_STATUSES`) and open tasks waiting on a person
    (`services.waiting_q`) — the definitions the agent's own pages count with.

    One grouped count per kind: counting them all in one query joins every
    agent's skills × schedules × projects × tasks before counting, which took a
    second on labs with a dozen agents and grows with each of them."""
    ids = [a.pk for a in agents]
    out: dict[int, dict] = {i: {} for i in ids}
    kinds = {
        "skills": Agent.skills.rel.related_model.objects.all(),
        # Cron triggers only: the column says "schedules" (an inbound-mail trigger is not one).
        "schedules": Agent.schedules.rel.related_model.objects.filter(kind="cron"),
        "projects": AgentProject.objects.filter(status=AgentProject.ACTIVE),
        "open_tasks": AgentTask.objects.filter(status__in=AgentTask.LIVE_STATUSES),
        "waiting_tasks": AgentTask.objects.filter(waiting_q()),
    }
    for kind, rows in kinds.items():
        for agent_id, n in (rows.filter(agent_id__in=ids).order_by().values("agent_id")
                            .annotate(n=Count("pk")).values_list("agent_id", "n")):
            out[agent_id][kind] = n
    return out


#: How many of an agent's latest sessions a row lists.
RECENT_SESSIONS = 3


def _sessions(agents: list[Agent], viewer) -> dict[int, dict]:
    """Per agent: how many sessions are open (`staleness.active`, the chat list's
    `state=active`), when any session last did something (`services.with_activity`,
    the list's rule) — both over ALL its sessions, as counts — and its latest
    sessions, only those the viewer may open (`access.readable_sessions`): showing
    a title is showing the chat, and opening one is never a grant."""

    ids = [a.pk for a in agents]
    out: dict[int, dict] = {i: {"active": 0, "last_at": None, "recent": []} for i in ids}
    mine = Session.objects.filter(agent_id__in=ids)
    open_ids = set(staleness.active(mine).values_list("pk", flat=True))
    readable = set(access.readable_sessions(viewer).filter(agent_id__in=ids).values_list("pk", flat=True))
    # One pass, newest first: the first row per agent is its last activity (over
    # every session); the readable ones fill its recent list.
    rows = session_transcript_read.with_activity(mine).order_by("-_activity").values(
        "pk", "agent_id", "workspace_id", "title", "_activity")
    for s in rows:
        row = out[s["agent_id"]]
        if row["last_at"] is None:
            row["last_at"] = s["_activity"]
        if s["pk"] in open_ids:
            row["active"] += 1
        if s["pk"] in readable and len(row["recent"]) < RECENT_SESSIONS:
            row["recent"].append({"id": str(s["pk"]), "workspace": s["workspace_id"], "title": s["title"],
                                  "at": s["_activity"], "active": s["pk"] in open_ids})
    return out


def build(root: Workspace, viewer, visible: Callable[[str], bool] = lambda _s: True,
          setup: bool = True) -> dict:
    """The tree's health. `setup=False` leaves out each agent's checklist and
    vault verdict and the totals counted from them (None): those cost a few dozen
    reads per agent, so the page shows the rest first and asks `setup_rows` for
    them a few agents at a time."""
    tree = wsvc.subtree(root, visible)
    agents = agents_in_tree(tree, "owner", "workspace")

    last_turns, counts = analytics.agent_turns([a.slug for a in agents], days=30), _counts(agents)
    sessions = _sessions(agents, viewer)
    standings = access_rule.Standings([viewer], agents)
    setups = {r["slug"]: r for r in setup_rows(agents, viewer, standings)} if setup else {}
    rows = [agent_row(a, viewer, last_turns, counts, sessions, standings, setups.get(a.slug)) for a in agents]
    per_ws: dict[str, int] = {}
    for r in rows:
        per_ws[r["workspace"]] = per_ws.get(r["workspace"], 0) + 1
    states = [r["vault"]["state"] for r in rows if r["vault"]]

    runners = topology.build(root, visible, viewer)["runners"]
    return {
        "root": root.slug,
        "workspaces": [{
            "slug": ws.slug, "name": ws.display_name, "depth": depth, "parent": ws.parent_id or "",
            "agents": per_ws.get(ws.slug, 0),
        } for ws, depth in tree],
        "agents": rows,
        "runners": runners,
        "totals": {
            "agents": len(rows),
            "used_30d": sum(1 for r in rows if r["turns_30d"] or r["people_30d"]),
            **({
                "set_up": sum(1 for r in rows if not r["left"]),
                "needs_setup": sum(1 for r in rows if r["left"]),
                "vault_canopy_web": states.count("canopy-web"),
                "vault_moving": states.count("moving"),
                "vault_none": states.count("none"),
            } if setup else {}),
            "runners": len(runners),
            "runners_online": sum(1 for r in runners if r["status"] == "online"),
        },
    }

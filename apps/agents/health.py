"""The health of a workspace and every workspace below it: what is not set up right.

Composes the answers that already exist rather than keeping a second opinion:
each agent's row IS its setup checklist (`setup.setup_steps`) plus its vault
verdict (`vault_health.vault_health`); each workspace's shared vault is
`Workspace.shared_vault_source`. So the dashboard, the agent's Settings checklist
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


def _shared_vault(ws: Workspace) -> dict:
    src = ws.shared_vault_source()
    return {
        "vault": src.shared_op_vault if src is not None else "",
        "key_set": bool(src.shared_op_sa_token_enc) if src is not None else False,
        "inherited_from": src.slug if src is not None and src.slug != ws.slug else "",
    }


def agent_row(agent: Agent, viewer, last_turns: dict, counts: dict, sessions: dict,
              standings: access_rule.Standings, facts) -> dict:
    """One agent's row. `standings` holds the viewer and `facts`
    (`device_keys.serving_facts`) the fleet, each read once for every agent."""
    can_fix = standings.is_admin(viewer, agent)
    vault = vault_health(agent, facts)
    steps = setup_steps(agent, viewer_is_admin=can_fix, vault_state=vault, facts=facts)
    left = [s for s in steps if s["status"] not in (DONE, OPTIONAL)]
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
        "vault": vault,
        "steps": steps,
        "left": len(left),
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
        "schedules": Agent.schedules.rel.related_model.objects.all(),
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


def build(root: Workspace, viewer, visible: Callable[[str], bool] = lambda _s: True) -> dict:
    tree = wsvc.subtree(root, visible)
    agents = agents_in_tree(tree, "owner", "workspace")

    last_turns, counts = analytics.agent_turns([a.slug for a in agents], days=30), _counts(agents)
    sessions = _sessions(agents, viewer)
    standings, facts = access_rule.Standings([viewer], agents), device_keys.serving_facts(agents)
    rows = [agent_row(a, viewer, last_turns, counts, sessions, standings, facts) for a in agents]
    per_ws: dict[str, int] = {}
    for r in rows:
        per_ws[r["workspace"]] = per_ws.get(r["workspace"], 0) + 1
    states = [r["vault"]["state"] for r in rows]

    runners = topology.build(root, visible, viewer)["runners"]
    return {
        "root": root.slug,
        "workspaces": [{
            "slug": ws.slug, "name": ws.display_name, "depth": depth, "parent": ws.parent_id or "",
            "agents": per_ws.get(ws.slug, 0), "shared_vault": _shared_vault(ws),
        } for ws, depth in tree],
        "agents": rows,
        "runners": runners,
        "totals": {
            "agents": len(rows),
            "set_up": sum(1 for r in rows if not r["left"]),
            "needs_setup": sum(1 for r in rows if r["left"]),
            "used_30d": sum(1 for r in rows if r["turns_30d"] or r["people_30d"]),
            "vault_canopy_web": states.count("canopy-web"),
            "vault_moving": states.count("moving"),
            "vault_1password": states.count("1password"),
            "vault_none": states.count("none"),
            "runners": len(runners),
            "runners_online": sum(1 for r in runners if r["status"] == "online"),
        },
    }

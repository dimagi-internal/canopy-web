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

from django.db.models import Count, Max

from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import interface as iface_mod
from .models import Agent
from .setup import DONE, OPTIONAL, setup_steps
from .tree import agents_in_tree
from .vault_health import vault_health


def _shared_vault(ws: Workspace) -> dict:
    src = ws.shared_vault_source()
    return {
        "vault": src.shared_op_vault if src is not None else "",
        "key_set": bool(src.shared_op_sa_token_enc) if src is not None else False,
        "inherited_from": src.slug if src is not None and src.slug != ws.slug else "",
    }


def agent_row(agent: Agent, viewer, last_turns: dict, counts: dict) -> dict:
    steps = setup_steps(agent)
    left = [s for s in steps if s["status"] not in (DONE, OPTIONAL)]
    last = last_turns.get(agent.pk) or {}
    c = counts.get(agent.pk) or {}
    return {
        "slug": agent.slug,
        "name": agent.name,
        "workspace": agent.workspace_id,
        "owner_email": agent.owner.email if agent.owner_id else "",
        "can_fix": agent.is_admin(viewer),
        "email": agent.email,
        "repo_url": agent.repo_url,
        "turn_mode": agent.turn_mode,
        "has_interface": iface_mod.published(agent.interface or {}),
        "skills": c.get("skills", 0),
        "schedules": c.get("schedules", 0),
        "created_at": agent.created_at,
        "last_turn_at": last.get("at"),
        "turns_30d": last.get("n", 0),
        "vault": vault_health(agent),
        "steps": steps,
        "left": len(left),
    }


def _counts(agents: list[Agent]) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for rel, key in (("skills", "skills"), ("schedules", "schedules")):
        for row in (Agent.objects.filter(pk__in=[a.pk for a in agents]).order_by()
                    .annotate(n=Count(rel)).values("pk", "n")):
            out.setdefault(row["pk"], {})[key] = row["n"]
    return out


def _last_turns(agents: list[Agent]) -> dict[int, dict]:
    """Latest turn and 30-day count per agent, both under `agent_turns_q`'s
    definition (agent turns AND turns on its sessions)."""
    import datetime as dt

    from django.utils import timezone

    from apps.harness.models import Turn

    since = timezone.now() - dt.timedelta(days=30)
    out: dict[int, dict] = {}
    ids = [a.pk for a in agents]
    # order_by() clears any default ordering, which would otherwise join the GROUP BY.
    direct = Turn.objects.filter(agent_id__in=ids).order_by().values("agent_id")
    via_session = (Turn.objects.filter(agent__isnull=True, chat_session__agent_id__in=ids)
                   .order_by().values("chat_session__agent_id"))
    for qs, key in ((direct, "agent_id"), (via_session, "chat_session__agent_id")):
        for row in qs.annotate(at=Max("created_at")):
            cur = out.setdefault(row[key], {"at": None, "n": 0})
            if cur["at"] is None or (row["at"] and row["at"] > cur["at"]):
                cur["at"] = row["at"]
        for row in qs.filter(created_at__gte=since).annotate(n=Count("id")):
            out.setdefault(row[key], {"at": None, "n": 0})["n"] += row["n"]
    return out


def build(root: Workspace, viewer, visible: Callable[[str], bool] = lambda _s: True) -> dict:
    tree = wsvc.subtree(root, visible)
    agents = agents_in_tree(tree, "owner", "workspace")
    last_turns, counts = _last_turns(agents), _counts(agents)
    rows = [agent_row(a, viewer, last_turns, counts) for a in agents]
    per_ws: dict[str, int] = {}
    for r in rows:
        per_ws[r["workspace"]] = per_ws.get(r["workspace"], 0) + 1
    states = [r["vault"]["state"] for r in rows]
    return {
        "root": root.slug,
        "workspaces": [{
            "slug": ws.slug, "name": ws.display_name, "depth": depth, "parent": ws.parent_id or "",
            "agents": per_ws.get(ws.slug, 0), "shared_vault": _shared_vault(ws),
        } for ws, depth in tree],
        "agents": rows,
        "totals": {
            "agents": len(rows),
            "set_up": sum(1 for r in rows if not r["left"]),
            "needs_setup": sum(1 for r in rows if r["left"]),
            "vault_canopy_web": states.count("canopy-web"),
            "vault_moving": states.count("moving"),
            "vault_1password": states.count("1password"),
            "vault_none": states.count("none"),
        },
    }

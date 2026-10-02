"""The agent topology of a workspace subtree: which agent can make which other
agent do what.

An agent sends another agent work with its OWN canopy login (`Agent.user`) —
`canopy agent dispatch`, an MCP call, a chat. Since #986 that login is not the
sending agent to anyone but itself: to agent B it is an ordinary person, and
gets whatever B gives that person — B's owner or an admin get B whole, a
member gets what B's published interface lets members reach, someone outside
B's workspace gets nothing. So "can Ada send Hal work?" is "what does Hal give
ada@dimagi-ai.com?", which was answerable only by reading Hal's interface,
Hal's admin list and the workspace's members side by side — and the answer
for Ada was "the `ask` capability", which needs an email thread, so every
direct Ada→Hal dispatch failed for a week while each screen looked fine.

Every cell is decided by the predicates a real turn is (`access._member_access`,
`Agent.is_admin`), for a request made signed in with a token — verified by
construction. A board card someone approves is a different path (it runs as
canopy itself, `SYSTEM`) and is not what this shows.

Making A's login an admin of B is the one-click fix, and it is TRANSITIVE: anyone
with A's whole profile can then steer B through A. `full_people` is there so the
grant can say who that is before it is made.
"""
from __future__ import annotations

from collections.abc import Callable

from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc
from apps.workspaces.models import Workspace

from . import access
from . import interface as iface_mod
from .models import Agent, AgentAdmin

#: Why an edge has the access it has.
OWNER, WS_OWNER, ADMIN, FULL_RULE, NO_INTERFACE, CAPABILITIES, NOTHING, NOT_MEMBER, NO_LOGIN = (
    "owner", "workspace-owner", "admin", "full-rule", "no-interface", "capabilities",
    "nothing-offered", "not-member", "no-login",
)


def _edge(src: Agent, dst: Agent, admin_ids: set[int], viewer) -> dict:
    login = src.user
    out = {"source": src.slug, "target": dst.slug, "access": "none", "basis": NO_LOGIN,
           "capabilities": [], "full_rule": None, "explicit_admin": False,
           "can_grant": False, "can_revoke": False}
    if login is None:
        return out
    may_manage = (dst.owner_id == getattr(viewer, "pk", None)
                  or perms.can(viewer, dst.workspace_id, perms.OWN))
    role = wsvc.member_role(login, dst.workspace_id)  # a question about the sending agent's login
    if role is None:
        out["basis"] = NOT_MEMBER
        return out
    explicit = login.pk in admin_ids
    out["explicit_admin"] = explicit
    if dst.owner_id == login.pk:
        out.update(access="full", basis=OWNER)
    elif perms.role_allows(role, perms.OWN):
        out.update(access="full", basis=WS_OWNER)
    elif explicit:
        out.update(access="full", basis=ADMIN, can_revoke=may_manage)
    else:
        level, caps, rule = access._member_access(login.email or "", dst.interface or {})
        if level == "full":
            out.update(access="full", basis=FULL_RULE if rule else NO_INTERFACE, full_rule=rule)
        elif level == "confined":
            out.update(access="confined", basis=CAPABILITIES, capabilities=caps)
        else:
            out["basis"] = NOTHING
        out["can_grant"] = may_manage
    return out


def build(root: Workspace, viewer, visible: Callable[[str], bool] = lambda _s: True) -> dict:
    tree = wsvc.subtree(root, visible)
    slugs = {ws.slug for ws, _ in tree}
    agents = list(
        Agent.objects.filter(workspace_id__in=slugs).select_related("user", "owner").order_by("name")
    )
    admins: dict[int, set[int]] = {}
    for agent_id, user_id in AgentAdmin.objects.filter(agent__in=agents).values_list("agent_id", "user_id"):
        admins.setdefault(agent_id, set()).add(user_id)

    by_ws: dict[str, list[Agent]] = {}
    for a in agents:
        by_ws.setdefault(a.workspace_id, []).append(a)
    ordered = [a for ws, _ in tree for a in by_ws.get(ws.slug, [])]

    agent_rows = []
    for a in ordered:
        iface = a.interface or {}
        full = [r["email"] for r in access.roster(a) if r["access"] == "full"]
        agent_rows.append({
            "slug": a.slug,
            "name": a.name,
            "workspace": a.workspace_id,
            "owner_email": a.owner.email if a.owner_id else None,
            "login_email": a.user.email if a.user_id else None,
            "login_user_id": a.user_id,
            "interface_published": iface_mod.published(iface),
            "full_people": sorted(e for e in full if e),
        })

    edges = [
        _edge(src, dst, admins.get(dst.pk, set()), viewer)
        for src in ordered for dst in ordered if src.pk != dst.pk
    ]
    return {
        "root": root.slug,
        "workspaces": [
            {"slug": ws.slug, "display_name": ws.display_name, "depth": depth}
            for ws, depth in tree
        ],
        "agents": agent_rows,
        "edges": edges,
    }


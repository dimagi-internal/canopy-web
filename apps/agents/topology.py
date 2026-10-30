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

Every cell is decided by THE rule a real turn is (`access.decide`, see
docs/architecture/access.md), for a request made signed in with a token —
verified by construction. A login that is a workspace EDITOR of B reaches B
whole but only in manual mode (basis `editor`); making it an admin lifts that. A board card someone approves is a different path (it runs as
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
from .models import Agent
from .tree import agents_in_tree

#: Why an edge has the access it has. `editor`: full, but manual only.
#: `no-interface`: a viewer, refused because the target published no interface.
OWNER, WS_OWNER, ADMIN, EDITOR, NO_INTERFACE, CAPABILITIES, NOTHING, NOT_MEMBER, NO_LOGIN = (
    "owner", "workspace-owner", "admin", "editor", "no-interface", "capabilities",
    "nothing-offered", "not-member", "no-login",
)


def _edge(src: Agent, dst: Agent, standings: access.Standings, may_manage: bool) -> dict:
    """One ordered pair's cell, from what `standings` read about the sending
    agent's login (`may_manage`: the viewer owns `dst` or its workspace)."""
    login = src.user
    out = {"source": src.slug, "target": dst.slug, "access": "none", "basis": NO_LOGIN,
           "capabilities": [], "explicit_admin": False,
           "can_grant": False, "can_revoke": False}
    if login is None:
        return out
    st = standings.of(login, dst)
    role = st.role
    if role is None:
        out["basis"] = NOT_MEMBER
        return out
    explicit = bool(st.granted)
    out["explicit_admin"] = explicit
    if dst.owner_id == login.pk:
        out.update(access="full", basis=OWNER)
    elif perms.role_allows(role, perms.OWN):
        out.update(access="full", basis=WS_OWNER)
    elif explicit:
        out.update(access="full", basis=ADMIN, can_revoke=may_manage)
    else:
        d = access.decide(dst, login, verified=True, standing=st)
        iface = dst.interface or {}
        if d.access == access.FULL:
            out.update(access="full", basis=EDITOR)
        elif d.access == access.CONFINED:
            caps = sorted(n for n, c in (iface.get("capabilities") or {}).items()
                          if iface_mod.role_opens(d.agent_role, c))
            out.update(access="confined", basis=CAPABILITIES, capabilities=caps)
        else:
            out["basis"] = NOTHING if iface_mod.published(iface) else NO_INTERFACE
        out["can_grant"] = may_manage
    return out


#: The edges that let one agent RUN another: its login owns it, owns its
#: workspace, or is one of its admins — not the editor tier (manual only) and not
#: a confined interface. What a work group's leader needs over every member.
RUNS = frozenset({OWNER, WS_OWNER, ADMIN})


def _may_manage(dst: Agent, viewer, owned: set[str]) -> bool:
    return dst.owner_id == getattr(viewer, "pk", None) or dst.workspace_id in owned


def edge(src: Agent, dst: Agent, viewer=None) -> dict:
    """One ordered pair's cell, exactly as `build` computes it."""
    standings = access.Standings([src.user] if src.user_id else [], [dst])
    owned = perms.slugs_with(viewer, perms.OWN, within={dst.workspace_id})
    return _edge(src, dst, standings, _may_manage(dst, viewer, owned))


def can_run(src: Agent, dst: Agent) -> bool:
    """Does `src`'s login hold an owner/admin edge over `dst`?"""
    return src.pk == dst.pk or edge(src, dst)["basis"] in RUNS


def build(root: Workspace, viewer, visible: Callable[[str], bool] = lambda _s: True) -> dict:

    tree = wsvc.subtree(root, visible)
    ordered = agents_in_tree(tree, "user", "owner")

    # Every fact the cells are decided from, read once: each workspace's members,
    # then the roles and agent grants of everyone judged — the agents' logins
    # (every edge) and the members (every roster). Each cell is then `decide`
    # over these, with no read of its own; the per-pair reads made this N² queries.
    members = {slug: wsvc.effective_memberships(slug) for slug in {a.workspace_id for a in ordered}}
    people = {a.user for a in ordered if a.user_id} | {m.user for ms in members.values() for m in ms}
    standings = access.Standings(people, ordered)
    owned = perms.slugs_with(viewer, perms.OWN, within={ws.slug for ws, _ in tree})

    agent_rows = []
    for a in ordered:
        iface = a.interface or {}
        full = [r["email"] for r in access.roster(a, members[a.workspace_id], standings) if r["access"] == "full"]
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
        _edge(src, dst, standings, _may_manage(dst, viewer, owned))
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


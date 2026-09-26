"""Who has what on an agent, in one place.

An agent's access is decided by four things that each live somewhere else: its
owner (`Agent.owner`), explicit admins (`AgentAdmin`), the workspace's owners
(admins implicitly — `Agent.is_admin` — and listed nowhere), and the published
interface, which confines everyone else to a capability or lets a `full:` rule
through. Reading any one of them answers a quarter of "what can this person do
with this agent". This module composes the whole answer per person, using the
SAME predicates a turn is decided by (`interface.full_rule`, a capability's
`callers`), so the roster cannot say something the harness would not do.

The answer is for a member asking SIGNED IN to canopy — web, chat, a PAT, a
linked Slack account — which is verified by construction. A member writing in
by unverified email falls in fewer classes and may reach less; that is a
property of the message, not of the person, so it has no row of its own here.
"""
from __future__ import annotations

from apps.workspaces.models import WorkspaceMembership

from . import interface as iface_mod
from .models import AgentAdmin

#: A member's role on the agent, strongest first.
OWNER, ADMIN, MEMBER = "owner", "admin", "member"


def _member_access(email: str, iface: dict) -> tuple[str, list[str], str | None]:
    """(access, capabilities, rule) for a non-admin member.

    `full` when there is no interface (opt-in: nothing is confined until one is
    published) or a `full:` rule matches; `confined` to the capabilities whose
    callers include them; `none` when no capability lists them.
    """
    if not iface.get("capabilities") and not iface.get("full"):
        return "full", [], None
    classes = iface_mod._expand("member", email, True)
    rule = iface_mod.full_rule(classes, iface)
    if rule:
        return "full", [], rule
    caps = sorted(
        name for name, cap in (iface.get("capabilities") or {}).items()
        if classes & set(cap.get("callers") or [])
    )
    return ("confined" if caps else "none"), caps, None


def roster(agent) -> list[dict]:
    """One row per member of the agent's workspace: their workspace role, their
    role on this agent, why they have it, and what they can reach."""
    iface = agent.interface or {}
    grants = {
        g.user_id: g
        for g in AgentAdmin.objects.filter(agent=agent).select_related("granted_by")
    }
    rows = []
    # A listing of the workspace (filtered by workspace alone), not an access
    # decision about any one user — the authorizer rule does not apply.
    for m in WorkspaceMembership.objects.filter(workspace_id=agent.workspace_id).select_related("user"):
        user = m.user
        grant = grants.get(user.pk)
        if agent.owner_id == user.pk:
            role, basis = OWNER, "Owns this agent"
        elif m.role == WorkspaceMembership.OWNER:
            role, basis = ADMIN, "Owns the workspace"
        elif grant is not None:
            by = grant.granted_by.email if grant.granted_by else None
            role, basis = ADMIN, f"Made admin by {by}" if by else "Made admin"
        else:
            role, basis = MEMBER, "Workspace member"
        if role == MEMBER:
            access, caps, rule = _member_access(user.email or "", iface)
        else:
            access, caps, rule = "full", [], None
        rows.append({
            "user_id": user.pk,
            "email": user.email,
            "name": user.get_full_name() or user.email,
            "workspace_role": m.role,
            "agent_role": role,
            "basis": basis,
            "granted_at": grant.granted_at if grant is not None and role == ADMIN else None,
            "access": access,
            "capabilities": caps,
            "full_rule": rule,
        })
    rank = {OWNER: 0, ADMIN: 1, MEMBER: 2}
    rows.sort(key=lambda r: (rank[r["agent_role"]], (r["email"] or "").lower()))
    return rows


def outsiders(agent) -> list[dict]:
    """What people OUTSIDE the workspace can reach: every interface rule naming
    a contact or an unidentified caller. Empty when no interface is published,
    which means an outsider is refused (default deny, 2026-09-26) — the
    caller reports that separately as `interface_published`."""
    iface = agent.interface or {}
    out = []
    for rule in iface.get("full") or []:
        if not rule.startswith("member"):
            out.append({"caller": rule, "access": "full", "capability": None})
    for name, cap in sorted((iface.get("capabilities") or {}).items()):
        for caller in cap.get("callers") or []:
            if not caller.startswith("member"):
                out.append({"caller": caller, "access": "confined", "capability": name})
    return out

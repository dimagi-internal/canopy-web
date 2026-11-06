"""Questions about an agent's people that are answered from THE rule, `turn_context.resolve_turn_context`.

Every word used here is defined once, in `docs/architecture/access.md`. The rule itself is
`apps/agents/turn_context.py`; this module holds what is asked of it: who may change the agent itself
(`may_edit_agent`), who may pin its turns (`may_pin_runner`), and its roster, one row per member of its
workspace, so the roster cannot say something the harness would not do.
"""
from __future__ import annotations

from apps.harness import actors, routing, turn_asker
from apps.harness import caller_context as cc
from apps.harness.runners import can_administer_runner
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import interface as iface_mod
from . import roles, turn_context
from .standings import Standing, Standings

#: How canopy knows the asker, strongest first (defined once, in `caller_context`). `system` is
#: not a person. Not agent roles: those are `roles.py`.
OWNER, ADMIN, MEMBER, CONTACT, SYSTEM = cc.OWNER, cc.ADMIN, cc.MEMBER, cc.CONTACT, cc.SYSTEM
#: What a turn may reach.
FULL, CONFINED = turn_context.FULL, turn_context.CONFINED
#: What a roster row says of someone refused. Only here: a Turn Context's own word is `refused`.
ROSTER_REFUSED = "none"


def may_edit_agent(user, agent, *, standing: Standing | None = None, lines=None) -> bool:
    """May `user` change THE AGENT ITSELF (its skills, its persona, and the work in its repo)?

    The ONE predicate for the `agent_edit` permission: never write a second. It is the rule's
    answer, so it cannot say something the rest of access would not: the owner, an admin
    (`Agent.is_admin`) and the agent's own login (system) always may; anyone else only if a line
    on the agent's access list gives them `agent_edit` (an `editor` line, or a workspace default
    that is one). Being an editor of the agent's WORKSPACE gives nothing here: the workspace
    default lines (migration 0112) are what keep today's editors able to.

    A request an agent made on behalf of the person in its turn (`harness.turn_asker`) is judged
    as that person, like `may_pin_runner`: the agent's own authority never edits itself for an
    asker who could not.

    For a list of (person, agent) pairs pass `standing` (`Standings.of`) and `lines`
    (`Standings.lines_of`) read once, as `roster` does.
    """
    ob = turn_asker.of(user)
    if ob is not None:
        user = ob.user
        standing = lines = None
    d = turn_context.for_asker(agent, user, verified=True, channel="canopy_web", standing=standing, lines=lines)
    allowed = d.access == FULL and (d.standing.agent_role in (roles.OWNER, roles.ADMIN, roles.SYSTEM)
                                    or roles.holds(d.standing.permissions, roles.AGENT_EDIT))
    if ob is not None and allowed:
        turn_asker.note(ob, f"edit {agent.slug}")
    return allowed


def edit_refusal(agent) -> str:
    """Why `may_edit_agent` said no, naming who can fix it (the 403's detail)."""
    owner = agent.owner.email if agent.owner_id and agent.owner else "its owner or an admin"
    return (f"changing {agent.slug} itself needs the editor role on it; ask {owner} to give you "
            f"the editor role on {agent.slug}")


def may_pin_runner(user, agent, runner) -> bool:
    """May `user` pin `agent`'s turn to `runner`? An agent admin may pin any box
    that can hold the agent; the agent's OWN login may pin a box its admins
    already route it to (`_routed_to`); anyone else only a box they administer
    (its owner, or a `RunnerAdmin`). Either way the box must be able to hold the
    agent (`services.runner_may_hold_agent`) — that is checked separately, and
    first.

    A request an agent made ON BEHALF OF the person in its turn
    (`harness.turn_asker`) is judged as that person: "run it on my box" in the
    owner's own session is theirs to decide, not the agent's."""
    ob = turn_asker.of(user)
    if ob is not None:
        if agent.is_admin(ob.user) or can_administer_runner(ob.user, runner):
            turn_asker.note(ob, f"pin {agent.slug}'s turn to {runner.name}")
            return True
        return False
    if agent.is_admin(user):
        return True
    if agent.is_own_login(user) and _routed_to(agent, runner):
        return True

    return can_administer_runner(user, runner)


def _routed_to(agent, runner) -> bool:
    """Is `runner` one its admins already route `agent`'s own work to? It must be
    an enabled box in the agent's default order (its own, or the workspace's it
    follows), and a STRICT rule for the agent's own address, when there is one,
    must name it too. Pinning such a box adds no reach the routing did not
    already give: an agent sending its next chat to a box it is ordered onto
    (hal handing off acedimagi → haldimagi, 2026-10-19) is a choice among boxes
    its admins chose, while a box outside the order, or one a strict rule keeps
    its work off, stays the admins' or the box's administrators' to pick."""

    defaults, rules = routing.load_assignment_rows([agent.pk])
    if runner.pk not in {r.pk for _rank, r in defaults.get(agent.pk, [])}:
        return False
    own = actors.normalize_actor(getattr(agent.user, "email", ""))
    for (_agent_id, _source, actor), rows in rules.items():
        if own and actor == own and any(row.strict for row in rows) \
                and runner.pk not in {row.runner_id for row in rows}:
            return False
    return True


def roster(agent, members=None, standings: Standings | None = None) -> list[dict]:
    """One row per member of the agent's workspace: their workspace role, their role on this
    agent, why they have it, and what they reach signed in — by the rule, so the roster cannot
    say something the harness would not do. (People who are not in the workspace but whom a
    line names appear on the access list itself, not here.)

    A view listing many agents' rosters passes each workspace's `members`
    (`wsvc.effective_memberships`) and the `standings` of everyone in them."""
    if members is None:
        members = wsvc.effective_memberships(agent.workspace_id)
    if standings is None:
        standings = Standings([m.user for m in members], [agent])
    admin_grants = standings.grants[agent.pk]
    lines = standings.lines_of(agent)
    iface = agent.interface or {}
    rows = []
    # Everyone in the workspace as the authorizer sees them — owners of an
    # ancestor workspace included, since they are owners (and so admins) here.

    for m in members:
        user = m.user
        grant = admin_grants.get(user.pk)
        if agent.owner_id == user.pk:
            held, basis = OWNER, "Owns this agent"
        elif perms.role_allows(m.role, perms.OWN):
            held, basis = ADMIN, ("Owns a parent workspace" if getattr(m, "inherited", False)
                                  else "Owns the workspace")
        elif grant is not None:
            by = grant.granted_by.email if grant.granted_by else None
            held, basis = ADMIN, f"Made admin by {by}" if by else "Made admin"
        else:
            held, basis = MEMBER, "Workspace member"
        if held == MEMBER:
            d = turn_context.for_asker(agent, user, verified=True, channel="canopy_web",
                                       standing=standings.of(user, agent), lines=lines)
            caps = iface_mod.reachable(d.standing.permissions, iface) if d.access == CONFINED else []
            # The roster's own word for a refusal, which its readers (the API, the vault, topology) know.
            access, manual_only, may_auto = ROSTER_REFUSED if d.refused else d.access, d.review.manual_only, False
            role, source, via = d.standing.agent_role, d.standing.source, d.standing.via
            if d.standing.source == "system":
                via = "The agent's own login: it acts as the agent itself"
        else:
            access, caps, manual_only, may_auto = FULL, [], False, True
            role, source, via = held, held, basis
        rows.append({
            "user_id": user.pk,
            "email": user.email,
            "name": user.get_full_name() or user.email,
            "workspace_role": m.role,
            "agent_role": held,
            "basis": basis,
            "granted_at": grant.granted_at if grant is not None and held == ADMIN else None,
            "access": access,
            "profiles": caps,
            "manual_only": manual_only,
            "may_request_auto": may_auto,
            "role": role,
            "source": source,
            "via": via,
        })
    rank = {OWNER: 0, ADMIN: 1, MEMBER: 2}
    rows.sort(key=lambda r: (rank[r["agent_role"]], (r["email"] or "").lower()))
    return rows

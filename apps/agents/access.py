"""Who has what on an agent — THE rule, in one place.

Every word used here is defined once, in `docs/architecture/access.md` (the
glossary and the decision table). Read that first; this module implements it.

An agent's access is decided by things that each live somewhere else: its owner
(`Agent.owner`), explicit admins (`AgentAdmin`), the workspace's owners (admins
implicitly — `Agent.is_admin` — and listed nowhere), the person's workspace role
(`apps/workspaces/permissions.py`), and the published interface
(`apps/agents/interface.py`), which confines everyone else to a capability or
lets a `full:` rule through. `decide` composes the whole answer, and EVERY door
that makes an agent do work asks it — harness enqueue, a chat send, Slack, email
(through `harness.services.enqueue_turn`), the caller envelope, the turn mode at
claim, and the roster below — so no two of them can disagree.

**Agent roles:** `owner` / `admin` / `member` / `contact`, plus `system` (canopy
itself, the agent's OWN login, an approved item's dispatch).
**Access:** `full` (the agent's whole profile) / `confined` (one capability) /
`none` (refused).

The tiers, strongest first:

* **admin** (the agent's owner, a workspace owner, an explicit `AgentAdmin`;
  and `system`) → `full`. May ask for `auto`, pin any runner that may hold the
  agent, and manage its keys, interface, admins and routing.
* **workspace editor or above who is not an agent admin** → `full`, but every
  turn they start runs `manual`: they may edit the agent and dispatch work to it,
  never let it act outbound unreviewed. No `auto`, no `auto` routing rule, and a
  pin only to a runner they administer.
* **viewer members and contacts** → what the published interface names for
  them: `full` through a `full:` rule, `confined` to a capability, else `none`.
  With NO published interface they get `none`.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import interface as iface_mod
from .models import AgentAdmin

#: A person's role on the agent, strongest first. `system` is not a person.
OWNER, ADMIN, MEMBER, CONTACT, SYSTEM = "owner", "admin", "member", "contact", "system"
#: What a turn may reach.
FULL, CONFINED, NONE = "full", "confined", "none"
#: The `basis` of the editor tier — full profile, manual only.
EDITOR_BASIS = "editor"


@dataclass(frozen=True)
class Decision:
    """The answer for one (agent, asker, message). See docs/architecture/access.md."""

    role: str                 # owner | admin | member | contact | system
    access: str               # full | confined | none
    capability: str | None    # the capability a `confined` turn runs in
    may_request_auto: bool    # may ask for turn_mode=auto on a dispatch
    may_pin_runner: bool      # may pin ANY runner that can hold the agent (else: only runners they administer)
    manual_only: bool         # every turn they start runs manual (the editor tier)
    # WHY: owner | admin | system | editor | session:<role> | full:<rule> |
    # capability:<name> | refused
    basis: str
    # For `none`: what was refused and what the asker CAN do instead.
    reason: str = ""

    @property
    def stamp(self) -> str | None:
        """`Turn.capability` for this decision: FULL (""), a name, or None (refused)."""
        if self.access == FULL:
            return iface_mod.FULL
        if self.access == CONFINED:
            return self.capability
        return None


def _is_editor(user, agent) -> bool:
    """Workspace editor or above: may reshape the agent and dispatch to it."""
    from apps.workspaces import permissions as perms

    return bool(user is not None and getattr(user, "is_authenticated", False)
                and perms.can(user, agent.workspace_id, perms.AGENT_WORK))


def _offered(classes: set[str], iface: dict) -> list[str]:
    return sorted(n for n, c in (iface.get("capabilities") or {}).items()
                  if classes & set(c.get("callers") or []))


def _refusal(agent, iface: dict, classes: set[str], requested: str | None) -> str:
    """Why `none`, and what the asker CAN do."""
    owner = agent.owner.email if agent.owner_id and agent.owner else "its owner"
    if not iface_mod.published(iface):
        return (f"{agent.slug} has published no interface, so it takes work only from its "
                f"admins and workspace editors; ask {owner} to publish one naming you, or "
                "to make you an editor or an admin")
    offered = _offered(classes, iface)
    what = f"capability '{requested}'" if requested else "nothing"
    if offered:
        doors = ", ".join(f"'{n}'" for n in offered)
        return (f"{agent.slug} offers {what} to you; you may use capability {doors} "
                "(e.g. on a thread, or its MCP tool)")
    hint = ""
    if "contact:verified" in classes:
        # A verified sender nobody listed is, far more often than a stranger, an
        # automated one someone subscribed on purpose — CloudWatch alarms went
        # unanswered for nine days this way (canopy-web#1253). Say what fixes it.
        hint = ("; if this is an automated sender (alarms, CI, a monitor), a workspace admin "
                "can bind its address to a system account under Settings → System accounts")
    return (f"{agent.slug} offers {what} to you (see its declared interface); ask {owner} "
            f"to list you in it{hint}")


def decide(agent, who=None, *, verified: bool, origin: str = "", capability: str | None = None,
           turn=None) -> Decision:
    """THE rule: what `who` (a canopy User, a Contact, or None) may make `agent` do.

    `verified` is about THIS message (a signed-in request always is). `origin`
    is the channel (for the record; the rule does not vary by it). `capability`
    is a capability the asker named (an MCP tool), else the free-form door.
    `turn`, when given, supplies what only a turn knows: its initiator kind
    (canopy itself, another agent), the conversation it continues
    (`session_writer`) and the page or site it is held on (which door applies).
    """
    from apps.harness import caller_context as cc
    from apps.harness import initiator as initiator_mod

    iface = getattr(agent, "interface", None) or {}
    user = None
    if turn is not None:
        rel = cc.relationship(turn, agent)
        user = turn.initiator_user if turn.initiator_kind == initiator_mod.USER else None
        classes = iface_mod.caller_classes(turn, rel)
    else:
        from django.contrib.auth import get_user_model

        if isinstance(who, get_user_model()):
            user = who
            rel = cc.relationship_for_user(user, agent)
        else:
            rel = CONTACT
        classes = _classes_for(rel, who, verified)

    is_admin = user is not None and agent.is_admin(user)
    may_auto = bool(is_admin and verified)

    if rel in (OWNER, ADMIN, SYSTEM):
        return Decision(rel, FULL, None, may_auto, is_admin or rel in (OWNER, ADMIN), False, rel)

    def _full(basis: str, manual: bool = False) -> Decision:
        return Decision(rel, FULL, None, False, False, manual, basis)

    if turn is not None:
        role = iface_mod.session_writer(turn, agent)
        if role:
            return _full(f"session:{role}")
    editor = rel == MEMBER and _is_editor(user, agent)
    if iface_mod.published(iface):
        rule = iface_mod.full_rule(classes, iface)
        if rule:
            # The owner's chosen domain-wide access (e.g. Eva's `full: [member]`):
            # the whole agent, in whatever mode its routing gives, as before.
            return _full(f"full:{rule}")
    if editor:
        return _full(EDITOR_BASIS, manual=True)
    if not iface_mod.published(iface):
        return Decision(rel, NONE, None, False, False, False, "refused",
                        _refusal(agent, iface, classes, capability))
    name = capability
    if name is None and turn is not None:
        name = (iface_mod._page_capability(turn, iface, classes)
                or iface_mod._site_capability(turn, iface, classes))
    name = name or iface_mod.ASK
    cap = (iface.get("capabilities") or {}).get(name)
    if cap and classes & set(cap.get("callers") or []):
        return Decision(rel, CONFINED, name, False, False, False, f"capability:{name}")
    return Decision(rel, NONE, None, False, False, False, "refused",
                    _refusal(agent, iface, classes, capability))


def _classes_for(rel: str, who, verified: bool) -> set[str]:
    """Caller classes with no turn in hand — mirrors `interface.caller_classes`."""
    from apps.contacts.models import Contact

    if rel == MEMBER:
        return iface_mod._expand("member", getattr(who, "email", "") or "", verified)
    if isinstance(who, Contact):
        return iface_mod._expand("contact", who.email or "", verified)
    return iface_mod._expand("unknown", "", verified)


def decide_for_turn(turn, agent, requested: str | None = None) -> Decision:
    """`decide` for a turn already written, from its own initiator."""
    from apps.harness.caller_context import _verified

    who = turn.initiator_user or turn.initiator_contact
    return decide(agent, who, verified=_verified(turn), origin=turn.origin,
                  capability=requested, turn=turn)


def may_pin_runner(user, agent, runner) -> bool:
    """May `user` pin `agent`'s turn to `runner`? An agent admin may pin any box
    that can hold the agent; anyone else only a box they administer (its owner,
    or a `RunnerAdmin`). Either way the box must be able to hold the agent
    (`services.runner_may_hold_agent`) — that is checked separately, and first."""
    if agent.is_admin(user):
        return True
    from apps.harness.services import can_administer_runner

    return can_administer_runner(user, runner)


def roster(agent) -> list[dict]:
    """One row per member of the agent's workspace: their workspace role, their
    role on this agent, why they have it, and what they reach signed in — by
    `decide`, so the roster cannot say something the harness would not do."""
    grants = {
        g.user_id: g
        for g in AgentAdmin.objects.filter(agent=agent).select_related("granted_by")
    }
    rows = []
    # Everyone in the workspace as the authorizer sees them — owners of an
    # ancestor workspace included, since they are owners (and so admins) here.
    from apps.workspaces import permissions as perms
    from apps.workspaces import services as wsvc

    for m in wsvc.effective_memberships(agent.workspace_id):
        user = m.user
        grant = grants.get(user.pk)
        if agent.owner_id == user.pk:
            role, basis = OWNER, "Owns this agent"
        elif perms.role_allows(m.role, perms.OWN):
            role, basis = ADMIN, ("Owns a parent workspace" if getattr(m, "inherited", False)
                                  else "Owns the workspace")
        elif grant is not None:
            by = grant.granted_by.email if grant.granted_by else None
            role, basis = ADMIN, f"Made admin by {by}" if by else "Made admin"
        else:
            role, basis = MEMBER, "Workspace member"
        if role == MEMBER:
            d = decide(agent, user, verified=True, origin="canopy_web")
            iface = agent.interface or {}
            if d.access == CONFINED:
                # Every door listed for them, not just the free-form one.
                caps = _offered(iface_mod._expand("member", user.email or "", True), iface)
            else:
                caps = []
            access = d.access
            rule = d.basis.split(":", 1)[1] if d.basis.startswith("full:") else None
            manual_only = d.manual_only
            may_auto = False
        else:
            access, caps, rule, manual_only, may_auto = FULL, [], None, False, True
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
            "manual_only": manual_only,
            "may_request_auto": may_auto,
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

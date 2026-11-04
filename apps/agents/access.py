"""Who has what on an agent — THE rule, in one place.

Every word used here is defined once, in `docs/architecture/access.md` (the glossary and the
decision table). Read that first; this module implements it.

An agent's access is decided by two things: who the agent itself trusts without a list — its
owner (`Agent.owner`), its admins (`AgentAdmin`, and the workspace's owners, who are admins
implicitly: `Agent.is_admin`), canopy itself, and a writer in an admin's own session — and its
**access list** (`apps/agents/grants.py`), which gives everyone else a role. `decide` composes
the whole answer, and EVERY entry point that makes an agent do work asks it — harness enqueue,
a chat send, Slack, email (through `harness.services.enqueue_turn`), the caller envelope, the
turn mode at claim, and the roster below — so no two of them can disagree.

**Agent roles and permissions** (`roles.py`): owner > admin > editor > read_write > read_only,
each a named bundle of permissions (read, write, agent_edit, admin, own), plus `system` (canopy
itself, the agent's OWN login, an approved item's dispatch). A line may instead open one named
capability (`use:<name>`). `Decision.relationship` is how canopy knows the asker (owner / admin /
member / contact / system); `Decision.agent_role` is the named role (None for someone who holds
only capability lines) and `Decision.permissions` is everything their lines grant.
**Access:** `full` (the agent's whole profile) / `confined` (one capability) / `none` (refused).

The tiers, strongest first:

* **owner, admin, system** → `full`. May ask for `auto`, pin any runner that may hold the agent,
  and manage its keys, capabilities, admins, access list and routing.
* **anyone whose lines give `write`** (read_write, editor) → `full`, in the agent's own mode unless
  every line that gives it says `manual_only`: then every turn they start runs `manual` — they may
  dispatch work to it, never let it act outbound unreviewed. No `auto`, and a pin only to a runner
  they administer.
* **anyone whose lines give only `read` or named capabilities** → confined to the capability they
  ask for (else the page's or site's, else `ask`), if their permissions open it. A line cannot open
  a capability the agent does not have.
* **anyone else** → `none`, with the reason naming who can add them.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from django.contrib.auth import get_user_model

from apps.contacts.models import Contact
from apps.harness import actors, routing, turn_asker
from apps.harness import caller_context as cc
from apps.harness import initiator as initiator_mod
from apps.harness.caller_context import is_verified
from apps.harness.runners import can_administer_runner
from apps.workspaces import permissions as perms
from apps.workspaces import services as wsvc

from . import grants, roles
from . import interface as iface_mod
from .standings import Standing, Standings, standing_of

#: How canopy knows the asker, strongest first (defined once, in `caller_context`). `system` is
#: not a person. Not agent roles: those are `roles.py`.
OWNER, ADMIN, MEMBER, CONTACT, SYSTEM = cc.OWNER, cc.ADMIN, cc.MEMBER, cc.CONTACT, cc.SYSTEM
#: What a turn may reach.
FULL, CONFINED, NONE = "full", "confined", "none"


@dataclass(frozen=True, kw_only=True)
class Decision:
    """The answer for one (agent, asker, message). See docs/architecture/access.md."""

    relationship: str         # owner | admin | member | contact | system (how canopy knows them)
    access: str               # full | confined | none
    capability: str | None    # the capability a `confined` turn runs in
    may_request_auto: bool    # may ask for turn_mode=auto on a dispatch
    may_pin_runner: bool      # may pin ANY runner that can hold the agent (else: only runners they administer)
    manual_only: bool         # every turn they start runs manual (an editor line that says so)
    # WHY: owner | admin | system | session:<role> | grant:<line> | capability:<name> |
    # sensitive:<capability> | refused
    basis: str
    # For `none`: what was refused and what the asker CAN do instead.
    reason: str = ""
    # The asker's named role (`roles.py`): owner | admin | editor | read_write | read_only |
    # system, or None when refused or when they hold only named-capability lines.
    agent_role: str | None = None
    # WHERE the role came from: `owner` | `admin` | `system` | `session` | `list:<line>`.
    source: str = ""
    # `source` in words, for a person to read: the access-list line's `who` ("Anyone at
    # @dimagi.com (verified)").
    via: str = ""
    # Everything their lines grant (read, write, use:<capability>, ...); what a confined turn is
    # checked against. Empty for the owner/admin/system tiers, who need no list.
    permissions: frozenset = frozenset()

    @property
    def stamp(self) -> str | None:
        """`Turn.capability` for this decision: FULL (""), a name, or None (refused)."""
        if self.access == FULL:
            return iface_mod.FULL
        if self.access == CONFINED:
            return self.capability
        return None


def _list_refusal(agent, *, verified_contact: bool = False) -> str:
    owner = agent.owner.email if agent.owner_id and agent.owner else "its owner"
    why = (f"{agent.slug} does not list you on its access list; ask {owner} or an admin of "
           f"{agent.slug} to add you (read_only can ask it questions, read_write can use all of "
           "it, an editor can also change it)")
    if verified_contact:
        # A verified sender nobody listed is, far more often than a stranger, an automated
        # one someone subscribed on purpose — CloudWatch alarms went unanswered for nine days
        # this way (canopy-web#1253). Say what fixes it.
        why += ("; if this is an automated sender (alarms, CI, a monitor), an admin of the agent "
                "can bind its address to an inbound-mail trigger on the agent's Triggers tab")
    return why


def decide(agent, who=None, *, verified: bool, origin: str = "", capability: str | None = None,
           turn=None, standing: Standing | None = None, lines=None) -> Decision:
    """THE rule: what `who` (a canopy User, a Contact, or None) may make `agent` do.

    Owner, admin, system and a session writer (`_decide`), then the access list (`_from_access_list`)
    for everyone else, then the sensitive-data rule over it
    (`_sensitive_rule`): a capability that handles sensitive data (`data:
    sensitive`), and any turn in a sensitive conversation, is confined for
    everyone — owner, admin and canopy included.

    `standing` is what the database says about a canopy user and this agent, when
    the caller read it in bulk (`Standings`); else it is read here. `lines` is the
    agent's access list (`grants.lines_for`) when the caller already read it.

    Everyone but the owner, the admins and canopy gets what their line on the access list
    gives them, and nothing else.
    """
    decision = _stamp(_decide(agent, who, verified=verified, origin=origin, capability=capability,
                              turn=turn, st=standing))
    decision = _from_access_list(agent, decision, who, verified=verified, capability=capability,
                                 turn=turn, standing=standing, lines=lines)
    return _sensitive_rule(agent, decision, capability=capability, turn=turn)


#: Why a sensitive conversation with no capability that handles sensitive data is refused.
SENSITIVE_REFUSAL = ("this conversation holds a host's sensitive data, so it runs only in a "
                     "capability its author declared handles sensitive data (`data: sensitive`), "
                     "and {slug} has none for it{you}")


def _sensitive_capability(agent, iface: dict, capability: str | None, turn) -> str | None:
    """The `data: sensitive` capability this ask is in: the one it names, else the
    one its conversation's site or page selects. Callers are NOT checked here."""
    from fnmatch import fnmatchcase

    from . import sensitive_data

    caps = iface.get("capabilities") or {}
    if capability:
        # `read` and `ask` are one thing: naming either finds the agent's published read-mode
        # profile, and if that handles sensitive data, naming `read` must not dodge the rule.
        published, cap = iface_mod.published_capability(iface, capability)
        return published if (cap or {}).get("data") == sensitive_data.SENSITIVE else None
    if turn is None:
        return None
    site = iface_mod._conversation_site(turn)
    resource = iface_mod._declared_resource(turn)
    for name, cap in caps.items():
        if cap.get("data") != sensitive_data.SENSITIVE:
            continue
        if site and site in (cap.get("sites") or []):
            return name
        if resource and any(fnmatchcase(resource, p) for p in cap.get("pages") or []):
            return name
    return None


def _sensitive_rule(agent, d: Decision, *, capability: str | None, turn) -> Decision:
    """Spec 2026-10-22-sensitive-data-agents §5, "confined for everyone".

    * in a sensitive-data capability, whoever would have had the agent's FULL profile (owner,
      admin, system, an editor, a session writer) runs that capability's
      confined profile instead — same role, same mode, same pin rights;
    * a confined caller keeps the capability the table gave them;
    * a turn in a sensitive conversation that ends up anywhere but a
      sensitive-data capability is refused, never run full or in another one.
    """
    from apps.harness import sensitivity

    iface = getattr(agent, "interface", None) or {}
    capability_name = _sensitive_capability(agent, iface, capability, turn)
    sensitive = turn is not None and sensitivity.is_sensitive(turn)
    if capability_name is None and not sensitive:
        return d
    if capability_name is not None and d.access == FULL:
        return replace(d, access=CONFINED, capability=capability_name, basis=f"sensitive:{capability_name}")
    if d.access == NONE or not sensitive:
        return d
    if capability_name is not None and d.access == CONFINED and d.capability == capability_name:
        return d
    you = "" if capability_name is None else f" that is open to you ('{capability_name}' does not list you)"
    return replace(d, access=NONE, capability=None, may_request_auto=False, may_pin_runner=False,
                   manual_only=False, basis="refused", agent_role=None, source="",
                   permissions=frozenset(), reason=SENSITIVE_REFUSAL.format(slug=agent.slug, you=you))


def _stamp(d: Decision) -> Decision:
    """Name where the rules that need no list put `d` on the role ladder."""
    if d.relationship == OWNER:
        return replace(d, agent_role=roles.OWNER, source="owner")
    if d.relationship == ADMIN:
        return replace(d, agent_role=roles.ADMIN, source="admin")
    if d.relationship == SYSTEM:
        return replace(d, agent_role=roles.SYSTEM, source="system")
    if d.access == FULL:
        # A session writer drives this conversation; nothing gives them the agent itself, so their
        # `permissions` (what the envelope and the own-repo rail read) stop at `write`.
        return replace(d, agent_role=roles.EDITOR, source="session", permissions=roles.ROLES.close({roles.WRITE}))
    return d


def _asker_of(who, turn, verified: bool, standing: Standing | None) -> grants.Asker:
    """The asker as the access list sees them: a canopy user, or a contact known by address."""
    user = contact = None
    if turn is not None:
        user = turn.initiator_user if turn.initiator_kind == initiator_mod.USER else None
        contact = turn.initiator_contact if turn.initiator_kind == initiator_mod.CONTACT else None
        verified = is_verified(turn)
    elif isinstance(who, get_user_model()):
        user = who
    elif isinstance(who, Contact):
        contact = who
    email = (getattr(user, "email", "") or getattr(contact, "email", "") or "").strip().lower()
    return grants.Asker(user=user, email=email, verified=bool(verified), contact=contact is not None,
                        workspace_roles=getattr(standing, "roles", None),
                        descendants=getattr(standing, "descendants", None))


def _capability_for(agent, iface: dict, permissions, capability, turn):
    """The capability someone confined is held to: the one named, else the page's or the
    site's, else read mode — among those their permissions open.

    Read mode (`read`/`ask`, one thing: `interface.is_read_mode`) exists on every agent. It is
    the agent's own published `ask` when it has one (the stamp is that name), else canopy's
    default (`interface.default_read_profile`, stamped `read`). Any other name is a door and
    must be published; an unknown or retired door is refused here, never widened."""
    def opens(name, cap):
        return iface_mod.opens(permissions, name)

    name = capability
    if name is None and turn is not None:
        name = iface_mod._page_capability(turn, iface, opens) or iface_mod._site_capability(turn, iface, opens)
    name = name or iface_mod.ASK
    published, cap = iface_mod.published_capability(iface, name)
    if published is None and iface_mod.is_read_mode(name):
        published, cap = iface_mod.READ_MODE, {}   # {}: canopy's default, built at claim (`profile`)
    return (published, cap) if cap is not None and opens(published, cap) else (None, None)


def _from_access_list(agent, d: Decision, who, *, verified: bool, capability, turn,
                      standing: Standing | None, lines) -> Decision:
    """What the access list gives the asker. An owner, admin or canopy itself, and a session
    writer, already have the whole agent; everyone else has exactly what their lines say."""
    if d.access == FULL:
        return d
    asker = _asker_of(who, turn, verified, standing)
    got = grants.resolve(agent, asker, lines if lines is not None else getattr(standing, "lines", None))
    if not got.permissions:
        return d
    iface = getattr(agent, "interface", None) or {}
    if roles.holds(got.permissions, roles.WRITE):
        # `write` is driving the whole agent: that is `full`, whatever the role is called. What
        # makes someone an agent ADMIN is still `Agent.is_admin` (owner, workspace owner,
        # `AgentAdmin`), never a list line, so a line never raises `may_request_auto`.
        line = got.line_for(roles.WRITE)
        return Decision(relationship=d.relationship, access=FULL, capability=None, may_request_auto=False,
                        may_pin_runner=False, manual_only=got.manual_only, basis=got.basis,
                        agent_role=got.role, source=f"list:{line.subject_key}", via=grants.describe(line),
                        permissions=got.permissions)
    name, _cap = _capability_for(agent, iface, got.permissions, capability, turn)
    if name is None:
        return d
    line = got.line_opening(name)
    return Decision(relationship=d.relationship, access=CONFINED, capability=name, may_request_auto=False,
                    may_pin_runner=False, manual_only=False, basis=f"capability:{name}",
                    agent_role=got.role, source=f"list:{line.subject_key}", via=grants.describe(line),
                    permissions=got.permissions)


def _decide(agent, who=None, *, verified: bool, origin: str = "", capability: str | None = None,
            turn=None, st: Standing | None = None) -> Decision:
    """The part of the rule that needs no list: the owner, the admins, canopy itself and a
    writer in an admin's own session have the whole agent; everyone else is `none` here and
    gets whatever their line on the access list says (`_from_access_list`).

    `verified` is about THIS message (a signed-in request always is). `origin` is the channel
    (for the record; the rule does not vary by it). `capability` is one the asker named (e.g. a
    storyboard's), else the free-form one (`ask`). `turn`, when given, supplies what only a
    turn knows: its initiator kind (canopy itself, another agent) and the conversation it
    continues (`session_writer`).
    """
    user = None
    if turn is not None:
        rel = cc.relationship(turn, agent)
        user = turn.initiator_user if turn.initiator_kind == initiator_mod.USER else None
    elif isinstance(who, get_user_model()):
        user = who
        st = st or standing_of(user, agent)
        rel = cc.relationship_for_user(user, agent, role=st.role, granted=st.granted)
    else:
        rel = CONTACT

    if user is not None and st is None:
        st = standing_of(user, agent)
    is_admin = user is not None and agent.is_admin(user, role=st.role, granted=st.granted)
    may_auto = bool(is_admin and verified)

    if rel in (OWNER, ADMIN, SYSTEM):
        return Decision(relationship=rel, access=FULL, capability=None, may_request_auto=may_auto,
                        may_pin_runner=is_admin or rel in (OWNER, ADMIN), manual_only=False, basis=rel)
    if turn is not None:
        role = iface_mod.session_writer(turn, agent)
        if role:
            return Decision(relationship=rel, access=FULL, capability=None, may_request_auto=False,
                            may_pin_runner=False, manual_only=False, basis=f"session:{role}")
    return Decision(relationship=rel, access=NONE, capability=None, may_request_auto=False,
                    may_pin_runner=False, manual_only=False, basis="refused",
                    reason=_list_refusal(agent, verified_contact=rel == CONTACT and bool(verified)))


def may_edit_agent(user, agent, *, standing: Standing | None = None, lines=None) -> bool:
    """May `user` change THE AGENT ITSELF (its skills, its persona, and the work in its repo)?

    The ONE predicate for the `agent_edit` permission: never write a second. It is `decide`'s
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
    d = decide(agent, user, verified=True, origin="canopy_web", standing=standing, lines=lines)
    allowed = d.access == FULL and (d.agent_role in (roles.OWNER, roles.ADMIN, roles.SYSTEM)
                                    or roles.holds(d.permissions, roles.AGENT_EDIT))
    if ob is not None and allowed:
        turn_asker.note(ob, f"edit {agent.slug}")
    return allowed


def edit_refusal(agent) -> str:
    """Why `may_edit_agent` said no, naming who can fix it (the 403's detail)."""
    owner = agent.owner.email if agent.owner_id and agent.owner else "its owner or an admin"
    return (f"changing {agent.slug} itself needs the editor role on it; ask {owner} to give you "
            f"the editor role on {agent.slug}")


def decide_for_turn(turn, agent, requested: str | None = None) -> Decision:
    """`decide` for a turn already written, from its own initiator."""

    who = turn.initiator_user or turn.initiator_contact
    return decide(agent, who, verified=is_verified(turn), origin=turn.origin,
                  capability=requested, turn=turn)


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
    agent, why they have it, and what they reach signed in — by `decide`, so the roster cannot
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
            d = decide(agent, user, verified=True, origin="canopy_web", standing=standings.of(user, agent),
                       lines=lines)
            caps = iface_mod.reachable(d.permissions, iface) if d.access == CONFINED else []
            access, manual_only, may_auto = d.access, d.manual_only, False
            role, source, via = d.agent_role, d.source, d.via
            if d.source == "system":
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
            "capabilities": caps,
            "manual_only": manual_only,
            "may_request_auto": may_auto,
            "role": role,
            "source": source,
            "via": via,
        })
    rank = {OWNER: 0, ADMIN: 1, MEMBER: 2}
    rows.sort(key=lambda r: (rank[r["agent_role"]], (r["email"] or "").lower()))
    return rows

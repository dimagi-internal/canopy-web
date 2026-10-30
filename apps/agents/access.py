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
  pin only to a runner they administer. An interface with `editors: confined`
  removes this tier: such an editor is decided like a viewer member, below.
* **viewer members and contacts** → what the published interface names for
  them: `full` through a `full:` rule, `confined` to a capability, else `none`.
  With NO published interface they get `none`.
"""
from __future__ import annotations

from collections.abc import Mapping
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
    # The asker's place on the role ladder (`roles.py`): owner | admin | editor | user |
    # viewer | system, or None when refused. `role` above is the older relationship word
    # (owner | admin | member | contact | system), kept until its readers move.
    agent_role: str | None = None
    # WHERE the role came from: `owner` | `admin` | `system` | `workspace-editor` (the
    # implicit rule) | `rule:<full rule>` | `door:<name>` | `session` | `list:<line>`.
    source: str = ""

    @property
    def stamp(self) -> str | None:
        """`Turn.capability` for this decision: FULL (""), a name, or None (refused)."""
        if self.access == FULL:
            return iface_mod.FULL
        if self.access == CONFINED:
            return self.capability
        return None


@dataclass(frozen=True)
class Standing:
    """What the database says about one person and one agent — all `decide` reads
    about them: their role in the agent's workspace (`wsvc.member_role`, None =
    not a member) and whether they hold an explicit `AgentAdmin` grant (None =
    not read; `Agent.is_admin` reads it if it needs it)."""

    role: str | None
    granted: bool | None = None
    #: Their effective role in EVERY workspace, when the caller read them in bulk
    #: (`wsvc.member_roles_of`) — what an access-list line naming another workspace asks.
    roles: Mapping[str, str] | None = None
    #: The agent's access list, when the caller read it in bulk (`Standings`).
    lines: list | None = None


def standing_of(user, agent) -> Standing:
    return Standing(wsvc.member_role(user, agent.workspace_id))


class Standings:
    """`standing_of` for many people and many agents, read at once: every role of
    every person (`wsvc.member_roles_of`) and every agent's grants, in a fixed
    number of queries however many pairs are then asked. For a view that judges
    many (person, agent) pairs — the agent topology, the roster — so it asks
    `decide` per pair without a membership read per pair.

    Read per request and dropped with it: not a cache, nothing to invalidate.
    Asking about a person it was not given raises, rather than reading them as
    a non-member."""

    def __init__(self, users, agents) -> None:
        self._roles = wsvc.member_roles_of(users)
        self._lines = grants.lines_for_many(agents)
        self.grants: dict[int, dict[int, AgentAdmin]] = {a.pk: {} for a in agents}
        for g in AgentAdmin.objects.filter(agent__in=self.grants).select_related("granted_by"):
            self.grants[g.agent_id][g.user_id] = g

    def of(self, user, agent) -> Standing:
        mine = self._roles[user.pk]
        return Standing(mine.get(agent.workspace_id), user.pk in self.grants[agent.pk], mine,
                        self._lines[agent.pk])

    def is_admin(self, user, agent) -> bool:
        """`agent.is_admin(user)`, from what was read."""
        st = self.of(user, agent)
        return agent.is_admin(user, role=st.role, granted=st.granted)


def _is_editor(user, role: str | None) -> bool:
    """Workspace editor or above: may reshape the agent and dispatch to it."""

    return bool(user is not None and getattr(user, "is_authenticated", False)
                and perms.role_allows(role, perms.AGENT_WORK))


def _offered(classes: set[str], iface: dict) -> list[str]:
    return sorted(n for n, c in (iface.get("capabilities") or {}).items()
                  if classes & set(c.get("callers") or []))


def _list_refusal(agent) -> str:
    owner = agent.owner.email if agent.owner_id and agent.owner else "its owner"
    return (f"{agent.slug} does not list you on its access list; ask {owner} or an admin of "
            f"{agent.slug} to add you (a viewer can ask it questions, a user can use what it "
            "publishes, an editor can run all of it)")


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
                "(e.g. on a thread)")
    hint = ""
    if "contact:verified" in classes:
        # A verified sender nobody listed is, far more often than a stranger, an
        # automated one someone subscribed on purpose — CloudWatch alarms went
        # unanswered for nine days this way (canopy-web#1253). Say what fixes it.
        hint = ("; if this is an automated sender (alarms, CI, a monitor), an admin of the "
                "agent can bind its address to an inbound-mail trigger on the agent's Triggers tab")
    return (f"{agent.slug} offers {what} to you (see its declared interface); ask {owner} "
            f"to list you in it{hint}")


def decide(agent, who=None, *, verified: bool, origin: str = "", capability: str | None = None,
           turn=None, standing: Standing | None = None, lines=None) -> Decision:
    """THE rule: what `who` (a canopy User, a Contact, or None) may make `agent` do.

    The decision table (`_decide`), then the sensitive-data rule over it
    (`_sensitive_rule`): a capability that handles sensitive data (`data:
    sensitive`), and any turn in a sensitive conversation, is confined for
    everyone — owner, admin and canopy included.

    `standing` is what the database says about a canopy user and this agent, when
    the caller read it in bulk (`Standings`); else it is read here. `lines` is the
    agent's access list (`grants.lines_for`) when the caller already read it.

    The tiers above are the rules an agent was published with. The access list
    (`grants.py`) is the other way in: whatever it gives the asker is merged with them,
    and the more permissive answer stands, so an agent with an empty list decides
    exactly as before.
    """
    decision = _stamp(agent, _decide(agent, who, verified=verified, origin=origin,
                                     capability=capability, turn=turn, st=standing))
    decision = _with_access_list(agent, decision, who, verified=verified, capability=capability,
                                 turn=turn, standing=standing, lines=lines)
    return _sensitive_rule(agent, decision, capability=capability, turn=turn)


#: Why a sensitive conversation with no capability that handles sensitive data is refused.
SENSITIVE_REFUSAL = ("this conversation holds a host's sensitive data, so it runs only in a "
                     "capability its author declared handles sensitive data (`data: sensitive`), "
                     "and {slug} has none for it{you}")


def _sensitive_door(agent, iface: dict, capability: str | None, turn) -> str | None:
    """The `data: sensitive` capability this ask is in: the one it names, else the
    one its conversation's site or page selects. Callers are NOT checked here."""
    from fnmatch import fnmatchcase

    from . import sensitive_data

    caps = iface.get("capabilities") or {}
    if capability:
        return capability if (caps.get(capability) or {}).get("data") == sensitive_data.SENSITIVE else None
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

    * in a sensitive-data door, whoever would have had the agent's FULL profile (owner,
      admin, system, a `full:` rule, an editor, a session writer) runs that door's
      confined profile instead — same role, same mode, same pin rights;
    * a confined caller keeps the door the table gave them;
    * a turn in a sensitive conversation that ends up anywhere but a
      sensitive-data door is refused, never run full or in another capability.
    """
    from apps.harness import sensitivity

    iface = getattr(agent, "interface", None) or {}
    door = _sensitive_door(agent, iface, capability, turn)
    sensitive = turn is not None and sensitivity.is_sensitive(turn)
    if door is None and not sensitive:
        return d
    if door is not None and d.access == FULL:
        return replace(d, access=CONFINED, capability=door, basis=f"sensitive:{door}")
    if d.access == NONE or not sensitive:
        return d
    if door is not None and d.access == CONFINED and d.capability == door:
        return d
    you = "" if door is None else f" that is open to you ('{door}' does not list you)"
    return replace(d, access=NONE, capability=None, may_request_auto=False, may_pin_runner=False,
                   manual_only=False, basis="refused", agent_role=None, source="",
                   reason=SENSITIVE_REFUSAL.format(slug=agent.slug, you=you))


def _stamp(agent, d: Decision) -> Decision:
    """Name where the rules an agent was published with put `d` on the role ladder."""
    if d.role == OWNER:
        return replace(d, agent_role=roles.OWNER, source="owner")
    if d.role == ADMIN:
        return replace(d, agent_role=roles.ADMIN, source="admin")
    if d.role == SYSTEM:
        return replace(d, agent_role=roles.SYSTEM, source="system")
    if d.access == FULL:
        what = d.basis.split(":", 1)[0]
        return replace(d, agent_role=roles.EDITOR,
                       source={"full": f"rule:{d.basis.partition(':')[2]}", "editor": "workspace-editor",
                               "session": "session"}.get(what, what))
    if d.access == CONFINED:
        cap = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).get(d.capability) or {}
        return replace(d, agent_role=iface_mod.door_role(cap), source=f"door:{d.capability}")
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
                        workspace_roles=getattr(standing, "roles", None))


def _door_for(agent, iface: dict, asker_role: str, capability, turn):
    """The door a viewer or user is confined to: the one named, else the page's or the
    site's, else `ask` — the same order as always — among doors their role can open."""
    def opens(cap):
        return iface_mod.role_opens(asker_role, cap)

    name = capability
    if name is None and turn is not None:
        name = iface_mod._page_capability(turn, iface, set(), opens) \
            or iface_mod._site_capability(turn, iface, set(), opens)
    name = name or iface_mod.ASK
    cap = (iface.get("capabilities") or {}).get(name)
    return (name, cap) if cap and opens(cap) else (None, None)


_ACCESS_ORDER = {NONE: 0, CONFINED: 1, FULL: 2}


def _with_access_list(agent, d: Decision, who, *, verified: bool, capability, turn,
                      standing: Standing | None, lines) -> Decision:
    """Merge what the access list gives the asker into `d`; the more permissive answer
    stands. An owner, admin or canopy itself already has everything."""
    if d.role in (OWNER, ADMIN, SYSTEM):
        return d
    asker = _asker_of(who, turn, verified, standing)
    got = grants.resolve(agent, asker, lines if lines is not None else getattr(standing, "lines", None))
    if got.role is None:
        return d
    iface = getattr(agent, "interface", None) or {}
    if roles.allows(got.role, roles.RUN):
        # An `admin` line is read as `editor` here: what makes someone an agent admin is
        # still `Agent.is_admin` (owner, workspace owner, `AgentAdmin`), not a list line.
        listed = Decision(d.role, FULL, None, False, False, got.manual_only, got.basis,
                          agent_role=roles.EDITOR, source=f"list:{got.lines[0].subject_key}")
    else:
        name, _cap = _door_for(agent, iface, got.role, capability, turn)
        if name is None:
            return d
        listed = Decision(d.role, CONFINED, name, False, False, False, f"capability:{name}",
                          agent_role=got.role, source=f"list:{got.lines[0].subject_key}")
    if _ACCESS_ORDER[listed.access] > _ACCESS_ORDER[d.access]:
        return listed
    if listed.access == d.access == FULL and d.manual_only and not listed.manual_only:
        return listed      # the list names them without the gate the implicit editor rule puts on
    if listed.access == d.access == CONFINED and roles.rank(listed.agent_role) > roles.rank(d.agent_role):
        return replace(d, agent_role=listed.agent_role, source=listed.source)
    return d


def _decide(agent, who=None, *, verified: bool, origin: str = "", capability: str | None = None,
            turn=None, st: Standing | None = None) -> Decision:
    """The decision table (docs/architecture/access.md), before the sensitive-data rule.

    `verified` is about THIS message (a signed-in request always is). `origin`
    is the channel (for the record; the rule does not vary by it). `capability`
    is a capability the asker named (e.g. a storyboard's), else the free-form door.
    `turn`, when given, supplies what only a turn knows: its initiator kind
    (canopy itself, another agent), the conversation it continues
    (`session_writer`) and the page or site it is held on (which door applies).
    """

    iface = getattr(agent, "interface", None) or {}
    user = None
    if turn is not None:
        rel = cc.relationship(turn, agent)
        user = turn.initiator_user if turn.initiator_kind == initiator_mod.USER else None
        classes = iface_mod.caller_classes(turn, rel)
    else:
        if isinstance(who, get_user_model()):
            user = who
            st = st or standing_of(user, agent)
            rel = cc.relationship_for_user(user, agent, role=st.role, granted=st.granted)
        else:
            rel = CONTACT
        classes = _classes_for(rel, who, verified)

    if user is not None and st is None:
        st = standing_of(user, agent)
    is_admin = user is not None and agent.is_admin(user, role=st.role, granted=st.granted)
    may_auto = bool(is_admin and verified)

    if rel in (OWNER, ADMIN, SYSTEM):
        return Decision(rel, FULL, None, may_auto, is_admin or rel in (OWNER, ADMIN), False, rel)

    def _full(basis: str, manual: bool = False) -> Decision:
        return Decision(rel, FULL, None, False, False, manual, basis)

    if turn is not None:
        role = iface_mod.session_writer(turn, agent)
        if role:
            return _full(f"session:{role}")
    if agent.uses_access_list:
        # The access list is the only way in beyond the owner and admins (and the session
        # writer above): it is merged in by `_with_access_list`, so nothing is granted here.
        return Decision(rel, NONE, None, False, False, False, "refused", _list_refusal(agent))
    editor = rel == MEMBER and _is_editor(user, st.role if st else None)
    if iface_mod.published(iface):
        rule = iface_mod.full_rule(classes, iface)
        if rule:
            # The owner's chosen domain-wide access (e.g. Eva's `full: [member]`):
            # the whole agent, in whatever mode its routing gives, as before.
            return _full(f"full:{rule}")
    if editor and not iface_mod.editors_confined(iface):
        # `editors: confined` skips this tier: the editor is decided below
        # exactly like a viewer member (capability, else refused).
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

    if rel == MEMBER:
        return iface_mod._expand("member", getattr(who, "email", "") or "", verified)
    if isinstance(who, Contact):
        return iface_mod._expand("contact", who.email or "", verified)
    return iface_mod._expand("unknown", "", verified)


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
    """One row per member of the agent's workspace: their workspace role, their
    role on this agent, why they have it, and what they reach signed in — by
    `decide`, so the roster cannot say something the harness would not do.

    A view listing many agents' rosters passes each workspace's `members`
    (`wsvc.effective_memberships`) and the `standings` of everyone in them."""
    if members is None:
        members = wsvc.effective_memberships(agent.workspace_id)
    if standings is None:
        standings = Standings([m.user for m in members], [agent])
    admin_grants = standings.grants[agent.pk]
    lines = standings._lines[agent.pk]
    rows = []
    # Everyone in the workspace as the authorizer sees them — owners of an
    # ancestor workspace included, since they are owners (and so admins) here.

    for m in members:
        user = m.user
        grant = admin_grants.get(user.pk)
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
            d = decide(agent, user, verified=True, origin="canopy_web", standing=standings.of(user, agent),
                       lines=lines)
            iface = agent.interface or {}
            if d.access == CONFINED and d.source.startswith("list:"):
                caps = sorted(n for n, c in (iface.get("capabilities") or {}).items()
                              if iface_mod.role_opens(d.agent_role, c))
            elif d.access == CONFINED:
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
            "role": d.agent_role if role == MEMBER else role,
            "source": d.source if role == MEMBER else role,
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

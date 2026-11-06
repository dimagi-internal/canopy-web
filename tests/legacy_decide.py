"""A frozen copy of `access.decide`, `decide_for_turn` and `Decision` as they were when they were deleted.

`docs/superpowers/specs/2026-11-05-turn-context-design.md`: this project changes no one's access, so the
resolver that replaced them is held equal to them, case by case, in `tests/test_turn_context_equivalence.py`.
That test first ran against the real functions, in the commit before they were deleted; this is the same
code, moved here unchanged.

Do not import it from `apps/`, and do not edit it to make that test pass: this file is the reference. A
difference means the resolver changed what someone may do. Facts that are not the rule (who holds which
line, what a role grants, who is an admin, what an entry is) are still read from the real code.

One deliberate edit since: on 2026-11-06 the owner removed the `session_writer` rule (a participant with
role owner or editor in a session the agent's owner or an admin started held the whole agent). Its branch
is gone from `_decide` and `_stamp` here exactly as it is gone from the resolver, so this stays the rule
as stated. `tests/test_turn_context_equivalence.py` names the cases that changed.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

from django.contrib.auth import get_user_model

from apps.agents import grants, roles
from apps.agents import interface as iface_mod
from apps.agents.standings import Standing, standing_of
from apps.contacts.models import Contact
from apps.harness import caller_context as cc
from apps.harness import initiator as initiator_mod
from apps.harness.caller_context import is_verified

OWNER, ADMIN, MEMBER, CONTACT, SYSTEM = cc.OWNER, cc.ADMIN, cc.MEMBER, cc.CONTACT, cc.SYSTEM
FULL, CONFINED, NONE = "full", "confined", "none"


def _page_capability(turn, iface: dict, opens) -> str | None:
    from fnmatch import fnmatchcase

    resource = iface_mod._declared_resource(turn)
    if not resource:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        patterns = cap.get("pages") or []
        if not patterns or not opens(name, cap):
            continue
        if any(fnmatchcase(resource, pattern) for pattern in patterns):
            return name
    return None


def _site_capability(turn, iface: dict, opens) -> str | None:
    site = iface_mod._conversation_site(turn)
    if not site:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        if site in (cap.get("sites") or []) and opens(name, cap):
            return name
    return None


@dataclass(frozen=True, kw_only=True)
class Decision:
    """The answer for one (agent, asker, message). See docs/architecture/access.md."""

    relationship: str         # owner | admin | member | contact | system (how canopy knows them)
    access: str               # full | confined | none
    capability: str | None    # the capability a `confined` turn runs in
    may_request_auto: bool    # may ask for turn_mode=auto on a dispatch
    may_pin_runner: bool      # may pin ANY runner that can hold the agent (else: only runners they administer)
    manual_only: bool         # every turn they start runs manual (an editor line that says so)
    # WHY: owner | admin | system | grant:<line> | capability:<name> |
    # sensitive:<capability> | refused
    basis: str
    # For `none`: what was refused and what the asker CAN do instead.
    reason: str = ""
    # The asker's named role (`roles.py`): owner | admin | editor | read_write | read_only |
    # system, or None when refused or when they hold only named-capability lines.
    agent_role: str | None = None
    # WHERE the role came from: `owner` | `admin` | `system` | `list:<line>`.
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

    Owner, admin and system (`_decide`), then the access list (`_from_access_list`)
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
    review = turn is not None and iface_mod.is_review_turn(turn)
    if review:
        capability = iface_mod.SESSION_REVIEW
    decision = _stamp(_decide(agent, who, verified=verified, origin=origin, capability=capability,
                              turn=turn, st=standing))
    decision = _from_access_list(agent, decision, who, verified=verified, capability=capability,
                                 turn=turn, standing=standing, lines=lines)
    if review:
        decision = _review_rule(decision)
    return _sensitive_rule(agent, decision, capability=capability, turn=turn)


def _review_rule(d: Decision) -> Decision:
    """A turn in a one-session review's chat runs in its review profile, for everyone with any access at
    all: owner, admin, editor and canopy too. It only narrows, as `_sensitive_rule` does. That
    chat holds a transcript someone may have written to steer whoever reads it, so no later
    message there picks up the agent's whole profile, nor a login, a token or a send."""
    if d.access == NONE:
        return d
    return replace(d, access=CONFINED, capability=iface_mod.SESSION_REVIEW, may_request_auto=False,
                   may_pin_runner=False, basis=f"capability:{iface_mod.SESSION_REVIEW}")


#: Why a sensitive conversation with no capability that handles sensitive data is refused.
SENSITIVE_REFUSAL = ("this conversation holds a host's sensitive data, so it runs only in a "
                     "capability its author declared handles sensitive data (`data: sensitive`), "
                     "and {slug} has none for it{you}")


def _sensitive_capability(agent, iface: dict, capability: str | None, turn) -> str | None:
    """The `data: sensitive` capability this ask is in: the one it names, else the
    one its conversation's site or page selects. Callers are NOT checked here."""
    from fnmatch import fnmatchcase

    from apps.agents import sensitive_data

    caps = iface.get("capabilities") or {}
    if capability:
        # `read` and `ask` are one thing: naming either finds the agent's published read-mode
        # profile, and if that handles sensitive data, naming `read` must not dodge the rule.
        published, cap = iface_mod.published_profile(iface, capability)
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
      admin, system, an editor) runs that capability's
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

    Read mode (`read` and `READ_MODE_STAMP`, one thing: `interface.is_read_mode`) exists on every agent. It is
    the agent's own published read mode when it has one (the stamp is that name), else canopy's
    default (`interface.default_read_profile`, stamped `read`). Any other name is a host site and
    must be published; an unknown or retired one is refused here, never widened."""
    def opens(name, cap):
        return iface_mod.opens(permissions, name)

    if capability == iface_mod.SESSION_REVIEW:
        # Canopy's own profile, for a turn in a review chat only (`decide`): `read` opens it.
        is_review = turn is not None and iface_mod.is_review_turn(turn)
        return (capability, {}) if is_review and roles.holds(permissions, roles.READ) else (None, None)
    name = capability
    if name is None and turn is not None:
        name = _page_capability(turn, iface, opens) or _site_capability(turn, iface, opens)
    name = name or iface_mod.READ_MODE_STAMP
    published, cap = iface_mod.published_profile(iface, name)
    if published is None and iface_mod.is_read_mode(name):
        published, cap = iface_mod.READ_MODE, {}   # {}: canopy's default, built at claim (`profile`)
    return (published, cap) if cap is not None and opens(published, cap) else (None, None)


def _from_access_list(agent, d: Decision, who, *, verified: bool, capability, turn,
                      standing: Standing | None, lines) -> Decision:
    """What the access list gives the asker. An owner, admin or canopy itself already has the whole
    agent; everyone else has exactly what their lines say."""
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
    # The review profile is opened by `read`; `line_opening` knows only host sites and read mode.
    line = got.line_for(roles.READ) if name == iface_mod.SESSION_REVIEW else got.line_opening(name)
    return Decision(relationship=d.relationship, access=CONFINED, capability=name, may_request_auto=False,
                    may_pin_runner=False, manual_only=False, basis=f"capability:{name}",
                    agent_role=got.role, source=f"list:{line.subject_key}", via=grants.describe(line),
                    permissions=got.permissions)


def _decide(agent, who=None, *, verified: bool, origin: str = "", capability: str | None = None,
            turn=None, st: Standing | None = None) -> Decision:
    """The part of the rule that needs no list: the owner, the admins and canopy itself have the
    whole agent; everyone else is `none` here and gets whatever their line on the access list
    says (`_from_access_list`).

    `verified` is about THIS message (a signed-in request always is). `origin` is the channel
    (for the record; the rule does not vary by it). `capability` is one the asker named (e.g. a
    storyboard's), else the free-form one (`ask`). `turn`, when given, supplies what only a
    turn knows: its initiator kind (canopy itself, another agent).
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
    return Decision(relationship=rel, access=NONE, capability=None, may_request_auto=False,
                    may_pin_runner=False, manual_only=False, basis="refused",
                    reason=_list_refusal(agent, verified_contact=rel == CONTACT and bool(verified)))


def decide_for_turn(turn, agent, requested: str | None = None) -> Decision:
    """`decide` for a turn already written, from its own initiator."""

    who = turn.initiator_user or turn.initiator_contact
    return decide(agent, who, verified=is_verified(turn), origin=turn.origin,
                  capability=requested, turn=turn)


def grant_facts(turn, agent) -> dict:
    d = decide_for_turn(turn, agent, turn.capability or None)
    if d.access == NONE and turn.capability:
        d = decide_for_turn(turn, agent)   # the stamped capability no longer admits them: as before
    # The capability the turn was STAMPED with at enqueue (a requested one may differ from
    # the free-form one `decide` falls back to).
    why = f"capability:{turn.capability or d.capability}" if d.access == CONFINED else d.basis
    return {"granted_by": why, "role": d.agent_role, "source": d.source,
            "permissions": _held_permissions(d, turn.capability or None),
            "manual_only": bool(d.manual_only)}


def _held_permissions(d, stamped: str | None) -> list[str]:
    # What the asker holds on this agent for THIS turn, from the one decision `d`. A full turn
    # holds everything their role or lines give (closed under the dependency rule, so `write`
    # is in it exactly when the turn is full). A confined turn holds `read` and its
    # capability's `use:<name>` only, even for an owner a sensitive conversation confined.
    # A refusal holds nothing.
    if d.access == NONE:
        return []
    # `system` is not in the role table: it may do what an admin may.
    role = roles.ADMIN if d.agent_role == roles.SYSTEM else d.agent_role
    held = d.permissions or roles.permissions_of(role)
    # `profile` in the envelope is "confined" iff the turn carries a capability stamp, so the
    # stamp confines here too: `profile == full` iff `write` is in the list.
    if d.access == FULL and not stamped:
        return sorted(held)
    name = stamped or d.capability
    if iface_mod.is_read_mode(name):
        # Read mode is `read`, not a host site: nothing is named after it (no `use:read`, no `use:` + its stamp).
        return [roles.READ]
    kept = {p for p in held if p == roles.READ or (p.startswith(f"{roles.USE}:") and p != f"{roles.USE}:*")}
    if name:
        kept.add(roles.use(name))
    return sorted(kept)


def envelope_profile(agent, capability: str, turn=None):
    """`caller_context._profile`, as it was before the envelope read the Turn Context."""
    from apps.agents.interface import profile
    from apps.harness import sensitivity

    if agent is None or not capability:
        return None
    got = profile(agent, capability)
    if "default_profile_version" in got and turn is not None and sensitivity.is_sensitive(turn):
        return {"name": capability, "description": "", "entry": None, "tools": [], "bash": [],
                "read_paths": [], "write_paths": []}
    return got

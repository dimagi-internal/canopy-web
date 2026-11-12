"""THE rule: what one turn may do, resolved from who is asking, the agent and the conversation.

Every word is defined in `docs/architecture/access.md` 
Every entry point that makes an agent do
work calls `resolve_turn_context`, directly or through `for_turn` / `for_asker`, and everything
downstream reads the `TurnContext` it returns, so no two of them can disagree.

    Session Context ─┐
    Asker           ─┼─→ resolve_turn_context() → Turn Context
    Agent settings  ─┘

An agent's access is decided by two things: who the agent itself trusts without a list (its owner, its
admins and canopy itself) and its **access list**
(`apps/agents/grants.py`), which gives everyone else a role. A role is a named bundle of permissions
(`roles.py`), and a line may instead open one named host site (`use:<name>`). The resolver runs:

* **Stage 0, can this session take a message from them.** A preview only: a real turn is already through its send path.
* **Stage 1, standing**, first match: canopy itself, the agent's own login or an approved dispatch
  (`system`); its owner and admins; a session grant (no kind exists now); the access list; otherwise nothing.
* **Stage 2, profile.** A base, then two rules that only narrow. Owner, admin, system, a session grant
  or `write` get `whole_agent`. Someone holding only `read` or named lines gets what they named if it
  opens for them, else (naming nothing) the app entry their conversation is stamped with, else the site
  entry that opens for them, else read mode, else the app entry a line of theirs opens. Anyone else is
  refused. Then a review chat is `session_review` for anyone not refused, and
  sensitive data confines the whole agent to its entry and refuses anything else in a sensitive
  conversation.
* **Stage 3, fan-out**: turn mode, the runner session's suffix, and a true account of credentials.

It writes nothing: `caller_context.build` writes a `PersonAccess` row, and that stays in the envelope's
`person` block outside this module.

`Standing` is what the asker holds on this agent and why, on the result. `standings.Membership` is what
the database says about a person, read in bulk and passed in on `Asker`; `harness.turn_asker.AnsweringTo`
is who a turn's login answers to.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from functools import cached_property

from django.contrib.auth import get_user_model

from apps.contacts.models import Contact
from apps.harness import caller_context as cc
from apps.harness import initiator as initiator_mod
from apps.harness.caller_context import is_verified

from . import app_entries, grants, refusals, roles, sensitive_data
from . import published_profiles as profiles_mod
from . import session_context as sc
from .standings import standing_of

#: How canopy knows the asker, strongest first (defined once, in `caller_context`).
OWNER, ADMIN, MEMBER, CONTACT, SYSTEM = cc.OWNER, cc.ADMIN, cc.MEMBER, cc.CONTACT, cc.SYSTEM

#: `TurnContext.access`.
FULL, CONFINED, REFUSED = "full", "confined", "refused"

#: `Profile.kind`, a closed set.
WHOLE_AGENT = "whole_agent"
READ_MODE = "read_mode"
HOST_SITE = "host_site"
#: An entry bound to one workspace app (`app_entries`): canopy's fixed tools for that app.
APP_ENTRY = "app_entry"
SESSION_REVIEW = "session_review"
DENY_ALL = "deny_all"
NO_AGENT = "no_agent"
PROFILE_KINDS = frozenset({WHOLE_AGENT, READ_MODE, HOST_SITE, APP_ENTRY, SESSION_REVIEW, DENY_ALL, NO_AGENT})

#: What a stage reports when the answer needs something only the claim knows.
PENDING = "pending"

#: `Refusal.code`, a closed set defined once, with its words, in `refusals.Code`: the refusals the rule makes, the
#: mail channel's, then stage 0's.
NOT_ON_ACCESS_LIST = refusals.Code.NOT_ON_ACCESS_LIST
SENDER_NOT_PROVEN = refusals.Code.SENDER_NOT_PROVEN
PROFILE_NOT_OPEN = refusals.Code.PROFILE_NOT_OPEN
SENSITIVE_NOT_OPEN = refusals.Code.SENSITIVE_NOT_OPEN
#: Refused by the mail channel, before the rule: the sender is blocked (`blocked_sender`).
SENDER_BLOCKED = refusals.Code.SENDER_BLOCKED
REFUSAL_CODES = frozenset(refusals.Code)

#: `basis` when a turn is refused, and for a turn with no agent (the envelope's `granted_by`).
REFUSED_BASIS, NO_INTERFACE = "refused", "no-interface"


@dataclass(frozen=True)
class Asker:
    """Who this turn is for, and whether this message proves it (`verified`). `who` is a canopy user,
    a contact or None. `standing` and `lines` are the facts about them when the caller read those in
    bulk (`Standings`), so a view judging many pairs does not read per pair."""

    who: object = None
    verified: bool = False
    standing: object = None
    lines: list | None = None
    #: Whether this asker is at one of a sensitive conversation's chat surfaces (a preview's stage 0).
    chat_surface: bool = True

    @classmethod
    def of_turn(cls, turn) -> Asker:
        return cls(who=turn.initiator_user or turn.initiator_contact, verified=is_verified(turn))

    @property
    def user(self):
        return self.who if isinstance(self.who, get_user_model()) else None

    @property
    def contact(self):
        return self.who if isinstance(self.who, Contact) else None


@dataclass(frozen=True)
class Standing:
    """What the asker holds on this agent, and why."""

    relationship: str            # owner | admin | member | contact | system
    #: Everything their lines grant (read, write, use:<name>, ...). Empty for the owner, admin and
    #: system tiers, who need no list, and for a refusal.
    permissions: frozenset = frozenset()
    #: Owner | admin | editor | read_write | read_only | system, or a workspace's own role. None when
    #: refused, or when they hold only named lines.
    agent_role: str | None = None
    #: `owner` | `admin` | `system` | `session` | `list:<line>`.
    source: str = ""
    #: `source` in words a person can read.
    via: str = ""


@dataclass(frozen=True)
class Review:
    """How the turn is overseen. `mode` and `basis` come from `turn_mode`, whose rules need the claim:
    `PENDING` until then, or None for a turn with no agent."""

    mode: str | None = PENDING
    basis: str = ""
    manual_only: bool = False
    may_request_auto: bool = False
    may_pin_runner: bool = False


#: Why a turn is not run. Non-null exactly when `TurnContext.access == REFUSED`. Its codes and words: `refusals`.
Refusal = refusals.Refusal


@dataclass(frozen=True)
class TraceEntry:
    rule: str
    outcome: str
    detail: str = ""


@dataclass(frozen=True)
class Profile:
    """The complete definition of what a turn can do. For `whole_agent` and `no_agent` nothing is
    enumerated and the lists are None; on any other kind an empty or absent list denies. Only an
    explicit `kind` means the whole agent, never a missing list, profile or name.

    Read when asked, because canopy's default read profile reads the agent's skills, and the roster
    asks the rule per member."""

    kind: str
    #: What a turn in it is stamped: "" for the whole agent.
    name: str = ""
    #: The site a `host_site` serves, None for one with no site.
    site: str | None = None
    #: `"<workspace>/<app key>"` an `app_entry` is bound to, None for any other kind.
    app: str | None = None
    _agent: object = field(default=None, compare=False, repr=False)

    @cached_property
    def _definition(self) -> dict | None:
        if self.kind in (WHOLE_AGENT, NO_AGENT):
            return None
        if self.kind == DENY_ALL:
            # What the envelope already gives a stamp that no longer resolves (`caller_context._profile`).
            return {"name": self.name, "description": "", "entry": None, "tools": [], "bash": [],
                    "read_paths": [], "write_paths": []}
        return profiles_mod.profile(self._agent, self.name)

    def _read(self, key):
        return (self._definition or {}).get(key)

    description = property(lambda self: self._read("description"))
    entry = property(lambda self: self._read("entry"))
    tools = property(lambda self: self._read("tools"))
    bash = property(lambda self: self._read("bash"))
    read_paths = property(lambda self: self._read("read_paths"))
    write_paths = property(lambda self: self._read("write_paths"))
    data = property(lambda self: self._read("data"))
    default_profile_version = property(lambda self: self._read("default_profile_version"))

    def as_envelope(self) -> dict | None:
        """The profile as the runner and its guard read it, None for the whole agent."""
        return self._definition


@dataclass(frozen=True)
class TurnContext:
    """What one turn may do. Always produced, even when refused."""

    standing: Standing
    access: str                       # full | confined | refused
    profile: Profile | None           # None only when refused
    #: WHY in one word: owner | admin | system | session:<role> | grant:<line> | capability:<name> |
    #: sensitive:<name> | refused | no-interface.
    basis: str
    session_key_suffix: str           # "#<name>" for a confined turn, "" otherwise (§ 6)
    review: Review
    #: `Turn.capability` for this answer: "" for the whole agent, a name, or None when refused.
    stamped: str | None
    refusal: Refusal | None
    trace: tuple[TraceEntry, ...]
    _session_ctx: object = field(default=None, compare=False, repr=False)
    _agent: object = field(default=None, compare=False, repr=False)
    _turn: object = field(default=None, compare=False, repr=False)
    _runner: object = field(default=None, compare=False, repr=False)
    #: What the rule itself answered. Under a stamp it can differ from `access`, which is what the turn runs as.
    _reach: object = field(default=None, compare=False, repr=False)

    def __post_init__(self):
        # What the rest of canopy relies on, held at the one place each answer is made.
        if (self.access == REFUSED) != (self.profile is None) or (self.access == REFUSED) != (self.refusal is not None):
            raise ValueError("a Turn Context has a profile exactly when it is not refused, and a refusal "
                             "exactly when it is")
        if self.profile is not None and (self.access == FULL) != (self.profile.kind in (WHOLE_AGENT, NO_AGENT)):
            raise ValueError(f"access {self.access} cannot run a {self.profile.kind} profile")

    @property
    def refused(self) -> bool:
        return self.access == REFUSED

    @cached_property
    def credentials(self):
        """A true statement of what this turn gets, by name and kind, never a value. Read when asked:
        `credentials/for_turn.py` asks functions that themselves ask this rule."""
        from .credentials import for_turn as for_turn_mod  # noqa: PLC0415 — it reads harness modules that read this one

        return for_turn_mod.statement(self, self._agent, self._turn, runner=self._runner)

    @cached_property
    def held_permissions(self) -> list[str]:
        """What the asker holds on this agent for THIS turn (the envelope's `permissions`). A full turn holds
        everything their role or lines give (closed under the dependency rule, so `write` is in it exactly when
        the turn is full). A confined turn holds `read` and its host site's `use:<name>` only, even for an owner
        a sensitive conversation confined. Someone the rule no longer admits holds nothing, whatever the stamp."""
        reach = self._reach
        if reach is None or reach.access == REFUSED:
            return []
        # `system` is not in the role table: it may do what an admin may.
        role = roles.ADMIN if self.standing.agent_role == roles.SYSTEM else self.standing.agent_role
        held = self.standing.permissions or roles.permissions_of(role)
        if reach.access == FULL and not self.stamped:
            return sorted(held)
        name = self.stamped or reach.name or ""
        if profiles_mod.is_read_mode(name):
            # Read mode is `read`, not a host site: nothing is named after it (no `use:read`, no
            # `use:<READ_MODE_STAMP>`).
            return [roles.READ]
        kept = {p for p in held if p == roles.READ or (p.startswith(f"{roles.USE}:") and p != f"{roles.USE}:*")}
        if name:
            kept.add(roles.use(name))
        return sorted(kept)


# --- stages 1 and 2: the answer, as it is worked out ---------------------------------------------

@dataclass(frozen=True)
class _Reach:
    relationship: str
    access: str = REFUSED
    name: str | None = None
    basis: str = REFUSED_BASIS
    may_request_auto: bool = False
    may_pin_runner: bool = False
    manual_only: bool = False
    agent_role: str | None = None
    source: str = ""
    via: str = ""
    permissions: frozenset = frozenset()
    #: When refused: its `refusals.Code`, and the few facts its words name (`refusals.make`).
    code: str = ""
    facts: tuple = ()


def _no_list_stage(agent, asker: Asker, session_ctx, turn, trace: list) -> _Reach:
    """Stage 1, the part that needs no list: the owner, the admins and canopy itself have the whole agent,
    and so would someone a session grant names (no kind exists now). Everyone else goes on to the access list."""
    st = asker.standing
    user = None
    if turn is not None:
        rel = cc.relationship(turn, agent)
        user = turn.initiator_user if turn.initiator_kind == initiator_mod.USER else None
    elif asker.user is not None:
        user = asker.user
        st = st or standing_of(user, agent)
        rel = cc.relationship_for_user(user, agent, role=st.role, granted=st.granted)
    else:
        rel = CONTACT

    if user is not None and st is None:
        st = standing_of(user, agent)
    is_admin = user is not None and agent.is_admin(user, role=st.role, granted=st.granted)

    if rel in (OWNER, ADMIN, SYSTEM):
        role = {OWNER: roles.OWNER, ADMIN: roles.ADMIN, SYSTEM: roles.SYSTEM}[rel]
        trace.append(TraceEntry("trusted_without_a_list", rel))
        return _Reach(relationship=rel, access=FULL, basis=rel, agent_role=role, source=rel,
                      may_request_auto=bool(is_admin and asker.verified),
                      may_pin_runner=is_admin or rel in (OWNER, ADMIN))
    trace.append(TraceEntry("trusted_without_a_list", "no", f"relationship {rel}"))
    if user is not None:
        # The seam (spec § 3.4): the one place a session grant applies. No kind exists now
        # (there is no `session_writer`), so this always finds none; a new kind is a builder
        # in `session_context.SessionContext.session_grants`, and is applied here as it was: someone a
        # grant names drives this conversation, nothing gives them the agent itself, so their
        # permissions (what the envelope and the own-repo rail read) stop at `write`.
        grant = next((g for g in session_ctx.session_grants if g.user_id == user.pk), None)
        if grant is not None:
            trace.append(TraceEntry("session_grants", grant.kind, grant.source))
            return _Reach(relationship=rel, access=FULL, basis=f"session:{grant.role}", agent_role=roles.EDITOR,
                          source="session", permissions=roles.ROLES.close({roles.WRITE}))
        trace.append(TraceEntry("session_grants", "none"))
    return _Reach(relationship=rel, code=NOT_ON_ACCESS_LIST,
                  facts=(("automated", rel == CONTACT and bool(asker.verified)),))


def _list_asker(asker: Asker, turn) -> grants.Identity:
    """The asker as the access list sees them: a canopy user, or a contact known by address."""
    user, contact, verified = asker.user, asker.contact, asker.verified
    if turn is not None:
        user = turn.initiator_user if turn.initiator_kind == initiator_mod.USER else None
        contact = turn.initiator_contact if turn.initiator_kind == initiator_mod.CONTACT else None
        verified = is_verified(turn)
    email = (getattr(user, "email", "") or getattr(contact, "email", "") or "").strip().lower()
    return grants.Identity(user=user, email=email, verified=bool(verified), contact=contact is not None,
                        workspace_roles=getattr(asker.standing, "roles", None),
                        descendants=getattr(asker.standing, "descendants", None))


def _selected_entry(iface: dict, opens, session_ctx) -> str | None:
    """The entry this conversation's site selects, if this asker can open it. One they cannot open is
    skipped, not fatal: read mode may still admit them."""
    return profiles_mod.site_entry(session_ctx.site, iface, opens)


def _app_selected(agent, opens, session_ctx) -> str | None:
    """The app entry this conversation's app stamp selects, if this asker can open it. Skipped, not fatal."""
    name = app_entries.selected_entry(agent, session_ctx.app)
    caps = (getattr(agent, "interface", None) or {}).get("capabilities") or {}
    return name if name is not None and opens(name, caps.get(name)) else None


def _app_by_line(iface: dict, permissions) -> str | None:
    """For someone whose lines open no read mode: the app entry a `use:<key>` line of theirs opens, so a person
    let into one app reaches the agent there on every channel (email, chat), and in nothing else. The first by
    name when several do."""
    return next((n for n in sorted(app_entries.entries(iface)) if profiles_mod.opens(permissions, n)), None)


def _entry_for(agent, iface: dict, permissions, requested, session_ctx, trace: list) -> str | None:
    """The entry someone without `write` is held to: the one named, else the app entry its conversation is
    stamped with, else the site's, else read mode, else the app entry a line of theirs names, among those
    their permissions open. Read mode exists on every agent; any other name must be published, and an unknown
    or retired one is refused, never widened. A `read` line also opens the app entry of the conversation it
    is stamped with: inside an app's own chat, the app entry is preferred to read mode."""
    def opens(name, cap):
        if profiles_mod.opens(permissions, name):
            return True
        return (app_entries.is_app_entry(cap) and roles.holds(permissions, roles.READ)
                and app_entries.selected_entry(agent, session_ctx.app) == name)

    if requested == profiles_mod.SESSION_REVIEW:
        # Canopy's own, for a turn in a review chat only: `read` opens it.
        allowed = session_ctx.is_review and roles.holds(permissions, roles.READ)
        trace.append(TraceEntry("entry", requested if allowed else "not open", "review chat"))
        return requested if allowed else None
    name, how = requested, "named"
    if name is None:
        name, how = _app_selected(agent, opens, session_ctx), "app"
    if name is None:
        name, how = _selected_entry(iface, opens, session_ctx), "site"
    if name is None and not profiles_mod.opens(permissions, profiles_mod.READ_MODE_STAMP):
        name, how = _app_by_line(iface, permissions), "app line"
    if name is None:
        name, how = profiles_mod.READ_MODE_STAMP, "read mode"
    published, cap = profiles_mod.published_profile(iface, name)
    if published is None and profiles_mod.is_read_mode(name):
        published, cap = profiles_mod.READ_MODE_NAME, {}   # {}: canopy's default, built when it is read
    allowed = cap is not None and opens(published, cap)
    trace.append(TraceEntry("entry", published if allowed else "not open", f"{how}: {name}"))
    return published if allowed else None


def _base_stage(agent, reach: _Reach, asker: Asker, requested, session_ctx, turn, trace: list) -> _Reach:
    """Stage 1's access list, then stage 2's base."""
    if reach.access == FULL:
        return reach
    who = _list_asker(asker, turn)
    lines = asker.lines if asker.lines is not None else getattr(asker.standing, "lines", None)
    got = grants.resolve(agent, who, lines)
    if not got.permissions:
        trace.append(TraceEntry("access_list", "holds nothing"))
        if not who.verified and grants.resolve(agent, replace(who, verified=True), lines).permissions:
            # Only what it is told: the same answer, said truly. A line names them, but only for a proven message.
            return replace(reach, code=SENDER_NOT_PROVEN, facts=())
        return reach
    iface = getattr(agent, "interface", None) or {}
    if roles.holds(got.permissions, roles.WRITE):
        # `write` is driving the whole agent, whatever the role is called. What makes someone an agent
        # ADMIN is still `Agent.is_admin`, never a list line, so a line never raises `may_request_auto`.
        line = got.line_for(roles.WRITE)
        trace.append(TraceEntry("access_list", "write", got.basis))
        return replace(reach, access=FULL, manual_only=got.manual_only, basis=got.basis, agent_role=got.role,
                       source=f"list:{line.subject_key}", via=grants.describe(line), permissions=got.permissions,
                       code="", facts=())
    trace.append(TraceEntry("access_list", ",".join(sorted(got.permissions)), got.basis))
    name = _entry_for(agent, iface, got.permissions, requested, session_ctx, trace)
    if name is None:
        published = bool(requested) and (profiles_mod.published_profile(iface, requested)[0] is not None
                                         or profiles_mod.is_read_mode(requested)
                                         or requested == profiles_mod.SESSION_REVIEW)
        return replace(reach, code=PROFILE_NOT_OPEN, facts=(("name", requested), ("published", published)))
    # The review entry is opened by `read`; `line_opening` knows only named entries and read mode.
    # A `read` line opens the app entry of its own conversation without naming it (`_entry_for`).
    line = (got.line_for(roles.READ) if name == profiles_mod.SESSION_REVIEW
            else got.line_opening(name) or got.line_for(roles.READ))
    return replace(reach, access=CONFINED, name=name, basis=f"capability:{name}", agent_role=got.role,
                   source=f"list:{line.subject_key}", via=grants.describe(line), permissions=got.permissions,
                   code="", facts=())


def _review_rule(reach: _Reach, trace: list) -> _Reach:
    """A turn in a one-session review's chat runs in its own profile, for everyone with any access at
    all: owner, admin, editor and canopy too. It only narrows. That chat holds a transcript someone may
    have written to steer whoever reads it, so no later message there picks up the agent's whole
    profile, nor a login, a token or a send."""
    if reach.access == REFUSED:
        trace.append(TraceEntry("review_chat", "already refused"))
        return reach
    trace.append(TraceEntry("review_chat", profiles_mod.SESSION_REVIEW))
    return replace(reach, access=CONFINED, name=profiles_mod.SESSION_REVIEW, may_request_auto=False,
                   may_pin_runner=False, basis=f"capability:{profiles_mod.SESSION_REVIEW}")


def _sensitive_entry(iface: dict, requested, session_ctx) -> str | None:
    """The `data: sensitive` entry this request is in: the one it names, else the one its conversation's
    site selects. Who is asking is not checked here."""
    if requested:
        # `read` and `READ_MODE_STAMP` are one thing: naming either finds the agent's published read-mode
        # profile, and if that handles sensitive data, naming `read` must not dodge the rule.
        published, cap = profiles_mod.published_profile(iface, requested)
        return published if (cap or {}).get("data") == sensitive_data.SENSITIVE else None
    for name, cap in (iface.get("capabilities") or {}).items():
        if cap.get("data") != sensitive_data.SENSITIVE:
            continue
        if profiles_mod.names_site(cap, session_ctx.site):
            return name
    return None


def _sensitive_rule(agent, reach: _Reach, requested, session_ctx, trace: list) -> _Reach:
    """Sensitive data is confined for everyone: in a `data: sensitive` entry, whoever would have had
    the whole agent runs that entry instead, with the same role, mode and pin rights. A confined caller
    keeps what the base gave them. A turn in a sensitive conversation that ends up anywhere but its
    sensitive entry is refused, never run whole or in another one."""
    iface = getattr(agent, "interface", None) or {}
    entry = _sensitive_entry(iface, requested, session_ctx)
    if entry is None and not session_ctx.sensitive:
        trace.append(TraceEntry("sensitive", "not sensitive"))
        return reach
    if entry is not None and reach.access == FULL:
        trace.append(TraceEntry("sensitive", entry, "the whole agent is confined to its sensitive entry"))
        return replace(reach, access=CONFINED, name=entry, basis=f"sensitive:{entry}")
    if reach.access == REFUSED or not session_ctx.sensitive:
        trace.append(TraceEntry("sensitive", "no change"))
        return reach
    if entry is not None and reach.access == CONFINED and reach.name == entry:
        trace.append(TraceEntry("sensitive", entry, "already in its sensitive entry"))
        return reach
    trace.append(TraceEntry("sensitive", REFUSED, "not in a sensitive entry"))
    return replace(reach, access=REFUSED, name=None, may_request_auto=False, may_pin_runner=False,
                   manual_only=False, basis=REFUSED_BASIS, agent_role=None, source="",
                   permissions=frozenset(), code=SENSITIVE_NOT_OPEN, facts=(("entry", entry),))


def _rules(agent, asker: Asker, session_ctx, requested, turn, trace: list) -> _Reach:
    """Stages 1 and 2, in the order they have always run."""
    if session_ctx.is_review:
        requested = profiles_mod.SESSION_REVIEW
    reach = _no_list_stage(agent, asker, session_ctx, turn, trace)
    reach = _base_stage(agent, reach, asker, requested, session_ctx, turn, trace)
    if session_ctx.is_review:
        reach = _review_rule(reach, trace)
    return _sensitive_rule(agent, reach, requested, session_ctx, trace)


# --- the result ------------------------------------------------------------------------------------

def _kind_of(name: str, agent=None) -> str:
    if name == profiles_mod.SESSION_REVIEW:
        return SESSION_REVIEW
    if app_entries.app_of_entry(getattr(agent, "interface", None), name):
        return APP_ENTRY
    return READ_MODE if profiles_mod.is_read_mode(name) else HOST_SITE


def _site_of(agent, name: str, session_ctx) -> str | None:
    caps = (getattr(agent, "interface", None) or {}).get("capabilities") or {}
    sites = (caps.get(name) or {}).get("sites") or []
    if session_ctx.site in sites:
        return session_ctx.site
    return sites[0] if sites else None


def _profile_of(agent, name: str, session_ctx) -> Profile:
    kind = _kind_of(name, agent)
    app = app_entries.app_of_entry(getattr(agent, "interface", None), name) if kind == APP_ENTRY else None
    return Profile(kind=kind, name=name, _agent=agent,
                   site=_site_of(agent, name, session_ctx) if kind == HOST_SITE else None,
                   app=app_entries.stamp_of(agent.workspace_id, app) if app else None)


def stamped_profile(agent, stamp: str, session_ctx) -> Profile:
    """The profile a turn stamped `stamp` runs in, whatever the rules say now. A stamp that no longer
    resolves is `deny_all`, never the whole agent, and so is a sensitive turn stamped with read mode
    that has since gone: canopy's default read profile is not what it was admitted to."""
    if stamp == profiles_mod.FULL_STAMP:
        return Profile(kind=WHOLE_AGENT, _agent=agent)
    if stamp == profiles_mod.SESSION_REVIEW:
        return _profile_of(agent, stamp, session_ctx)
    published, _cap = profiles_mod.published_profile(getattr(agent, "interface", None), stamp)
    if published is None and (not profiles_mod.is_read_mode(stamp) or session_ctx.sensitive):
        return Profile(kind=DENY_ALL, name=stamp, _agent=agent)
    # Stamped under its own name: an agent that later publishes the other read-mode name keeps the key.
    return _profile_of(agent, stamp, session_ctx)


def _review_of(reach: _Reach, turn, runner) -> Review:
    """Turn mode's rules are not here. A turn already claimed reads its stamp; the claim asks them
    afresh, because they need what only it knows; anything else is `PENDING`."""
    mode, basis = PENDING, ""
    if turn is not None and turn.turn_mode:
        mode, basis = turn.turn_mode, turn.turn_mode_basis or ""
    elif turn is not None and runner is not None:
        from apps.harness import turn_mode  # noqa: PLC0415 — turn_mode asks this rule

        resolved = turn_mode.for_turn(turn, fresh=True)
        if resolved is not None:
            mode, basis = resolved.mode, resolved.basis
    return Review(mode=mode, basis=basis, manual_only=reach.manual_only, may_request_auto=reach.may_request_auto,
                  may_pin_runner=reach.may_pin_runner)


def _finish(agent, reach: _Reach, session_ctx, turn, runner, trace: list) -> TurnContext:
    standing = Standing(relationship=reach.relationship, permissions=reach.permissions,
                        agent_role=reach.agent_role, source=reach.source, via=reach.via)
    if reach.access == REFUSED:
        profile, stamped, suffix = None, None, ""
        refusal = refusals.make(reach.code, agent=agent, session=session_ctx._session, **dict(reach.facts))
    elif reach.access == FULL:
        profile, stamped, suffix, refusal = Profile(kind=WHOLE_AGENT, _agent=agent), profiles_mod.FULL_STAMP, "", None
    else:
        profile, stamped, suffix, refusal = (_profile_of(agent, reach.name, session_ctx), reach.name,
                                             f"#{reach.name}", None)
    return TurnContext(standing=standing, access=reach.access, profile=profile, basis=reach.basis,
                       session_key_suffix=suffix, review=_review_of(reach, turn, runner), stamped=stamped,
                       refusal=refusal, trace=tuple(trace), _session_ctx=session_ctx, _agent=agent,
                       _turn=turn, _runner=runner, _reach=reach)


def _send_refusal(session_ctx, asker: Asker, turn, trace: list) -> Refusal | None:
    """Stage 0. A real turn is already through its send path: asking here would refuse turns no send path
    refuses today (an email, a schedule, a turn someone else's rule let in), and this project changes no
    one's access."""
    if turn is not None or session_ctx.session_id is None:
        return None
    block = next((b for b in session_ctx.send_blocks if b.applies(asker)), None)
    trace.append(TraceEntry("send_blocks", block.code if block is not None else "none",
                            block.why if block is not None else ""))
    return block.refusal if block is not None else None


def _relationship_without_agent(asker: Asker, session_ctx, turn) -> str:
    if turn is not None:
        return cc.relationship(turn, None)
    session = session_ctx._session
    return cc.session_relationship(asker.user, session) if asker.user is not None and session is not None else CONTACT


def _no_agent(asker: Asker, session_ctx, turn, stamp, trace: list) -> TurnContext:
    """A person's own repo or project session. Its enforcement and credentials are what they are today,
    and this only names them: there is no agent to hold a standing on.

    Not a turn stamped with a profile whose agent has since gone (`Session.agent` is SET_NULL): it was
    admitted to something confined, and no agent left to read that from must never read as the whole of
    anything."""
    rel = _relationship_without_agent(asker, session_ctx, turn)
    trace.append(TraceEntry("no_agent", rel))
    review = Review(mode=None) if turn is None else _review_without_agent(turn)
    if stamp:
        trace.append(TraceEntry("stamp", DENY_ALL, stamp))
        return TurnContext(standing=Standing(relationship=rel), access=CONFINED, profile=Profile(kind=DENY_ALL, name=stamp),
                           basis=NO_INTERFACE, session_key_suffix=f"#{stamp}", review=review, stamped=stamp, refusal=None,
                           trace=tuple(trace), _session_ctx=session_ctx, _turn=turn)
    return TurnContext(standing=Standing(relationship=rel), access=FULL, profile=Profile(kind=NO_AGENT),
                       basis=NO_INTERFACE, session_key_suffix="", review=review, stamped=profiles_mod.FULL_STAMP,
                       refusal=None,
                       trace=tuple(trace), _session_ctx=session_ctx, _turn=turn)


def _review_without_agent(turn) -> Review:
    return Review(mode=turn.turn_mode or None, basis=turn.turn_mode_basis or "")


def resolve_turn_context(session_ctx, asker: Asker, agent, *, requested: str | None = None, turn=None,
                         stamp: str | None = None, runner=None) -> TurnContext:
    """THE rule. See the module docstring. It writes nothing.

    `requested` is the profile the asker named. `turn`, when given, supplies what only a turn knows: its
    initiator and the conversation it continues.

    **Two moments.** Without `stamp` this is the question the enqueue asks, the claim asks again (`at_claim`) and a
    preview asks. `stamp` is `Turn.capability` once the claim has written it: the stored profile stands for the rest
    of the turn, while who is asking is still read live (`of_turn`). `runner` is what only the claim knows.
    """
    trace: list[TraceEntry] = []
    blocked = _send_refusal(session_ctx, asker, turn, trace)
    if agent is None:
        return _no_agent(asker, session_ctx, turn, stamp, trace) if blocked is None else _blocked_without_agent(
            asker, session_ctx, blocked, turn, trace)
    if stamp is not None:
        return _at_claim(agent, asker, session_ctx, stamp, turn, runner, trace)
    reach = _rules(agent, asker, session_ctx, requested, turn, trace)
    if blocked is not None:
        # Who they are is still true: only this message cannot be taken.
        reach = replace(reach, access=REFUSED, name=None, basis=REFUSED_BASIS, code=blocked.code, facts=(),
                        may_request_auto=False, may_pin_runner=False, manual_only=False)
        return replace(_finish(agent, reach, session_ctx, turn, runner, trace), refusal=blocked)
    return _finish(agent, reach, session_ctx, turn, runner, trace)


def _blocked_without_agent(asker: Asker, session_ctx, refusal: Refusal, turn, trace: list) -> TurnContext:
    # Who they are is still true, as on a turn with an agent.
    rel = _relationship_without_agent(asker, session_ctx, turn)
    return TurnContext(standing=Standing(relationship=rel), access=REFUSED, profile=None, basis=REFUSED_BASIS,
                       session_key_suffix="", review=Review(mode=None), stamped=None, refusal=refusal,
                       trace=tuple(trace), _session_ctx=session_ctx)


def _at_claim(agent, asker: Asker, session_ctx, stamp: str, turn, runner, trace: list) -> TurnContext:
    """The stamp decides the profile. The asker's standing is still read live, as the envelope reads it:
    under the stamp, and when that no longer admits them, as they stand now."""
    reach = _rules(agent, asker, session_ctx, stamp or None, turn, trace)
    if reach.access == REFUSED and stamp:
        trace.append(TraceEntry("stamp", "no longer admits them", stamp))
        reach = _rules(agent, asker, session_ctx, None, turn, trace)
    profile = stamped_profile(agent, stamp, session_ctx)
    trace.append(TraceEntry("stamp", profile.kind, stamp))
    access = FULL if profile.kind == WHOLE_AGENT else CONFINED
    # As the envelope words it: a stamp may differ from the free-form entry the rule fell back to.
    basis = f"capability:{stamp or reach.name}" if reach.access == CONFINED else reach.basis
    standing = Standing(relationship=reach.relationship, permissions=reach.permissions,
                        agent_role=reach.agent_role, source=reach.source, via=reach.via)
    return TurnContext(standing=standing, access=access, profile=profile, basis=basis,
                       session_key_suffix=f"#{stamp}" if stamp else "", review=_review_of(reach, turn, runner),
                       stamped=stamp, refusal=None, trace=tuple(trace), _session_ctx=session_ctx,
                       _agent=agent, _turn=turn, _runner=runner, _reach=reach)


# --- how entry points ask --------------------------------------------------------------------------

def for_turn(turn, agent, requested: str | None = None) -> TurnContext:
    """The rule for a turn already written or in memory, from its own initiator."""
    return resolve_turn_context(sc.build(None, turn, agent=agent), Asker.of_turn(turn), agent,
                                requested=requested, turn=turn)


def for_asker(agent, who=None, *, verified: bool, requested: str | None = None, turn=None, standing=None,
              lines=None, channel: str = "") -> TurnContext:
    """The rule for a question with no turn (`channel` says where it would arrive, for the record: the
    rule does not vary by it), or about a turn in hand when the caller knows better than the turn how
    far this message is proven. `standing` and `lines` as on `Asker`."""
    session_ctx = sc.build(None, turn, agent=agent) if turn is not None else sc.empty(channel=channel)
    return resolve_turn_context(session_ctx, Asker(who=who, verified=verified, standing=standing, lines=lines),
                                agent, requested=requested, turn=turn)


def of_turn(turn, agent, *, runner=None) -> TurnContext:
    """What a claimed turn runs as: its stamp, which the claim wrote from the rules as they were then (`at_claim`), and
    which stands for the rest of the turn (§ 3.3, "Two moments"). The envelope and the stored snapshot read this;
    `for_turn` is the question of what the rules say. `runner` is the box that just claimed it."""
    return resolve_turn_context(sc.build(None, turn, agent=agent), Asker.of_turn(turn), agent, turn=turn,
                                stamp=turn.capability, runner=runner)


#: `Turn.origin_ref` key the enqueue writes, and only the server (`harness.services.enqueue_turn` drops a caller's), so
#: the claim can put the enqueue's own question to the rule again: the agent the turn was sent to and the profile its
#: sender named, or None.
QUEUED_FOR = "queued_for"


def queued_for(agent, requested: str | None) -> dict:
    return {"agent": agent.pk, "profile": requested}


def requested_of(turn) -> str | None:
    """The profile this turn's sender named. A turn queued before the enqueue kept it names its stamp, which asks for
    nothing wider than the stamp itself."""
    queued = (turn.origin_ref or {}).get(QUEUED_FOR)
    if isinstance(queued, dict):
        return queued.get("profile") or None
    return turn.capability or None


def at_claim(turn) -> TurnContext:
    """§ 3.3, "Two moments": a queued turn runs on the rules as they are when it starts, like any other turn. The claim
    puts the enqueue's question to the rule again, from today's facts: the conversation may have become sensitive, the
    sender may have lost their line or role, read mode may have changed. Its answer is what the turn runs as, and the
    claim stamps it; a refusal here closes the turn as one at the enqueue would have.

    A turn sent to an agent that was deleted while it waited (`Session.agent` is SET_NULL) is refused, never run as a
    turn with no agent. One that never had an agent is what it was. So is one no enqueue ever asked about: it names
    no initiator at all (every turn `enqueue_turn` writes names one, `tests/test_turn_initiator.py`), so there is no
    asker to put the question to again, and its stamp is all anyone decided."""
    agent = turn.acting_agent
    if agent is not None:
        if not turn.initiator_kind and not isinstance((turn.origin_ref or {}).get(QUEUED_FOR), dict):
            return of_turn(turn, agent)
        return for_turn(turn, agent, requested_of(turn))
    queued = (turn.origin_ref or {}).get(QUEUED_FOR)
    if not isinstance(queued, dict) or queued.get("agent") is None:
        return of_turn(turn, None)
    refusal = refusals.make(refusals.Code.AGENT_GONE)
    return TurnContext(standing=Standing(relationship=cc.relationship(turn, None)), access=REFUSED, profile=None,
                       basis=REFUSED_BASIS, session_key_suffix="", review=Review(mode=None), stamped=None,
                       refusal=refusal, trace=(TraceEntry("agent", REFUSED, refusal.why),),
                       _session_ctx=sc.build(None, turn), _turn=turn)


def was_refused(turn) -> bool:
    """Was this turn refused before it ever ran? Such a turn has no stamp, and no stamp reads as the whole
    agent, so nothing may ask `of_turn` what it ran as without asking this first. It is told by what was stored
    (`resolved_context`) and, on a turn older than that, by the note the refusal wrote."""
    from apps.harness.models import Turn  # noqa: PLC0415 — harness imports this package

    stored = turn.resolved_context if isinstance(turn.resolved_context, dict) else {}
    if stored.get("access") == REFUSED:
        return True
    return (turn.status == Turn.CANCELLED and turn.claimed_at is None
            and (turn.result_note or "").startswith(REFUSED_NOTE_PREFIX))


#: How a refused turn's note begins (`harness.services._apply_capability`, `channels.email`).
REFUSED_NOTE_PREFIX = refusals.REFUSED_NOTE_PREFIX


def blocked_sender(turn, agent) -> TurnContext:
    """A blocked sender's mail, refused on the way in, before the rule is asked. Never carries why they were
    blocked: that is an admin's note about a person, and it stays on the contact."""
    refusal = refusals.make(SENDER_BLOCKED, agent=agent)
    return TurnContext(standing=Standing(relationship=CONTACT), access=REFUSED, profile=None, basis=REFUSED_BASIS,
                       session_key_suffix="", review=Review(mode=None), stamped=None, refusal=refusal,
                       trace=(TraceEntry("blocked_sender", REFUSED, refusal.why),),
                       _session_ctx=sc.empty(channel=turn.origin), _agent=agent, _turn=turn)

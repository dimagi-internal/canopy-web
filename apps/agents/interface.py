"""An agent's DECLARED INTERFACE: what it offers people who are not its admins.

Phase 4 of `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-
and-access-design.md` (§4). An agent has two relationships (§3): its owner and
admins reach its full working session; everyone else — a workspace member, an
emailer, a widget visitor — is a CALLER, and reaches only what the agent
declares here. It is LIVE STATE held by canopy-web (see "Stored on canopy-web"
below) — never a file in the agent's repo — and canopy enforces it.

**Allowlist by construction.** Restricting a fully-powered agent per caller —
instructions, a policy file, a hook with a denylist — leaks. So a caller turn
runs in its capability's profile, where everything not listed is denied:

    capabilities:
      ask:                                  # the free-form door every channel uses
        description: Ask ACE about your programme.
        callers: [contact:verified, member] # who may invoke it
        entry: /ace:ask --thread {thread_id}  # the session's prompt (optional)
        tools: [Read, Grep, "mcp__canopy-web__who_is_asking"]
        bash: ["canopy email read --repo . {thread_id}"]
        read_paths: ["{cwd}/**"]
    callers_default: none

**A page can pick the door.** A capability may name the resources it serves,
and a conversation held on a page that declared one of them runs there instead
of in `ask`:

    capabilities:
      marketplace:
        callers: [contact]
        pages: ["labs-marketplace://*"]   # what the PAGE declared it is showing
        tools: [Skill, "mcp__*canopy-web__current_page",
                "mcp__*connect_labs__marketplace_*"]

This exists because an embedded page already publishes a contract — `setPageState`
says what it is showing and which tool resolves those rows — and until now nothing
could answer it. Every free-form channel collapsed to `ask`, so an agent embedded
in a page had one door shared with its email, and the only way to give the page
what it needed was to give an email stranger the same thing. That is the wrong
trade in both directions: the page's conversation is under-equipped and the
mailbox is over-exposed.

It cannot widen anything on the host's word. The page supplies only the resource
it is showing; every tool in the profile was chosen by the agent's owner. A host
that lies about its resource reaches exactly the tools that owner already decided
to expose to that page — and it had to be an allowlisted app, framing from a
registered origin, to open a conversation at all.

**Default deny.** An agent that has published no interface is reachable by its
workspace's members (in its full profile, as before) and by nobody else: a
contact or an unidentified caller is refused. Until 2026-09-26 it ran EVERY
turn full, which made the enforcement below opt-in — and on that day Hal, with
no interface, pushed and deployed code for a colleague on Slack who was not a
member of its workspace. Publishing an interface is how an agent lets anyone
outside in. A capability with no `pages` is never selected by a page, so an
existing interface behaves exactly as it did.

Caller classes: `member` (a workspace member who is not an admin), `contact`
(someone canopy knows who is not a member), `unknown` (nobody established who).
`@domain.tld` narrows one to addresses at exactly that domain; `:verified`
additionally requires THIS message to be verified — for a contact, mail that is
DMARC-aligned or DKIM-signed by its own From: domain.

**`:verified` is mail-only for a contact**, so it can never admit a visitor from
an embedded site: the host vouches for them, canopy verifies the host's
signature rather than the person, and such an arrival is capped at tier 2 by
design. Gate an embedded agent's interface on `contact`, not
`contact:verified` — the latter reads like a slightly stricter rule and is in
fact one nobody can ever pass.

**`full:` — domain-wide access.** A list of caller classes that get the agent's
WHOLE profile, as its admins do, e.g. `full: [contact@dimagi.com:verified]`:
staff writing in from a verified company address steer the agent exactly as
before interfaces existed, while everyone else is confined to a capability.

**Stored on canopy-web, not in the agent's repo.** Who may do what is live
state an owner changes as people come and go, not code that ships with the
agent; it is edited on the agent's page (or `canopy agent interface set`).
"""
from __future__ import annotations

import re

from apps.harness import initiator as who

VERSION = 1

#: The fallback door for a free-form channel (email, chat, Slack, a widget on a
#: page that declared nothing). Named capabilities are for callers that ask for
#: something specific, or for a page whose declared resource matches one's
#: `pages` — see `_page_capability`.
ASK = "ask"

#: `Turn.capability` for a turn in the agent's FULL profile. Empty so that every
#: turn predating this reads as "full", which is what it was.
FULL = ""

CALLER_CLASSES = frozenset({"member", "contact", "unknown"})

#: `base[@domain][:verified]` — `contact@dimagi.com:verified` is a contact whose
#: address is at dimagi.com AND whose message proves it. The domain is matched
#: EXACTLY: not a subdomain, not a suffix, so `@dimagi.com` never admits
#: `dimagi.com.evil.org` or `x.dimagi.com`.
_CALLER = re.compile(r"^(?P<base>member|contact|unknown)"
                     r"(?:@(?P<domain>[a-z0-9-]+(?:\.[a-z0-9-]+)*\.[a-z]{2,}))?"
                     r"(?::(?P<verified>verified))?$")

#: What a capability's `input` fields may be — each becomes a typed, required
#: parameter of the capability's MCP tool (apps/mcp/agent_tools.py).
INPUT_TYPES = {"string": "string", "integer": "integer", "number": "number", "boolean": "boolean"}
#: Parameters every capability tool already has; an `input` field may not shadow one.
_RESERVED_INPUTS = frozenset({"message", "conversation_id", "wait_seconds"})
_NAME = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
_MAX_ITEMS = 100
_MAX_PATTERN = 300


class InterfaceError(ValueError):
    pass


def _strings(value, field: str, cap: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > _MAX_ITEMS:
        raise InterfaceError(f"{cap}.{field} must be a list of at most {_MAX_ITEMS} strings")
    out = []
    for v in value:
        if not isinstance(v, str) or not v.strip() or len(v) > _MAX_PATTERN:
            raise InterfaceError(f"{cap}.{field} entries must be non-empty strings "
                                 f"of at most {_MAX_PATTERN} characters")
        out.append(v.strip())
    return out


def parse(doc) -> dict:
    """Validate and normalise a published interface. Raises InterfaceError.

    Strict on purpose: an unknown key is refused rather than ignored, because
    a misspelt `tool:` silently dropped would publish a profile that allows
    less than its author thinks — and a misspelt `callers:` one that allows
    nobody, or everybody, depending on the default.
    """
    if not isinstance(doc, dict):
        raise InterfaceError("the interface must be a mapping")
    extra = set(doc) - {"capabilities", "callers_default", "version", "full"}
    if extra:
        raise InterfaceError(f"unknown top-level key(s): {sorted(extra)}")
    if doc.get("callers_default", "none") != "none":
        # The only default that is safe to publish. Named so that a reader of
        # the file sees the rule, and so that "everyone" is never one edit away.
        raise InterfaceError("callers_default must be 'none'")
    caps = doc.get("capabilities") or {}
    if not isinstance(caps, dict) or len(caps) > 50:
        raise InterfaceError("capabilities must be a mapping of at most 50 entries")
    out: dict = {}
    for name, cap in caps.items():
        if not isinstance(name, str) or not _NAME.match(name):
            raise InterfaceError(f"capability name {name!r} must match {_NAME.pattern}")
        if not isinstance(cap, dict):
            raise InterfaceError(f"{name} must be a mapping")
        bad = set(cap) - {"description", "callers", "entry", "tools", "bash", "read_paths",
                          "input", "pages"}
        if bad:
            raise InterfaceError(f"{name}: unknown key(s) {sorted(bad)}")
        callers = _classes(cap.get("callers"), f"{name}.callers")
        entry = cap.get("entry")
        if entry is not None and (not isinstance(entry, str) or not entry.startswith("/")
                                  or "\n" in entry or len(entry) > _MAX_PATTERN):
            raise InterfaceError(f"{name}.entry must be a one-line slash command")
        inputs = cap.get("input") or {}
        if not isinstance(inputs, dict) or len(inputs) > 20:
            raise InterfaceError(f"{name}.input must be a mapping of at most 20 fields")
        for field, typ in inputs.items():
            if not isinstance(field, str) or not _NAME.match(field) or field in _RESERVED_INPUTS:
                raise InterfaceError(f"{name}.input: {field!r} must match {_NAME.pattern} "
                                     f"and not be one of {sorted(_RESERVED_INPUTS)}")
            if typ not in INPUT_TYPES:
                raise InterfaceError(f"{name}.input.{field}: type must be one of {sorted(INPUT_TYPES)}")
        out[name] = {
            "input": dict(inputs),
            "description": str(cap.get("description") or "")[:500],
            "callers": callers,
            "entry": entry,
            "tools": _strings(cap.get("tools"), "tools", name),
            "bash": _strings(cap.get("bash"), "bash", name),
            "read_paths": _strings(cap.get("read_paths"), "read_paths", name),
            "pages": _pages(cap.get("pages"), name),
        }
    full = _classes(doc.get("full"), "full")
    return {"version": VERSION, "full": full, "capabilities": out, "callers_default": "none"}


def _pages(value, cap: str) -> list[str]:
    """Resource patterns a capability serves, e.g. `labs-marketplace://*`.

    An embedded page already declares what it is showing (`setPageState`), and
    that declaration is server-stored and server-versioned. This is how an agent
    ANSWERS it: the page says which room it is in, the owner says what is in that
    room. A `*` is the only wildcard, matched with `fnmatch`, and a bare `*` is
    refused — "any page at all" is what leaving `pages` off already means, and
    spelling it as a pattern reads like a narrowing while being the opposite.
    """
    out = _strings(value, "pages", cap)
    for pattern in out:
        if pattern == "*":
            raise InterfaceError(
                f"{cap}.pages: '*' matches every page, which is what omitting "
                "`pages` does — name the resources this capability is for")
        if "://" not in pattern:
            raise InterfaceError(
                f"{cap}.pages: {pattern!r} is not a resource pattern — it needs a "
                "scheme, as the page's own `resource` does (e.g. stock://*)")
    return out


def _declared_resource(turn) -> str:
    """What the page attached to this turn's conversation says it is showing."""
    session = getattr(turn, "chat_session", None)
    state = getattr(session, "page_state", None) or {} if session is not None else {}
    resource = state.get("resource")
    return resource if isinstance(resource, str) else ""


def _page_capability(turn, iface: dict, classes: set[str]) -> str | None:
    """The capability whose `pages` match what this turn's page declared.

    Checked before falling back to `ask`, so a conversation held on a page the
    agent has declared a door for gets THAT door, and every other channel is
    unaffected. Names are tried in order for determinism; a capability the
    caller's class is not listed for is skipped rather than refusing outright,
    because `ask` may still admit them.

    This cannot widen anything on the host's word: the page supplies only the
    resource it is showing, and every tool in the profile was chosen by the
    agent's owner. A host that lies about its resource reaches the tools that
    owner already decided to expose to that page.
    """
    from fnmatch import fnmatchcase

    resource = _declared_resource(turn)
    if not resource:
        return None
    for name, cap in (iface.get("capabilities") or {}).items():
        patterns = cap.get("pages") or []
        if not patterns or not (classes & set(cap.get("callers") or [])):
            continue
        if any(fnmatchcase(resource, pattern) for pattern in patterns):
            return name
    return None


def _classes(value, where: str) -> list[str]:
    out = []
    for c in _strings(value, where.rpartition(".")[2] or where, where.rpartition(".")[0] or "interface"):
        c = c.lower()
        if not _CALLER.match(c):
            raise InterfaceError(
                f"{where}: {c!r} is not a caller class — member | contact | unknown, "
                "optionally @domain.tld, optionally :verified")
        out.append(c)
    return out


def caller_classes(turn, relationship: str) -> set[str]:
    """The classes this turn's asker falls in: the base, the base at their
    address's domain, and the `:verified` form of each when THIS message is."""
    from apps.harness.caller_context import _verified

    kind = turn.initiator_kind
    if kind == who.CONTACT:
        base = "contact"
        address = getattr(turn.initiator_contact, "email", "") or ""
    elif kind == who.USER and relationship == "member":
        base = "member"
        address = getattr(turn.initiator_user, "email", "") or ""
    else:
        # Anyone else — including a canopy user with no business in this
        # workspace — is no more than someone unidentified.
        base, address = "unknown", ""
    return _expand(base, address, _verified(turn))


def _expand(base: str, address: str, verified: bool) -> set[str]:
    classes = {base}
    domain = address.strip().lower().rpartition("@")[2] if "@" in (address or "") else ""
    if domain:
        classes.add(f"{base}@{domain}")
    if verified:
        classes |= {f"{c}:verified" for c in list(classes)}
    return classes


def full_rule(classes: set[str], iface: dict) -> str | None:
    """The `full:` entry that grants these classes the agent's whole profile."""
    for rule in iface.get("full") or []:
        if rule in classes:
            return rule
    return None


def published(iface: dict) -> bool:
    """Whether the agent has declared anything. An interface with neither a
    `full:` rule nor a capability says nothing, and is treated as none."""
    return bool(iface.get("capabilities") or iface.get("full"))


#: Who reaches an agent that has published NO interface: the people canopy
#: already knows by login and their workspace let in, and canopy itself.
_TRUSTED_WITHOUT_INTERFACE = frozenset({"owner", "admin", "system", "member"})


def capability_for(turn, agent, requested: str | None = None) -> str | None:
    """Which profile this turn runs in: FULL, a capability name, or None (refused).

    FULL for its owner, admins, and canopy's own turns. With NO published
    interface, workspace members get FULL too and everyone else — a contact, a
    stranger — is refused: default deny (2026-09-26). It used to be FULL for
    everyone, so an agent reachable from Slack or email with no interface ran
    a colleague-of-nobody's ask with its whole profile, and the enforcement
    layer only existed for the one agent that had opted in. A
    caller gets the capability they asked for — `requested`, e.g. a tool called
    over MCP — or `ask` by default (every free-form channel), if their class is
    listed for it; otherwise they are refused (`callers_default: none`).
    """
    from apps.harness.caller_context import ADMIN, OWNER, SYSTEM, relationship

    iface = getattr(agent, "interface", None) or {}
    rel = relationship(turn, agent)
    if not published(iface):
        return FULL if rel in _TRUSTED_WITHOUT_INTERFACE else None
    if rel in (OWNER, ADMIN, SYSTEM):
        return FULL
    classes = caller_classes(turn, rel)
    if full_rule(classes, iface):
        return FULL
    name = requested or _page_capability(turn, iface, classes) or ASK
    cap = iface["capabilities"].get(name)
    if cap and classes & set(cap.get("callers") or []):
        return name
    return None


def granted_by(turn, agent) -> str:
    """WHY this turn has the access it has, for the envelope: `owner`, `admin`,
    `system`, `full:<rule>`, `capability:<name>`, `no-interface` or `refused`."""
    from apps.harness.caller_context import ADMIN, OWNER, SYSTEM, relationship

    iface = getattr(agent, "interface", None) or {}
    rel = relationship(turn, agent)
    if not published(iface):
        return "no-interface" if rel in _TRUSTED_WITHOUT_INTERFACE else "refused"
    if rel in (OWNER, ADMIN, SYSTEM):
        return rel
    rule = full_rule(caller_classes(turn, rel), iface)
    if rule:
        return f"full:{rule}"
    return f"capability:{turn.capability}" if turn.capability else "refused"


def offered_to(user, agent) -> list[str]:
    """The capabilities `user` may invoke on `agent` directly — the MCP tool list.

    Everything, for the owner and admins (their turns run FULL anyway). For a
    workspace member, the capabilities listing `member` or `member:verified`:
    a request authenticated by a canopy session or token IS verified, so the
    two are the same for someone calling as themselves. Nothing for anyone
    who is not a member of the agent's workspace — this surface is for people
    canopy already knows by login; outsiders arrive by email or widget.
    """
    caps = ((getattr(agent, "interface", None) or {}).get("capabilities") or {})
    if not caps or not getattr(user, "is_authenticated", False):
        return []
    from apps.harness.caller_context import ADMIN, MEMBER, OWNER, relationship_for_user

    rel = relationship_for_user(user, agent)
    if rel in (OWNER, ADMIN):
        return sorted(caps)
    if rel != MEMBER:
        return []
    # Calling as themselves over an authenticated token IS verified.
    classes = _expand("member", getattr(user, "email", "") or "", True)
    if full_rule(classes, agent.interface or {}):
        return sorted(caps)
    return sorted(n for n, c in caps.items() if classes & set(c.get("callers") or []))


def profile(agent, capability: str) -> dict | None:
    """The capability's profile as the runner and the guard see it, or None for FULL.

    A capability that has since been unpublished yields an EMPTY profile —
    deny everything — never the full one: a turn that was restricted when it
    was queued must not become unrestricted because the file changed.
    """
    if capability == FULL:
        return None
    cap = ((getattr(agent, "interface", None) or {}).get("capabilities") or {}).get(capability)
    cap = cap or {"description": "", "callers": [], "entry": None,
                  "tools": [], "bash": [], "read_paths": [], "input": {}, "pages": []}
    return {"name": capability, **{k: cap.get(k) for k in
                                   ("description", "entry", "tools", "bash", "read_paths",
                                    "input")}}

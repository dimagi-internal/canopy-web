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

**Opt-in per agent.** An agent that has published no interface behaves exactly
as before: every turn runs in its full profile. Publishing one is what turns
the caller path on, so an agent is never half-restricted by accident.

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

#: What free-form channels (email, chat, Slack, the widget) invoke. Named
#: capabilities are for callers that ask for something specific.
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
                          "input"}
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
        }
    full = _classes(doc.get("full"), "full")
    return {"version": VERSION, "full": full, "capabilities": out, "callers_default": "none"}


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


def capability_for(turn, agent, requested: str | None = None) -> str | None:
    """Which profile this turn runs in: FULL, a capability name, or None (refused).

    FULL when the agent has published no interface (opt-in: nothing changes
    until it does), and for its owner, admins, and canopy's own turns. A
    caller gets the capability they asked for — `requested`, e.g. a tool called
    over MCP — or `ask` by default (every free-form channel), if their class is
    listed for it; otherwise they are refused (`callers_default: none`).
    """
    from apps.harness.caller_context import ADMIN, OWNER, SYSTEM, relationship

    iface = getattr(agent, "interface", None) or {}
    if not iface.get("capabilities") and not iface.get("full"):
        return FULL
    rel = relationship(turn, agent)
    if rel in (OWNER, ADMIN, SYSTEM):
        return FULL
    classes = caller_classes(turn, rel)
    if full_rule(classes, iface):
        return FULL
    name = requested or ASK
    cap = iface["capabilities"].get(name)
    if cap and classes & set(cap.get("callers") or []):
        return name
    return None


def granted_by(turn, agent) -> str:
    """WHY this turn has the access it has, for the envelope: `owner`, `admin`,
    `system`, `full:<rule>`, `capability:<name>`, `no-interface` or `refused`."""
    from apps.harness.caller_context import ADMIN, OWNER, SYSTEM, relationship

    iface = getattr(agent, "interface", None) or {}
    if not iface.get("capabilities") and not iface.get("full"):
        return "no-interface"
    rel = relationship(turn, agent)
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
                  "tools": [], "bash": [], "read_paths": [], "input": {}}
    return {"name": capability, **{k: cap.get(k) for k in
                                   ("description", "entry", "tools", "bash", "read_paths",
                                    "input")}}

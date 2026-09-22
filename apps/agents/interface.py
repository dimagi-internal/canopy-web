"""An agent's DECLARED INTERFACE: what it offers people who are not its admins.

Phase 4 of `docs/superpowers/specs/2026-09-18-who-is-asking-initiator-identity-
and-access-design.md` (§4). An agent has two relationships (§3): its owner and
admins reach its full working session; everyone else — a workspace member, an
emailer, a widget visitor — is a CALLER, and reaches only what the agent
declares here. The definition lives in the agent's repo (`config/interface.yaml`);
canopy holds the published copy and enforces it.

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
A `:verified` suffix additionally requires THIS message to be verified — for a
contact, DMARC-aligned mail or a signed assertion from a framed origin.
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
    extra = set(doc) - {"capabilities", "callers_default", "version"}
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
        callers = _strings(cap.get("callers"), "callers", name)
        for c in callers:
            base, _, suffix = c.partition(":")
            if base not in CALLER_CLASSES or suffix not in ("", "verified"):
                raise InterfaceError(
                    f"{name}.callers: {c!r} is not one of {sorted(CALLER_CLASSES)} "
                    "(optionally with ':verified')")
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
    return {"version": VERSION, "capabilities": out, "callers_default": "none"}


def caller_classes(turn, relationship: str) -> set[str]:
    """The classes this turn's asker falls in, including `:verified` forms."""
    from apps.harness.caller_context import _verified

    kind = turn.initiator_kind
    if kind == who.CONTACT:
        base = "contact"
    elif kind == who.USER and relationship == "member":
        base = "member"
    elif kind == who.USER:
        # A canopy user who is not even a member of the agent's workspace: no
        # relationship at all, so nothing more than someone unidentified.
        base = "unknown"
    else:
        base = "unknown"
    classes = {base}
    if _verified(turn):
        classes.add(f"{base}:verified")
    return classes


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
    if not iface.get("capabilities"):
        return FULL
    rel = relationship(turn, agent)
    if rel in (OWNER, ADMIN, SYSTEM):
        return FULL
    name = requested or ASK
    cap = iface["capabilities"].get(name)
    if cap and caller_classes(turn, rel) & set(cap.get("callers") or []):
        return name
    return None


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
    return sorted(n for n, c in caps.items()
                  if {"member", "member:verified"} & set(c.get("callers") or []))


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

"""What a conversation requires of the runner it runs on, and whether a runner
satisfies it (spec 2026-09-30-zdr-runners).

The requirement is written by the HOST (a signed arrival claim), copied onto the
session's server-owned metadata, and read here — by claiming, the unclaimable
report, placement, turn status and the host gateway, so they cannot disagree.
"""
from __future__ import annotations

from canopy_sdk.contract import RUNNER_FLAGS, parse_runner_requirements

METADATA_KEY = "runner_requirements"
#: Stands in for a value that is not a valid requirement list. No runner can
#: declare it, so a corrupted row is unclaimable rather than unrestricted.
UNSATISFIABLE = "__malformed__"
#: The flag a sensitive conversation requires of its runner, and what a person
#: declares of a box set up for sensitive data.
SENSITIVE_DATA = "sensitive_data"
_LABELS = {SENSITIVE_DATA: "sensitive-data"}
#: What each flag means, as canopy states it to a connected site (the site
#: agents API's `runner_flags`). One entry per `RUNNER_FLAGS` member (a test holds it).
MEANINGS = {
    SENSITIVE_DATA: ("a sensitive-data runner: its owner declared, and the box reports, that it "
                     "runs Claude only on a zero-data-retention (ZDR) API key, with a locked "
                     "harness on every turn, egress limited to an allowlist of hosts, nothing "
                     "kept on the box after a turn, and only agents that handle sensitive data"),
}

__all__ = ["MEANINGS", "METADATA_KEY", "RUNNER_FLAGS", "SENSITIVE_DATA", "UNSATISFIABLE", "describe",
           "lineage_requirements", "lost_lineage", "requirements_of",
           "requirements_of_grant", "requirements_of_session", "satisfies", "in_force",
           "runner_satisfies"]


def _parse_stored(raw) -> frozenset[str]:
    try:
        return frozenset(parse_runner_requirements(raw))
    except ValueError:
        return frozenset({UNSATISFIABLE})


def requirements_of_session(session) -> frozenset[str]:
    return _parse_stored((getattr(session, "metadata", None) or {}).get(METADATA_KEY))


def requirements_of_grant(grant) -> frozenset[str]:
    """The requirements a `HostGrant` was minted under (fail-closed like a session's)."""
    return _parse_stored(getattr(grant, "runner_requirements", None))


#: How far up a chain of parent turns requirements are inherited. A chain longer
#: than this is unclaimable rather than unrestricted.
MAX_ANCESTORS = 25


def requirements_of(turn) -> frozenset[str]:
    """What the runner a turn runs on must satisfy: the union of its own conversation
    (`chat_session`, or for an agent/project turn the `canopy_session` it ran in), the
    conversation it was started from (`parent_session`) and every ancestor turn's.

    Requirements only grow down a chain: a turn a sensitive conversation started (a dispatch,
    a re-ask, a transfer) carries the conversation's data with it, so it is held to the
    same runners even though it has no chat session of its own.

    A turn that has not finished, whose chain lost a parent it was created with (the
    parent deleted since, its link set to NULL), is unsatisfiable: what the lost
    parent required can no longer be read, so it is not guessed to be nothing
    (`lost_lineage`). A finished turn is read as it stands.
    """
    reqs: set[str] = set()
    seen: set = set()
    node = turn
    live = getattr(turn, "status", None) not in getattr(turn, "TERMINAL", ())
    for _ in range(MAX_ANCESTORS + 1):
        if node is None:
            return frozenset(reqs)
        key = getattr(node, "pk", None) or id(node)
        if key in seen:
            return frozenset(reqs)
        seen.add(key)
        if node.chat_session_id:
            reqs |= requirements_of_session(node.chat_session)
        # An agent, board or email turn's conversation is the one its runner recorded
        # (`canopy_session`): held to the same floor as a chat turn's.
        if getattr(node, "canopy_session_id", None):
            reqs |= requirements_of_session(node.canopy_session)
        if getattr(node, "parent_session_id", None):
            reqs |= requirements_of_session(node.parent_session)
        if live and lost_lineage(node):
            reqs.add(UNSATISFIABLE)
        node = node.parent_turn if getattr(node, "parent_turn_id", None) else None
    return frozenset(reqs | {UNSATISFIABLE})


def lost_lineage(node) -> bool:
    """Whether a Turn or Session was created with a parent turn or session that is
    gone now: its provenance says the parent resolved (`provenance.parent_fields`
    records `resolved`), and the link is NULL. A record from before `resolved`
    was written counts a named key that did not fail to resolve."""
    record = (getattr(node, "provenance", None) or {}).get("parent") or {}
    if not isinstance(record, dict) or not record:
        return False
    if "resolved" in record:
        had = set(str(record["resolved"]).split(","))
    else:
        failed = set(str(record.get("unresolved") or "").split(","))
        had = {k for k in ("turn", "session") if record.get(k) and k not in failed}
    return (("turn" in had and not getattr(node, "parent_turn_id", None))
            or ("session" in had and not getattr(node, "parent_session_id", None)))


def lineage_requirements(*, parent_turn=None, parent_session=None) -> frozenset[str]:
    """What something started from `parent_turn` / `parent_session` inherits: the
    union of both. A session started from a sensitive turn or conversation is
    stamped with this at birth (`provenance.inherit_requirements`), so it is
    sensitive before its first message rather than only through a chain."""
    reqs: set[str] = set()
    if parent_turn is not None:
        reqs |= requirements_of(parent_turn)
    if parent_session is not None:
        reqs |= requirements_of_session(parent_session)
    return frozenset(reqs)


def satisfies(flags: frozenset[str], reqs: frozenset[str]) -> bool:
    return reqs <= flags


#: The data mode a box must REPORT for a person's declaration of each flag to count
#: (spec 2026-10-22-sensitive-data-agents §2). canopy cannot see a box's keys;
#: what it can see is whether the box says it is running in the mode that makes the
#: declaration true, and a declaration the box contradicts is not in force.
FLAG_NEEDS_DATA_MODE = {SENSITIVE_DATA: SENSITIVE_DATA}
#: Where the box's heartbeat report lands (`services.heartbeat`).
DATA_MODE_KEY = "data_mode"
DATA_MODE_HASH_KEY = "data_mode_hash"


def in_force(runner) -> frozenset[str]:
    """The flags routing may rely on: declared by a person (`Runner.flags`), and,
    for a flag that names a data mode, matched by the box's own latest report."""
    reported = str((getattr(runner, "capabilities", None) or {}).get(DATA_MODE_KEY) or "")
    return frozenset(f for f in runner.flags
                     if FLAG_NEEDS_DATA_MODE.get(f) in (None, reported))


def is_sensitive_data_box(runner) -> bool:
    """A box set up for sensitive data: a person declared `sensitive_data` on it, OR
    it reports a data mode on its heartbeat. Either alone counts — unlike `in_force`,
    which needs both before routing sends sensitive work there. What it decides errs
    toward the strict side: the box holds only agents that handle sensitive data
    (`agents.services.runner_may_hold_agent`) and its egress stays locked (`egress`)."""
    caps = getattr(runner, "capabilities", None) or {}
    return SENSITIVE_DATA in runner.flags or bool(caps.get(DATA_MODE_KEY))


def runner_satisfies(runner, reqs: frozenset[str]) -> bool:
    """Whether `runner` may hold work that requires `reqs` — the one question every
    routing, placement and gateway check asks."""
    return satisfies(in_force(runner), reqs)


def describe(reqs) -> str:
    return ", ".join(_LABELS.get(r, r) for r in sorted(reqs))

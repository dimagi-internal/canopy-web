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
_LABELS = {"zdr": "ZDR"}
#: What each flag means, as canopy states it to a connected site (the site
#: agents API's `runner_flags`). One entry per `RUNNER_FLAGS` member (a test holds it).
MEANINGS = {
    "zdr": ("zero data retention: the runner's owner declared that its Claude traffic uses a "
            "zero-data-retention key, and the box reports running in that mode"),
}

__all__ = ["MEANINGS", "METADATA_KEY", "RUNNER_FLAGS", "UNSATISFIABLE", "describe", "requirements_of",
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
    """What the runner a turn runs on must satisfy: the union of its own conversation,
    the conversation it was started from (`parent_session`) and every ancestor turn's.

    Requirements only grow down a chain: a turn a ZDR conversation started (a dispatch,
    a re-ask, a transfer) carries the conversation's data with it, so it is held to the
    same runners even though it has no chat session of its own.
    """
    reqs: set[str] = set()
    seen: set = set()
    node = turn
    for _ in range(MAX_ANCESTORS + 1):
        if node is None:
            return frozenset(reqs)
        key = getattr(node, "pk", None) or id(node)
        if key in seen:
            return frozenset(reqs)
        seen.add(key)
        if node.chat_session_id:
            reqs |= requirements_of_session(node.chat_session)
        if getattr(node, "parent_session_id", None):
            reqs |= requirements_of_session(node.parent_session)
        node = node.parent_turn if getattr(node, "parent_turn_id", None) else None
    return frozenset(reqs | {UNSATISFIABLE})


def satisfies(flags: frozenset[str], reqs: frozenset[str]) -> bool:
    return reqs <= flags


#: The data mode a box must REPORT for a person's declaration of each flag to count
#: (spec 2026-10-22-phi-agents-on-zdr-runners §2). canopy cannot see a box's keys;
#: what it can see is whether the box says it is running in the mode that makes the
#: declaration true, and a declaration the box contradicts is not in force.
FLAG_NEEDS_DATA_MODE = {"zdr": "zdr"}
#: Where the box's heartbeat report lands (`services.heartbeat`).
DATA_MODE_KEY = "data_mode"
DATA_MODE_HASH_KEY = "data_mode_hash"


def in_force(runner) -> frozenset[str]:
    """The flags routing may rely on: declared by a person (`Runner.flags`), and,
    for a flag that names a data mode, matched by the box's own latest report."""
    reported = str((getattr(runner, "capabilities", None) or {}).get(DATA_MODE_KEY) or "")
    return frozenset(f for f in runner.flags
                     if FLAG_NEEDS_DATA_MODE.get(f) in (None, reported))


def is_zdr_box(runner) -> bool:
    """A box set up for PHI work: a person declared `zdr` on it, OR it reports ZDR
    mode on its heartbeat. Either alone counts — unlike `in_force`, which needs both
    before routing sends ZDR work there. What it decides errs toward the strict side:
    the box holds only `data: phi` agents (`agents.services.runner_may_hold_agent`)
    and its egress stays locked (`egress`)."""
    caps = getattr(runner, "capabilities", None) or {}
    return "zdr" in runner.flags or bool(caps.get(DATA_MODE_KEY))


def runner_satisfies(runner, reqs: frozenset[str]) -> bool:
    """Whether `runner` may hold work that requires `reqs` — the one question every
    routing, placement and gateway check asks."""
    return satisfies(in_force(runner), reqs)


def describe(reqs) -> str:
    return ", ".join(_LABELS.get(r, r) for r in sorted(reqs))

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

__all__ = ["METADATA_KEY", "RUNNER_FLAGS", "UNSATISFIABLE", "describe", "requirements_of",
           "requirements_of_grant", "requirements_of_session", "satisfies"]


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


def describe(reqs) -> str:
    return ", ".join(_LABELS.get(r, r) for r in sorted(reqs))

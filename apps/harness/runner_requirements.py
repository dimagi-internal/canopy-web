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


def requirements_of(turn) -> frozenset[str]:
    if not turn.chat_session_id:
        return frozenset()
    return requirements_of_session(turn.chat_session)


def satisfies(flags: frozenset[str], reqs: frozenset[str]) -> bool:
    return reqs <= flags


def describe(reqs) -> str:
    return ", ".join(_LABELS.get(r, r) for r in sorted(reqs))

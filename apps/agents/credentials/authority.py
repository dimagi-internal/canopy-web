"""Whose authority a credential carries where it is read: the one table.

Two things decide what a read is handed — where the session runs (the placement)
and what kind of credential it is — and nothing else does. Code asks `stance`; it
does not branch on a principal's name to decide a credential's fate.

Placements:
- `runner`: a runner that claims the agent's turns, proved by its device key. A cloud box
  or a laptop running the runner app alike: the machine does not matter, who drives does.
- `direct`: a person's own Claude Code session (stored as a direct turn), under their
  license, on whatever machine they chose, a runner's laptop included.
- `manage`: an agent admin reading by hand with their own token (the browser never
  reads plaintext, so this is a CLI). Not a way to run the agent: it hands out what
  the agent itself holds, and never a lent account.

Kinds:
- `agent_only`: the agent's canopy login. It lets the holder BE the agent, past the cap
  on what a person who started a turn may do.
- `vault`: values in the agent's own vault. The agent's in every placement; who may
  read them at all is `access`'s and the vault's decision, not this module's.
- `delegated`: a person's account lent to the agent (their GitHub). The agent's on a
  box; in a direct session it is the person's own, so the lent copy never travels.

Spec: docs/superpowers/specs/2026-10-29-two-axes-three-credentials-design.md
"""
from __future__ import annotations

from dataclasses import dataclass

RUNNER = "runner"
DIRECT = "direct"
MANAGE = "manage"

AGENT_ONLY = "agent_only"
VAULT = "vault"
DELEGATED = "delegated"


@dataclass(frozen=True)
class Stance:
    hands_out: bool
    #: What a reader who is not handed it is told.
    why: str = ""


_USE_YOUR_OWN = "in your own session this is your own account: use it, not a lent copy"
_NEVER_TO_A_PERSON = ("it would let you act as the agent itself, past what the caller list gives you; "
                      "in your own session you act as yourself, or ask to act as you where canopy mediates")

_TABLE: dict[tuple[str, str], Stance] = {
    (RUNNER, AGENT_ONLY): Stance(True),
    (RUNNER, VAULT): Stance(True),
    (RUNNER, DELEGATED): Stance(True),
    (DIRECT, AGENT_ONLY): Stance(False, _NEVER_TO_A_PERSON),
    (DIRECT, VAULT): Stance(True),
    (DIRECT, DELEGATED): Stance(False, _USE_YOUR_OWN),
    (MANAGE, AGENT_ONLY): Stance(True),
    (MANAGE, VAULT): Stance(True),
    (MANAGE, DELEGATED): Stance(False, _USE_YOUR_OWN),
}


def stance(placement: str, kind: str) -> Stance:
    return _TABLE[(placement, kind)]


def hands_out(placement: str, kind: str) -> bool:
    return _TABLE[(placement, kind)].hands_out


def placement_of(via: str) -> str:
    """The placement a `credential_access` principal reads from."""
    return {"runner": RUNNER, "license": DIRECT, "admin": MANAGE}[via]

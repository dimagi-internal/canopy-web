"""A turn that keeps nothing on its box for later turns: a learning run's (`apps/brain`).

A learning run's turn reads other people's sessions (through `brain_read`, recorded). What it read must not leak
into the operator agent's memory or into its unrelated sessions: its conversation is its own (no creator, no
participant, readable by no other turn, `canopy_sessions.access`), and on the box Claude Code's auto memory is off
for it, so nothing it read is written to the memory the agent's later turns load. canopy-web marks such a turn
`isolated` in its claim (`ClaimedTurnOut.isolated`), and only a box whose code reports that it honours the mark
(`isolated` on its heartbeat, `runner/ec2/cloud_runner.py` `ISOLATED_TURN_ENV`) is handed one; any other waits.
A laptop runner drives its turns in the person's own Claude app and does not report it."""
from __future__ import annotations

#: The level a box must report (`Runner.capabilities["isolated"]`) to take an isolated turn.
ISOLATED_VERSION = 1


def is_isolated(turn) -> bool:
    """Whether `turn` belongs to a learning run's conversation."""
    from apps.canopy_sessions import access  # noqa: PLC0415 — canopy_sessions reads the harness's models

    return access.is_brain_conversation(access.turn_conversation(turn))


def runner_isolates(runner) -> bool:
    return runner is not None and int((runner.capabilities or {}).get("isolated") or 0) >= ISOLATED_VERSION


def refusal(runner, turn) -> str | None:
    """Why `runner` may not take `turn` for isolation, or None."""
    if not is_isolated(turn) or runner_isolates(runner):
        return None
    return ("a learning run's turn keeps nothing on its box for later turns, and this runner's code does not say "
            "it can (a cloud runner on current code does)")

"""Did anything happen on this box this tick? The idle back-off's one input
beyond the claim result (#647).

A tick that claimed nothing is not necessarily quiet: a person may be working in
an emdash session, or an agent may still be writing after its turn handed off. A
viewer watching either needs the normal cadence. The steps that observe that
work (the session report's change-check, the transcript streams) call `note()`
when they see it, and the loop `take()`s the flag once per tick.

A plain module flag is enough. Both writers run on the poll thread, and a
missed note costs one long wait at worst, never correctness: the WS doorbells
still cut any wait short.
"""
from __future__ import annotations

_seen = False


def note() -> None:
    """Something happened this tick: keep the normal cadence."""
    global _seen
    _seen = True


def take() -> bool:
    """Whether anything was noted since the last call, clearing it."""
    global _seen
    seen, _seen = _seen, False
    return seen

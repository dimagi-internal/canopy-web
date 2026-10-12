"""A caller's turn tells its owner what it was asked, in `result_note`.

A confined turn (a caller who is not the agent's owner or an admin) can act on
nothing, so its procedure ends with one line, `For <Agent>'s owner: <ask>`, and
promises the caller the request "is noted in this conversation's record on
canopy-web where the owner reviews it" (canopy-web#1400 item 5). The record a
caller's turn leaves is its transcript, and nobody reads the transcript of a
conversation they were not in. The runner lifts the line out (the laptop from
the session's `Stop` hook, the cloud box from the finished reply) and sends it
as an `owner_ask` turn event; this appends it to `result_note`, the one-line
outcome every view of the turn already shows its owner (canopy-web#1416).

The line is re-parsed here rather than trusted, so an event can only ever add a
single `For …'s owner:` line, and only to a confined turn.
"""
from __future__ import annotations

from canopy_transcript.owner_ask import find

from .models import Turn

#: The ledger event kind a runner sends (`TurnEvent.kind` holds 20 characters).
EVENT = "owner_ask"


def record(turn: Turn, text) -> bool:
    """Append the owner line found in `text` to a confined turn's result note.
    True if the note changed. Idempotent: the same line twice is one line."""
    if not turn.capability:
        return False
    line = find(text)
    if not line:
        return False
    note = turn.result_note or ""
    if line in note:
        return False
    turn.result_note = f"{note.rstrip()}\n{line}".strip()
    Turn.objects.filter(pk=turn.pk).update(result_note=turn.result_note)
    return True

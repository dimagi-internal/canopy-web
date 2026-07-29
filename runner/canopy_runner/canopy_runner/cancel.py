"""Turn ids the user asked to stop.

A leaf module on purpose: the set is PRODUCED by the wake listener (main) and
CONSUMED by the chat pump and the executor, so parking it in either would make
the other import its caller. Module-scoped for the process lifetime — it is a
transient "stop now" signal, not a durable per-turn record, and every consumer
discards an id once its turn ends (a leaked id would wrongly cancel whatever
future turn reused it).
"""
from __future__ import annotations

CANCELLED_TURNS: set[str] = set()

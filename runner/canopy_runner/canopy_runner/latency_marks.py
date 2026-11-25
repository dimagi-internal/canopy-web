"""The runner's half of a turn's reply-latency trace (canopy-web `apps/harness/latency.py`).

The runner is the only one who sees some moments first-hand: when the session was
ready, when the words were typed, when the transcript showed them, when the agent's
first words and its hand-back appeared. Each is noted here as it happens (`mark`)
and travels to canopy-web on the turn's finish (`take`), which stores it on the turn.

Times are this machine's wall clock, UTC. canopy-web's own marks (queued, claimed,
finished) are the server's clock; both are NTP-synced, so a span that crosses the two
can be off by the skew between them, typically well under a second.

In memory only: a runner restart mid-turn loses that turn's marks, and the trace
falls back to canopy's own timestamps. Bounded (`MAX_TRACES`), since a turn that
fails before finishing through the pump never takes its marks.
"""
from __future__ import annotations

import datetime as dt
import threading
import time

MAX_TRACES = 200

_lock = threading.Lock()
_traces: dict[str, dict] = {}
#: Monotonic times beside the wall-clock ones, for the per-step log line.
_mono: dict[str, dict[str, float]] = {}


def _now_iso() -> str:
    return dt.datetime.now(dt.UTC).isoformat()


def mark(turn_id: str, name: str) -> None:
    """Note that `name` happened now. The first note wins."""
    if not turn_id:
        return
    with _lock:
        trace = _traces.setdefault(str(turn_id), {})
        if name not in trace:
            trace[name] = _now_iso()
            _mono.setdefault(str(turn_id), {})[name] = time.monotonic()
        while len(_traces) > MAX_TRACES:
            oldest = next(iter(_traces))
            _traces.pop(oldest, None)
            _mono.pop(oldest, None)


def fact(turn_id: str, **facts) -> None:
    """Note a fact about the turn (`warm`)."""
    if not turn_id:
        return
    with _lock:
        _traces.setdefault(str(turn_id), {}).update(facts)


def has(turn_id: str, name: str) -> bool:
    with _lock:
        return name in _traces.get(str(turn_id), {})


def steps(turn_id: str, start: float) -> str:
    """`name=+1.23s` for every mark so far, relative to `start` (monotonic)."""
    with _lock:
        mono = dict(_mono.get(str(turn_id), {}))
    return " ".join(f"{k}=+{v - start:.2f}s" for k, v in sorted(mono.items(), key=lambda kv: kv[1]))


def take(turn_id: str) -> dict | None:
    """The turn's marks, removed: what its finish reports."""
    with _lock:
        _mono.pop(str(turn_id), None)
        return _traces.pop(str(turn_id), None) or None

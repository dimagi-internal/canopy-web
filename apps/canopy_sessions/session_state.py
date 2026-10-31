"""What a session is doing right now — the one answer the session list, the
supervisor feed, Home, a stop-everything and the chat socket's snapshot read.

A session is RUNNING when either

* a turn that drives it (`staleness.driving_turns`) is claimed or running on a
  runner that is still there — the turn-side fact `harness.turn_status` reads as
  WORKING, true on every runner, cloud included; or
* its engine reports it working (`transcript_read.is_session_running`) — the only
  evidence for work no turn owns: a person typing straight into emdash, or an
  agent that kept working after its fire-and-continue turn finished. Only a runner
  that reports the engine's flag (the laptop) can answer this one.

Its ACTIVITY is `blocked` while a dialog is up, else `working` while running,
else `idle` — the same three values the runner's live `session.activity` frames
carry, so a snapshot can replace whatever a client last heard.
"""
from __future__ import annotations

from django.db.models import Exists, Subquery

from apps.harness.models import Runner, Turn

from . import staleness, transcript_read
from .serializers import pending_menu

WORKING = "working"
BLOCKED = "blocked"
IDLE = "idle"

#: A turn in these states is producing in its session (NEEDS_HUMAN is blocked on a
#: person, which the dialog answers).
IN_FLIGHT = (Turn.CLAIMED, Turn.RUNNING)


def _working_turns():
    return (staleness.driving_turns().filter(status__in=IN_FLIGHT, rides_turn__isnull=True)
            .order_by("-claimed_at"))


def with_state(sessions):
    """`sessions` annotated for `is_running` and the feed: `_working_runner_id` (the
    runner holding the newest in-flight turn that drives each, or None) and
    `_turn_open` (a turn that drives it is queued or in flight)."""
    return sessions.annotate(
        _working_runner_id=Subquery(_working_turns().values("claimed_by_id")[:1]),
        _turn_open=Exists(staleness.driving_turns().filter(status__in=Turn.NON_TERMINAL)),
    )


_UNSET = object()


def _working_runner_id(session):
    if hasattr(session, "_working_runner_id"):
        return session._working_runner_id
    return (staleness.driving_turns_of(session).filter(status__in=IN_FLIGHT, rides_turn__isnull=True)
            .order_by("-claimed_at").values_list("claimed_by_id", flat=True).first())


def turn_in_flight(session, binding=_UNSET) -> bool:
    """A turn that drives `session` is claimed or running on a runner that is still
    reachable. A turn on a box that stopped heartbeating is PAUSED, not working
    (`turn_status.runner_gone`)."""
    if binding is _UNSET:
        binding = getattr(session, "runner_binding", None)
    rid = _working_runner_id(session)
    if rid is None:
        return False
    if binding is not None and binding.runner_id == rid:
        runner = binding.runner
    else:
        runner = Runner.objects.filter(pk=rid).first()
    return runner is not None and runner.is_reachable


def is_running(session, binding=_UNSET) -> bool:
    """Is this session working right now (the module docstring)."""
    if binding is _UNSET:
        binding = getattr(session, "runner_binding", None)
    return turn_in_flight(session, binding) or transcript_read.is_session_running(binding)


def turn_open(session) -> bool:
    """A turn that drives `session` is queued or in flight."""
    if hasattr(session, "_turn_open"):
        return bool(session._turn_open)
    return staleness.driving_turns_of(session).filter(status__in=Turn.NON_TERMINAL).exists()


def activity(session, binding=_UNSET) -> str:
    """`blocked` | `working` | `idle` (the module docstring)."""
    if binding is _UNSET:
        binding = getattr(session, "runner_binding", None)
    if pending_menu(session) is not None:
        return BLOCKED
    return WORKING if is_running(session, binding) else IDLE

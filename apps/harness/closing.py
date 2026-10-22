"""The one way a turn reaches a terminal status: `close_turn`.

Every terminal transition — a runner's finish, a lease that ran out, a queued
turn stopped, a rider ending with the turn it rode, an admission rule refusing
a queued turn, a stranded turn Slack moved off a dead box — is a conditional
UPDATE plus the same side effects: the turn's agent logins revoked, a status
line on its ledger, one `turn.finished` analytics fact, its riders ended, the
chat hooks, a schedule nag discharged, a drill resolved, the debugger told. A
copy of that list per path is how stops, riders, refusals and moved turns came
to be missing from analytics; so the list lives here once, and a path that
deliberately skips a hook says so with an explicit argument.

No transaction of its own: it runs inside the caller's, exactly as the inline
copies did. Hooks that send anything outward (push, admission notices) defer
to `on_commit` themselves.
"""
from __future__ import annotations

from collections.abc import Iterable

from django.utils import timezone

from .ledger import append_events
from .models import RunnerDrill, Turn


def close_turn(
    turn: Turn,
    status: str,
    note: str | None = None,
    *,
    from_states: Iterable[str],
    where: dict | None = None,
    now=None,
    ledger: bool = True,
    event: dict | None = None,
    riders: bool = True,
    chat_hooks: bool = True,
    drill_summary: str | None = None,
    auto_debug: bool = True,
) -> bool:
    """Move `turn` to terminal `status` if it is still in `from_states` (and
    matches `where`), then fire every hook a terminal transition owes.

    Returns True when this call made the transition; False when the guard
    missed (someone else moved the turn first), in which case nothing is
    written. `turn` is refreshed from the database either way.

    `note` is written to `result_note` when given; None leaves it as it was.
    `ledger=False` skips the status line, for a caller that writes its own
    (admission's `_fire`, shared with refusals that never pass through here);
    `event` adds keys to the status line. `drill_summary`, when given, fails
    the turn's RunnerDrill if it is still pending (a no-op for a non-drill).
    """
    if status not in Turn.TERMINAL:
        raise ValueError(f"close_turn needs a terminal status, got {status!r}")
    now = now or timezone.now()
    fields = {"status": status, "finished_at": now}
    if note is not None:
        fields["result_note"] = note
    updated = Turn.objects.filter(pk=turn.pk, status__in=list(from_states), **(where or {})) \
        .update(**fields)
    turn.refresh_from_db()
    if not updated:
        return False

    # Whatever runs this turn has stopped: its agent logins die now.
    from .turn_agent_tokens import revoke_for_turn

    revoke_for_turn(turn.pk)
    if ledger:
        payload = {"status": status}
        if note is not None:
            payload["result_note"] = note
        append_events(turn, [{"kind": "status", "payload": {**payload, **(event or {})}}])

    from apps.events import activity

    activity.for_turn(activity.TURN_FINISHED, turn, at=now)

    if riders:
        close_riders(turn, now=now)

    if chat_hooks and turn.chat_session_id:
        from .services import end_turns_ask, logger

        # "Your chat is done" — arms the quiet-timer (or pushes now, if the chat
        # asked for every completion); DONE/FAILED only, so a stop or a lost
        # turn arms nothing. Never allowed to fail the close.
        try:
            from apps.push import services as push_services

            push_services.on_session_turn_finished(turn)
        except Exception:  # noqa: BLE001
            logger.exception("push: could not arm the finish push for turn %s", turn.pk)
        # The box that held this turn's ACP ask open has let go of it (or is
        # gone, and nothing else would ever retract the dialog it left).
        end_turns_ask(turn)

    # A finished scheduled occurrence discharges any open nag for its schedule —
    # you no longer owe attention to a slot that has since completed.
    if status == Turn.DONE:
        sid = (turn.origin_ref or {}).get("schedule_id")
        if sid:
            from . import schedule_turns  # its re-export is the seam tests patch

            schedule_turns.resolve_schedule_nags(sid)

    # A drill is identified by its RunnerDrill FK, not its origin (drills are
    # ordinary `api` turns that name a runner); the filter no-ops otherwise.
    # Scoped to OUTCOME_PENDING so a drill its agent already reported on is
    # never clobbered.
    if drill_summary is not None:
        RunnerDrill.objects.filter(turn=turn, outcome=RunnerDrill.OUTCOME_PENDING).update(
            outcome=RunnerDrill.OUTCOME_FAIL, summary=drill_summary, finished_at=now)

    # A turn that really ENDED failed starts a debugger turn (apps/harness/
    # auto_debug.py decides: FAILED always, LOST only when CANOPY_AUTO_DEBUG_LOST
    # is on; never a drill, a human's stop, or a debug turn of its own). Never
    # raises: a debug turn is never worth a failed close.
    if auto_debug and status in (Turn.FAILED, Turn.LOST):
        from . import auto_debug as debugger

        debugger.on_turn_failed(turn)
    return True


def close_riders(turn: Turn, status: str | None = None, *, now=None) -> None:
    """The follow-ups delivered into `turn` end when it does, the same way
    (`status`, default the turn's own — finish_turn's sessionless requeue passes
    FAILED while the turn itself goes back to the queue).

    They were typed into its session, so they have no life of their own: the
    agent answered them in this turn's reply, or the turn's failure/stop took
    them with it. Closed here rather than through `finish_turn`, whose
    sessionless requeue would re-type a message that was already delivered.
    No chat hooks (the turn they rode already armed the push and retracted the
    ask) and no debugger (its failure is the turn's, already reported)."""
    from .routing import EXECUTING

    status = status or turn.status
    note = f"delivered into the running turn {str(turn.pk)[:8]} — {status}"
    for rider in Turn.objects.filter(rides_turn=turn, status__in=EXECUTING):
        close_turn(rider, status, note, from_states=EXECUTING, now=now,
                   event={"rode": str(turn.pk)}, riders=False, chat_hooks=False,
                   auto_debug=False)

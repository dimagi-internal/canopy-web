"""Controlling a live session: answering an agent's dialog, interrupting,
stopping and cancelling its turns, archiving and closing it.

Every verb a runner must carry out is a durable row on the session's
RunnerBinding (`pending_answer`, `pending_interrupt`, `close_requested`) that the
runner drains on its poll tick; the control frame published beside it only
rings the doorbell. Turn-shaped work is reached through its turns
(`cancel_session_turns`), which the runner learns of from its heartbeat.
"""
from __future__ import annotations

import datetime as dt
import time
import uuid

from django.db import transaction
from django.db.models import Q

from apps.harness import services as harness_services
from apps.harness.models import Turn
from apps.harness.routing import turn_workspace_q

from . import lifecycle, session_state, staleness
from .models import RunnerBinding, Session

#: How long a recorded stop stays on offer to its runner. A stop is about the work
#: running when it was asked; one that has waited longer than this would land on
#: whatever the session is doing since.
INTERRUPT_TTL = dt.timedelta(minutes=2)


def answer_menu(*, session: Session, option: int | None,
                selections: list[list[int]] | None = None,
                texts: list[str | None] | None = None) -> str:
    """Answer the dialog an agent is blocked on, from the web.

    `selections` is the whole answer: one list of chosen option numbers per
    declared question, in declaration order. It is what a multi-select or a
    multi-question ask needs, because there a number key toggles a checkbox and
    the dialog waits on an explicit Submit — a single `option` cannot express
    "Red and Blue", and cannot reach the tab holding the second question at all.

    `option` is still sent alongside it, set to the first pick, and is the ONLY
    field a runner older than this understands. That is deliberate: such a runner
    keeps doing exactly what it does today rather than seeing an empty option and
    pressing Escape, which would cancel the dialog outright.

    Returns "sent" | "unavailable" | "unbound", mirroring `request_backfill`'s
    refusal shape rather than raising: a menu can go stale between the phone
    rendering it and a thumb reaching it, and that is ordinary, not an error.

    `option=None` means refuse, which the runner sends as Escape. Escape is the
    one answer that is safe when the dialog is not what we think it is — a NUMBER
    typed at a session that is no longer showing a menu lands in its prompt.

    The keystroke itself is the runner's job: the server knows nothing about
    terminals, and emdash (not canopy) owns the session.
    """
    binding = getattr(session, "runner_binding", None)
    if binding is None or binding.runner_id is None or not binding.session_key:
        return "unbound"
    # Reachable, not available: pause stops STARTING work, never finishing it,
    # and a blocked agent is unfinished work already running. The answer rides
    # the wake-listener thread, which the pause gate never touches — a PAUSED
    # runner (fresh heartbeat by construction) presses the key just fine, and it
    # is the runner whose session report delivered this very menu.
    if not binding.runner.is_reachable:
        return "unavailable"
    # Record BEFORE publishing. The frame is the doorbell; this is the record the
    # runner drains on its poll tick. Publishing alone is how an answer gets lost
    # in silence when the control channel is down — see RunnerBinding.pending_answer.
    answer_id = str(uuid.uuid4())
    binding.pending_answer = {"id": answer_id, "option": option,
                              "selections": selections, "texts": texts,
                              "at": time.time()}
    binding.save(update_fields=["pending_answer"])

    from apps.realtime import groups
    groups.publish(groups.runner_group(binding.runner_id), {
        "type": "runner.menu_answer",
        "session_id": str(session.id),
        "session_key": binding.session_key,
        # emdash task names are unique per PROJECT only; without this the runner
        # could press the key in a same-named session of another agent.
        "project": session.emdash_project,
        "option": option,
        "selections": selections,
        "texts": texts,
        "answer_id": answer_id,
    })
    return "sent"


def interrupt_session(session: Session) -> str:
    """Stop whatever this session's agent is doing, by interrupting its session on
    the runner — the stop for work no Turn owns (an agent, board or scheduled turn
    goes terminal the moment its prompt is delivered, and the agent works on).
    Addressed like `answer_menu`: the binding's `session_key`, the runner owning
    the keystroke.

    Recorded as `RunnerBinding.pending_interrupt` before the frame is published,
    so a frame lost on a down control channel costs the runner a poll tick, not
    the stop (`GET /runners/{id}/session-interrupts`).

    Returns "sent" | "unavailable" | "unbound", mirroring `answer_menu`'s refusal
    shape rather than raising — a session can go idle between a thumb reaching the
    button and the frame landing, and that is ordinary, not an error.
    """
    binding = getattr(session, "runner_binding", None)
    if binding is None or binding.runner_id is None or not binding.session_key:
        return "unbound"
    # Reachable, not available: pause stops STARTING work, never finishing it, and
    # an agent mid-turn is work already running.
    if not binding.runner.is_reachable:
        return "unavailable"
    interrupt_id = str(uuid.uuid4())
    binding.pending_interrupt = {"id": interrupt_id, "at": time.time()}
    binding.save(update_fields=["pending_interrupt"])

    from apps.realtime import groups
    groups.publish(groups.runner_group(binding.runner_id), {
        "type": "runner.session_interrupt",
        "session_id": str(session.id),
        "session_key": binding.session_key,
        "interrupt_id": interrupt_id,
    })
    return "sent"


def interrupt_live(pending: dict | None, now: float | None = None) -> bool:
    """Is this recorded stop (`RunnerBinding.pending_interrupt`) still on offer?"""
    if not isinstance(pending, dict) or not pending.get("id"):
        return False
    at = pending.get("at")
    if not isinstance(at, (int, float)):
        return False
    return ((now if now is not None else time.time()) - at) <= INTERRUPT_TTL.total_seconds()


def person_name(user) -> str:
    """A user's name for "Stopped by <name>", or "" for no one / an anonymous caller."""
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    return (user.get_full_name() or user.email or "").strip()


def cancel_session_turns(session: Session, *, by: str = "") -> bool:
    """Cancel every non-terminal turn on a session. Returns whether anything moved.

    ALL of them, not just the newest: a mid-reply send queues a second turn behind
    the running one, so the running one gets cancel_requested and the queued one is
    finished CANCELLED. (Not `any(cancel_turn(t) ...)`, which would stop at the
    first.)

    "On a session" covers both shapes of turn: a CHAT turn names its session
    (`chat_session`); an AGENT or PROJECT turn names none and is tied to it by the
    key its runner stamped on it (`stamp_turn_session`), the binding's
    `session_key` — matched on the binding's runner and the session's own target
    too, because a laptop's key is an emdash task name, unique per project only.
    On a cloud runner such a turn stays RUNNING for the whole of the work, so this
    is how a stop reaches it.
    """
    from apps.harness import services as harness_services
    from apps.harness.models import Turn

    owned = Q(chat_session=session)
    binding = getattr(session, "runner_binding", None)
    if binding is not None and binding.runner_id is not None and binding.session_key:
        bound = Q(session_key=binding.session_key, claimed_by_id=binding.runner_id,
                  chat_session__isnull=True)
        if session.agent_id:
            bound &= Q(agent_id=session.agent_id)
        elif session.project:
            bound &= Q(agent__isnull=True, project=session.project)
        else:
            bound = None
        if bound is not None:
            owned |= bound
    cancelled = False
    for turn in Turn.objects.filter(owned, status__in=list(Turn.NON_TERMINAL)):
        if harness_services.cancel_turn(turn, by=by) is not None:
            cancelled = True
    return cancelled


def stop_session(session: Session, *, by: str = "") -> str:
    """Stop this session, by whichever route owns the running work. Returns WHICH
    route fired: "turns" | "session" | "".

    TURNS FIRST (`cancel_session_turns`): a chat reply, and any turn a cloud runner
    is executing, is owned by a live Turn; cancelling it records the intent,
    reaches a queued turn behind it, and the runner finishes it CANCELLED.

    THEN THE SESSION (`interrupt_session`), when no turn moved: the work is not
    turn-shaped (an agent, board, scheduled or project turn on a laptop, terminal
    once its prompt is delivered). Never both: a chat turn's cancel already
    interrupts the same session, and a second interrupt could land after the agent
    moved on.

    The one stop: the session socket, REST/MCP (`POST /canopy-sessions/{id}/stop`),
    a contact's stop and Slack's all call this.
    """
    if cancel_session_turns(session, by=by):
        return "turns"
    return "session" if interrupt_session(session) == "sent" else ""


def stop_everything(*, by: str = "", workspaces: set[str] | None = None) -> dict[str, int]:
    """Stop ALL work running anywhere in the fleet — or, given `workspaces`, in
    those workspaces only (a workspace stop passes its tree). Returns
    `{"sessions": n, "turns": n}`: how many sessions a stop reached and how many
    more executing turns were told to cancel.

    Two passes, because work is reachable two ways (see `stop_session`):

    * every bound session that is running right now (`session_state.is_running`)
      gets THE one stop, `stop_session` — turns first, else the session;
    * then every turn still executing that no stop has reached yet (no
      `cancel_requested` in its ledger) gets `cancel_turn` — a cloud turn whose
      session never bound, a turn on a runner that stopped reporting.

    Queued turns are left alone: under the hold they are the list of what tried to
    start, and the release is what runs them. A rider is reached through the turn it
    rides. Nothing here closes or archives a session.
    """
    sessions = 0
    bindings = (RunnerBinding.objects.filter(runner__isnull=False)
                .exclude(session_key="").select_related("runner", "session"))
    if workspaces is not None:
        bindings = bindings.filter(session__workspace_id__in=workspaces)
    for binding in bindings:
        if session_state.is_running(binding.session, binding) and stop_session(binding.session, by=by):
            sessions += 1
    turns = 0
    executing = (Turn.objects.filter(status__in=[Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN],
                                     rides_turn__isnull=True)
                 .exclude(events__kind="cancel_requested"))
    if workspaces is not None:
        executing = executing.filter(turn_workspace_q(workspaces))
    for turn in executing:
        if harness_services.cancel_turn(turn, by=by) is not None:
            turns += 1
    return {"sessions": sessions, "turns": turns}


def is_runner_reported(binding) -> bool:
    """Is a runner CURRENTLY reporting an emdash task for this session?

    The one question `close_session` branches on, observed rather than inferred:
    `reported_at`, which only the wholesale report loop writes (`record_session`,
    called by both runners, stamps `live_seen_at` and `session_key` instead), read
    against the same `stale_cutoff()` the session list uses.
    """
    if binding is None or binding.runner_id is None or not binding.session_key:
        return False
    if binding.reported_at is None:
        return False
    return binding.reported_at >= staleness.stale_cutoff()


def close_session(*, session: Session) -> str:
    """End a session for good. Returns
    "closing" | "closed" | "unavailable" | "already_closed".

    Two branches on one question — see `is_runner_reported`.

    REPORTED (a laptop's emdash task): cancel the turns, record and relay a close,
    and write nothing to the session. The emdash task is the truth for a local
    session: the runner deletes it, and its absence from the next report (or its
    name in the report's `archived` list) retires the row.

    UNREPORTED (a cloud session, a web chat that never bound): cancel the turns —
    which stops a running cloud turn and keeps a queued one from waking it — then
    archive. It sticks, because nothing will ever report it back.

    A refusal is a returned reason, never a raise: a session can go stale between
    the phone rendering the list and a thumb reaching it. `unavailable` does NOT
    queue — a close that sits until a box returns is indistinguishable from one
    that worked.
    """
    if session.status == Session.ARCHIVED:
        return "already_closed"

    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    if is_runner_reported(binding):
        # `is_reachable`, not ONLINE: a close FINISHES work, and a paused daemon
        # keeps its poll loop (`_report_sweep` drains closes above the pause gate).
        if not binding.runner.is_reachable:
            return "unavailable"
        # Cancel BEFORE relaying: deleting the emdash task kills the process the
        # turn runs in, which would leave a live turn EXECUTING with nobody to
        # finish it, and the ledger records a cancellation rather than silence.
        cancel_session_turns(session)
        # Record BEFORE relaying: the frame is the doorbell, `close_requested` is
        # what the runner drains (`GET /runners/{id}/closes`). A new request (the
        # Retry beside "Close failed: ...") starts clean.
        binding.close_requested = True
        binding.close_error = ""
        binding.close_error_at = None
        binding.save(update_fields=["close_requested", "close_error", "close_error_at"])
        # Who asked, now: the box's report that retires the row names only the box.
        lifecycle.record(lifecycle.CLOSE_REQUESTED, session, runner=str(binding.runner_id))
        from apps.realtime import groups

        groups.publish(groups.runner_group(binding.runner_id), {
            "type": "runner.close_session",
            "session_id": str(session.id),
            "session_key": binding.session_key,
            # A DELETE aimed by name alone can remove another agent's same-named
            # session; the runner refuses one it cannot place in a project.
            "project": binding.emdash_project or session.emdash_project,
        })
        return "closing"

    cancel_session_turns(session)
    session.status = Session.ARCHIVED
    with lifecycle.acting(closing=True):
        session.save(update_fields=["status", "updated_at"])
    # The REPORTED branch announces itself when the runner's report retires the
    # row; this one never gets a report, so it says so here.
    from apps.harness.session_sync import fire_sessions_closed

    transaction.on_commit(lambda: fire_sessions_closed([session.pk]))
    return "closed"


def archive_session(session: Session, *, by: str = "", user=None) -> None:
    """Retire a session by hand, without touching its runner's task (that is
    `close_session`). Its open turns are cancelled the way a close cancels them
    (`cancel_session_turns`) — an archived session is one nobody is watching, so
    nothing may keep running or wake up in it — and it is marked
    `Session.HAND_ARCHIVED_KEY` so a laptop's next report does not bring it straight back.
    Idempotent."""
    cancel_session_turns(session, by=by)
    if session.status == Session.ARCHIVED:
        return
    session.status = Session.ARCHIVED
    session.metadata = {**(session.metadata or {}), Session.HAND_ARCHIVED_KEY: True}
    with lifecycle.acting(by=user, reason="archived by hand"):
        session.save(update_fields=["status", "metadata", "updated_at"])


def unarchive_session(session: Session, *, user=None, reason: str = "unarchived") -> bool:
    """Bring an archived session back (the WRITTEN half of `state=active`; a
    runner session past the liveness window stays out until it is reported).
    Returns whether anything changed."""
    held = Session.HAND_ARCHIVED_KEY in (session.metadata or {})
    if session.status != Session.ARCHIVED and not held:
        return False
    session.status = Session.ACTIVE
    session.metadata = {k: v for k, v in (session.metadata or {}).items() if k != Session.HAND_ARCHIVED_KEY}
    with lifecycle.acting(by=user, reason=reason):
        session.save(update_fields=["status", "metadata", "updated_at"])
    return True


def close_agent_sessions(agent) -> dict[str, int]:
    """Close every open session of an agent that is about to be deleted.

    Deleting an agent only SET_NULLs `Session.agent`, so without this its sessions
    stay listed and their emdash tasks stay on the box, reported forever with no
    agent behind them. Each goes through `close_session`, with one difference: a
    session whose runner is offline gets its close recorded anyway
    (`close_requested`, drained on the runner's next poll tick) instead of the
    `unavailable` refusal — once the agent is gone there is nowhere left to retry
    from. Returns a count per outcome.
    """
    counts: dict[str, int] = {}
    open_sessions = (
        Session.objects.filter(agent=agent).exclude(status=Session.ARCHIVED)
        .select_related("runner_binding__runner")
    )
    for session in open_sessions:
        outcome = close_session(session=session)
        if outcome == "unavailable":
            cancel_session_turns(session)
            binding = session.runner_binding
            binding.close_requested = True
            binding.close_error = ""
            binding.close_error_at = None
            binding.save(update_fields=["close_requested", "close_error", "close_error_at"])
            lifecycle.record(lifecycle.CLOSE_REQUESTED, session, runner=str(binding.runner_id),
                             reason="agent deleted")
            outcome = "queued"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts

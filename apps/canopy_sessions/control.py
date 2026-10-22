"""Controlling a live session: answering an agent's dialog, interrupting,
stopping and cancelling its turns, and closing it.
"""
from __future__ import annotations

import time
import uuid

from django.db import transaction
from django.db.models import Q

from . import staleness
from .models import Session


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
    """Stop whatever this session's agent is doing, by interrupting its TERMINAL.

    The turn-shaped stop (`cancel_session_turns` below) can only reach work a
    non-terminal Turn still owns, which in practice means chat. An agent, board or
    scheduled turn is fire-and-continue: `execute_turn` finishes it the moment the
    prompt is delivered (runner execute.py), so seconds later the agent is working
    hard on a turn that is already DONE, and a stop keyed on turns finds nothing to
    cancel and silently does nothing.

    But the work is not turn-shaped, it is SESSION-shaped, and canopy already knows
    the session: `harness.services.record_session` gives every agent/project/phone
    thread a durable Session plus a RunnerBinding carrying `session_key` — the very
    same binding `answer_menu` above uses to press a key into that terminal. So
    stopping is addressed exactly like answering: name the session, let the runner
    own the keystroke.

    Returns "sent" | "unavailable" | "unbound", mirroring `answer_menu`'s refusal
    shape rather than raising — a session can go idle between a thumb reaching the
    button and the frame landing, and that is ordinary, not an error.
    """
    binding = getattr(session, "runner_binding", None)
    if binding is None or binding.runner_id is None or not binding.session_key:
        return "unbound"
    # Same reasoning as answer_menu: pause stops STARTING work, never finishing it,
    # and an agent mid-turn is work already running. Reachable, not available.
    if not binding.runner.is_reachable:
        return "unavailable"

    from apps.realtime import groups
    groups.publish(groups.runner_group(binding.runner_id), {
        "type": "runner.session_interrupt",
        "session_id": str(session.id),
        "session_key": binding.session_key,
    })
    return "sent"


def person_name(user) -> str:
    """A user's name for "Stopped by <name>", or "" for no one / an anonymous caller."""
    if user is None or not getattr(user, "is_authenticated", False):
        return ""
    return (user.get_full_name() or user.email or "").strip()


def cancel_session_turns(session: Session, *, by: str = "") -> bool:
    """Cancel every non-terminal turn on a session. Returns whether anything moved.

    ALL non-terminal turns, not just the newest: a mid-reply send queues a second
    turn behind the one still running, so both must be reached — the running one
    gets cancel_requested, the queued one is finished CANCELLED.

    Deliberately NOT `any(cancel_turn(t) for t in turns)`: any() short-circuits on
    the first truthy result and would skip every turn after it.

    "On a session" means two things, and this used to see only the first:

    * a CHAT turn names its session (`chat_session`);
    * an AGENT or PROJECT turn names no session (the target is agent XOR project
      XOR chat_session) — it is tied to one by the key its runner stamped on it
      (`stamp_turn_session`), the same key the session's RunnerBinding carries.

    On a laptop the second kind is terminal seconds after delivery, so missing it
    cost nothing. On a CLOUD runner it stays RUNNING for the whole of the work —
    and a stop that cannot see it found "nothing to cancel" and fell through to a
    session interrupt the cloud runner never acted on (cloud-ec2-2, 2026-10-17:
    stopped twice, closed once, ran 23 more minutes). Matched on the binding's
    runner and the session's own target too, because a laptop's key is an emdash
    task name and those are unique per project only.
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
    """Stop this session, by whichever route actually owns the running work.
    Returns WHICH route fired: "turns" | "session" | "".

    TURNS FIRST. A chat reply is owned by a live Turn: cancelling it records the
    intent, reaches a queued turn behind the running one, and lets the runner's
    bridge interrupt and finish it. So is ANY turn a cloud runner is executing —
    agent and project turns included, found by the session key the runner stamped
    on them (`cancel_session_turns`) — and the cloud runner stops its process and
    finishes it CANCELLED.

    THEN THE SESSION. If no turn moved, the work is not turn-shaped — the normal
    state of an agent, board, scheduled or project turn, which goes terminal the
    moment its prompt is delivered — so stop it as a session (`interrupt_session`).

    Deliberately not both: a chat turn's cancel already interrupts the same session
    through the bridge, and a second interrupt could land after the agent moved on.

    The ONE stop. The web UI (the session websocket) and REST/MCP
    (`POST /canopy-sessions/{id}/stop`) both call this; REST used to stop at the
    turns, so a stop from MCP or a script did nothing to a working agent
    (canopy-web#1226).
    """
    if cancel_session_turns(session, by=by):
        return "turns"
    return "session" if interrupt_session(session) == "sent" else ""


def is_runner_reported(binding) -> bool:
    """Is a runner CURRENTLY reporting an emdash task for this session?

    The one question `close_session` branches on, observed rather than inferred.
    `Runner.kind` would answer "what program is this" — a different question, and
    already deprecated as a behavioural input. `live_seen_at` and `session_key`
    cannot answer it at all: `record_session` is called by BOTH runners and stamps
    both, with the cloud runner writing a Claude session id where the laptop writes
    an emdash task name. Hence `reported_at`, which only the report loop writes.

    Read against the same `stale_cutoff()` the session list uses, so "reported"
    and "live" can never drift into meaning different windows.
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

    REPORTED (a laptop's emdash task): cancel the turns, then relay a close and
    write NOTHING to the session. The emdash task is the truth for a local session,
    and `replace_reported_sessions` un-archives anything re-reported as open, so a
    status write here would be undone within ~10s anyway. The runner deletes the
    task and puts its name in the `archived:` closing signal on its next report;
    that is what retires the row.

    UNREPORTED (a cloud session, a web chat that never bound): no emdash task exists
    on a box, but a cloud turn may still be RUNNING there. Cancel the turns — that
    stops a running cloud turn (its runner is told on the socket and on its next
    heartbeat, and finishes it CANCELLED) and keeps a queued one from waking it —
    then archive, done. It sticks, because nothing will ever report it back.

    A refusal is a returned reason, never a raise: a session can go stale between
    the phone rendering the list and a thumb reaching it, which is ordinary rather
    than a client error. `unavailable` deliberately does NOT queue — a close that
    sits until a box returns is indistinguishable from one that worked.
    """
    from apps.harness.models import Runner  # framework->framework; lazy, import cycle

    if session.status == Session.ARCHIVED:
        return "already_closed"

    binding = getattr(session, "runner_binding", None)  # reverse 1:1 -> None when absent
    if is_runner_reported(binding):
        reachable = {Runner.ONLINE, Runner.DEGRADED}
        if binding.runner.live_status not in reachable:
            return "unavailable"
        # Cancel BEFORE relaying. Deleting the emdash task kills the process the
        # turn runs in, so a live turn would otherwise stay EXECUTING with nobody
        # left to finish it — held until the lease sweep, wedging the agent through
        # one_executing_turn_per_agent. Cancelling first also means the ledger
        # records a cancellation rather than a turn that merely stops emitting.
        cancel_session_turns(session)
        # Record BEFORE relaying, for the same reason `answer_menu` does: the frame
        # is the doorbell and this is what survives the channel being down. Without
        # it a lost frame leaves the emdash task open and the session active
        # forever — and `/close`'s own fallback ("the task's plain absence from the
        # following report retires it anyway") assumes the runner deleted the task,
        # which never happened. Verified 2026-08-01: the API answered
        # `{"ok":true,"closing":true}` and the runner logged nothing at all.
        binding.close_requested = True
        # A new request (the Retry next to "Close failed: …") starts clean: the
        # runner reports again if this attempt fails too.
        binding.close_error = ""
        binding.close_error_at = None
        binding.save(update_fields=["close_requested", "close_error", "close_error_at"])
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
    session.save(update_fields=["status", "updated_at"])
    # The REPORTED branch announces itself when the runner's report retires the
    # row; this one never gets a report, so it says so here.
    from apps.harness.services import fire_sessions_closed

    transaction.on_commit(lambda: fire_sessions_closed([session.pk]))
    return "closed"


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
            outcome = "queued"
        counts[outcome] = counts.get(outcome, 0) + 1
    return counts

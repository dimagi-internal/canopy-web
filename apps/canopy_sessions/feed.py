"""The supervisor FEED — is this session waiting on THIS person's next prompt?

Decided here, once, and asked by both things that act on the answer: the feed
itself (`/supervisor`, via `GET /api/canopy-sessions/?reply=true`, which stamps
every row with `feed_status`) and every push about a session
(`apps.push.services`: "is asking", "is done"), so a person is pushed about
exactly what lands on their feed.

Two halves, because a push already KNOWS the first one from the event that
fired it (a dialog appeared; a turn finished) while the list has to read it:

  - `needs_next_prompt` — the conversation is the person's move: blocked on a
    dialog, or the agent had the last word, has stopped (`session_state`) and
    nothing is queued for it.
  - `held` — why a waiting session is still not on this person's feed:
      NOT_YOURS  it runs on somebody else's runner and they did not start it —
                 the box's owner drives it and sees it on their own feed. A
                 cloud box has nobody at it, so a session an agent opened there
                 belongs to the agent's owner;
      PARKED     its runner is paused or offline AND something is demonstrably
                 held up there: a dialog is waiting (`waiting_on_you`) or a turn
                 for it is queued/in flight (`turn_open`). An idle session on a
                 paused box — the agent finished, nothing asked, nothing queued —
                 is NOT parked and not on the feed at all: a deliberate pause is
                 not something to nag about. Supervisor →
                 Runners counts those open sessions on the runner's row instead;
      AUTO       it ran on its own (`staleness.ran_on_its_own`: auto mode, and
                 not a person's conversation, or an agent started it), so it did
                 not stop to wait for anybody. The feed can show these on
                 request; nothing pushes about them.
"""

from __future__ import annotations

from . import staleness

WAITING = "waiting"
NOT_YOURS = "not_yours"
PARKED = "parked"
AUTO = "auto"


def needs_next_prompt(*, waiting_on_you: bool, agent_spoke_last: bool, running: bool,
                      turn_open: bool = False) -> bool:
    """Blocked on a dialog, or finished with the agent having the last word.

    A RUNNING session whose last row is the agent's is mid-turn, not done
    (`session_state.is_running`), and one with a turn queued or in flight for it
    (`turn_open`) has already been answered."""
    return waiting_on_you or (agent_spoke_last and not running and not turn_open)


def ran_on_its_own(session) -> bool:
    """`staleness.ran_on_its_own` for a session, from the first claimed turn that
    drove it — the annotations `transcript_read.with_driving_turn` adds, read
    from the database when the session was loaded without them."""
    if hasattr(session, "_turn_mode"):
        mode, origin, by_agent = session._turn_mode, session._turn_origin, session._turn_by_agent
    else:
        mode, origin, by_agent = driving_turn(session)
    return staleness.ran_on_its_own(
        mode=mode or "", origin=origin or "", slack_thread=_in_slack_thread(session),
        started_by_agent=bool(by_agent))


def turn_ran_on_its_own(session, turn) -> bool:
    """`staleness.ran_on_its_own` judged by `turn`, the one that drove the session."""
    return staleness.ran_on_its_own(
        mode=turn.turn_mode, origin=turn.origin, slack_thread=_in_slack_thread(session),
        started_by_agent=staleness.turn_started_by_agent(turn))


def _in_slack_thread(session) -> bool:
    return "slack_thread" in (session.metadata or {})


def held(viewer, session, *, auto: bool) -> str:
    """Why `session` stays off `viewer`'s feed even when waiting, or "" when it does
    not. `auto`: it ran on its own (`ran_on_its_own`)."""
    from apps.harness.models import Runner

    binding = getattr(session, "runner_binding", None)
    runner = binding.runner if (binding is not None and binding.runner_id) else None
    if runner is not None and not _is_yours(viewer, session, runner):
        return NOT_YOURS
    # Unbound counts as live: a web chat gets its runner on its first send.
    if runner is not None and runner.live_status != Runner.ONLINE:
        return PARKED
    if auto:
        return AUTO
    return ""


def status(
    viewer, session, *, waiting_on_you: bool, agent_spoke_last: bool, running: bool,
    auto: bool, turn_open: bool = False,
) -> str:
    """`WAITING` when on the viewer's feed; a `held` reason when waiting but held
    back; "" when the session is not waiting on anybody.

    `turn_open`: a turn that drives this session is queued or in flight
    (`Turn.NON_TERMINAL`). It is the only evidence besides a dialog that a session
    on a paused/offline runner is held up — so `PARKED` means exactly one of the
    two, and the feed can say which (a parked row that is not `waiting_on_you`
    has a turn stuck behind the box)."""
    reason = held(viewer, session, auto=auto)
    if reason == PARKED:
        return PARKED if (waiting_on_you or turn_open) else ""
    if not needs_next_prompt(
        waiting_on_you=waiting_on_you, agent_spoke_last=agent_spoke_last, running=running,
        turn_open=turn_open,
    ):
        return ""
    return reason or WAITING


def driving_turn(session) -> tuple[str, str, bool]:
    """(mode, origin, started by an agent) of the first claimed turn that drove
    `session` — the one-row form of `transcript_read.with_driving_turn`."""
    from .models import Session
    from .transcript_read import with_driving_turn

    row = (
        with_driving_turn(Session.objects.filter(pk=session.pk))
        .values_list("_turn_mode", "_turn_origin", "_turn_by_agent")
        .first()
    )
    return (row[0] or "", row[1] or "", bool(row[2])) if row else ("", "", False)


def _is_yours(viewer, session, runner) -> bool:
    # You started it, or it is on your box. Ownership, not administration: a
    # runner admin may fix a colleague's box, but the conversations on it are
    # still the colleague's. A runner with no owner belongs to nobody's feed
    # (a NULL never means allow — see can_administer_runner).
    #
    # Except on a cloud runner: nobody sits at that box, so a session an agent
    # opened there (a dispatch, a scheduled turn — no creator) is its owner's to
    # drive. Without this a manual cloud session waiting for approval reached no
    # one's feed (Jonathan, 2026-10-07).
    from apps.harness.models import Runner

    viewer_id = getattr(viewer, "pk", None)
    if viewer_id is None:
        return False
    if session.created_by_id == viewer_id or runner.owner_id == viewer_id:
        return True
    return (
        runner.kind == Runner.CLOUD
        and session.created_by_id is None
        and session.agent_id is not None
        and session.agent.owner_id == viewer_id
    )

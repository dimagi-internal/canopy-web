"""The supervisor FEED — is this session waiting on THIS person's next prompt?

Decided here, once, and asked by both things that act on the answer: the feed
itself (`/supervisor`, via `GET /api/canopy-sessions/?reply=true`, which stamps
every row with `feed_status`) and every push about a session
(`apps.push.services`: "is asking", "is done"). They used to be two rules — the
feed's lived in the browser (`feedRules.ts`) and the pushes asked only "can this
person open it?" — so a phone buzzed about sessions the feed then hid: a
colleague's laptop session, an agent's own scheduled run (Jonathan, 2026-10-05:
"I only want to be alerted for stuff that is coming onto the feed").

Two halves, because a push already KNOWS the first one from the event that
fired it (a dialog appeared; a turn finished) while the list has to read it:

  - `needs_next_prompt` — the conversation is the person's move: blocked on a
    dialog, or the agent had the last word and has stopped.
  - `held` — why a waiting session is still not on this person's feed:
      NOT_YOURS  it runs on somebody else's runner and they did not start it —
                 the box's owner drives it and sees it on their own feed. A
                 cloud box has nobody at it, so a session an agent opened there
                 belongs to the agent's owner;
      PARKED     its runner is paused or offline, so a reply would only queue;
      AUTO       an agent drove it on its own (auto mode, not a chat), so it
                 did not stop to wait for anybody. The feed can show these on
                 request; nothing pushes about them.
"""

from __future__ import annotations

WAITING = "waiting"
NOT_YOURS = "not_yours"
PARKED = "parked"
AUTO = "auto"


def needs_next_prompt(*, waiting_on_you: bool, agent_spoke_last: bool, running: bool) -> bool:
    """Blocked on a dialog, or finished with the agent having the last word.

    A RUNNING session whose last row is the agent's is mid-turn, not done.
    """
    return waiting_on_you or (agent_spoke_last and not running)


def ran_on_its_own(turn_mode: str | None, turn_origin: str | None) -> bool:
    """Its newest turn ran in `auto` mode from somewhere other than a person's chat."""
    from apps.harness.models import Turn  # framework->framework; lazy to avoid a cycle

    return turn_mode == "auto" and turn_origin != Turn.ORIGIN_CANOPY_WEB_CHAT


def held(viewer, session, *, turn_mode: str | None, turn_origin: str | None) -> str:
    """Why `session` stays off `viewer`'s feed even when waiting, or "" when it does not."""
    from apps.harness.models import Runner

    binding = getattr(session, "runner_binding", None)
    runner = binding.runner if (binding is not None and binding.runner_id) else None
    if runner is not None and not _is_yours(viewer, session, runner):
        return NOT_YOURS
    # Unbound counts as live: a web chat gets its runner on its first send.
    if runner is not None and runner.live_status != Runner.ONLINE:
        return PARKED
    if ran_on_its_own(turn_mode, turn_origin):
        return AUTO
    return ""


def status(
    viewer, session, *, waiting_on_you: bool, agent_spoke_last: bool, running: bool,
    turn_mode: str | None, turn_origin: str | None,
) -> str:
    """`WAITING` when on the viewer's feed; a `held` reason when waiting but held
    back; "" when the session is not waiting on anybody."""
    if not needs_next_prompt(
        waiting_on_you=waiting_on_you, agent_spoke_last=agent_spoke_last, running=running,
    ):
        return ""
    return held(viewer, session, turn_mode=turn_mode, turn_origin=turn_origin) or WAITING


def driving_turn(session) -> tuple[str, str]:
    """(mode, origin) of the newest claimed turn that drove `session` — the one-row
    form of `services.with_driving_turn`, for a caller holding a single session."""
    from .models import Session
    from .services import with_driving_turn

    row = (
        with_driving_turn(Session.objects.filter(pk=session.pk))
        .values_list("_turn_mode", "_turn_origin")
        .first()
    )
    return (row[0] or "", row[1] or "") if row else ("", "")


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

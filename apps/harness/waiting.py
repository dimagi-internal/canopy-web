"""Why is this turn still queued? — said on the turn, in words, not inferred from silence.

The failure this exists for (2026-10-30): a dispatch pinned to an `online, ready`
laptop sat queued for five hours while every status surface said a live runner was
about to take it. They were all reading the runner's HEARTBEAT, and a heartbeat is
not a claim: the daemon that heartbeats and the app that takes turns can be different
things, and one can be asleep while the other reports in. The server could not tell
the two apart because it only stored the heartbeat.

So two facts are now recorded and written onto the turn's own timeline, as `waiting`
events (a kind of their own, so nothing that reacts to `status` rows — Slack, the
chat panel — reacts to these):

- `claim_next_turn` notes WHY a box passed a candidate over (`claim._note_skips`);
- `note_waiting_turns` (a periodic sweep) writes one plain sentence per turn that has
  been queued past `FIRST_NOTE_AFTER`, naming what it is waiting on: a fleet or agent
  hold, the turn ahead, a runner that is up but has not asked for work, runners that
  are offline, paused or signed out, or routing that can never take it.

Throttled: a turn's explanation is re-said only when it changes or `REPEAT_EVERY` has
passed, so a long wait reads as a few lines, not a flood.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from django.utils import timezone

from . import claim
from .ledger import append_events
from .models import FleetHold, Turn, TurnEvent

logger = logging.getLogger(__name__)

#: A turn is explained once it has been queued this long (a healthy one is claimed in seconds).
FIRST_NOTE_AFTER = timedelta(seconds=120)
#: An unchanged explanation is repeated this often.
REPEAT_EVERY = timedelta(minutes=10)
#: A runner that has not asked for work for this long is not "about to take" anything.
STALE_POLL = timedelta(minutes=3)
#: Turns explained per sweep tick.
PER_TICK = 40


def _ago(delta: timedelta) -> str:
    seconds = max(int(delta.total_seconds()), 0)
    if seconds < 90:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 120:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def _runner_phrase(runner, turn: Turn, now: datetime) -> str:
    polled = runner.last_claim_poll_at
    beat = runner.last_heartbeat_at
    seen = f"heartbeat {_ago(now - beat)} ago" if beat else "no heartbeat recorded"
    if polled is None:
        return (f"{runner.name} is online ({seen}) but canopy has no record of it ever asking for work — "
                f"a heartbeat alone does not take turns, so the app that does may be asleep, stuck or closed.")
    quiet = now - polled
    if quiet > STALE_POLL:
        return (f"{runner.name} is online ({seen}) but has not asked for work for {_ago(quiet)} "
                f"(last at {polled:%H:%M} UTC). A heartbeat alone does not take turns, so the app that does "
                f"may be asleep, stuck or closed.")
    skipped = TurnEvent.objects.filter(turn=turn, kind="waiting").order_by("-seq")[:10]
    mine = [(e.payload or {}).get("skipped") for e in skipped if (e.payload or {}).get("runner") == runner.name]
    mine = [m for m in mine if m]
    if mine:
        return f"{runner.name} is asking for work and is passing this turn over: {mine[0]}."
    return (f"{runner.name} asked for work {_ago(quiet)} ago and did not take this turn, and no rule recorded "
            f"why — that would be a bug in the claim, not a setting.")


def explain(turn: Turn, now: datetime | None = None) -> str:
    """One plain sentence (or two) on what a QUEUED turn is waiting for."""
    now = now or timezone.now()
    if FleetHold.is_held():
        return "The whole fleet is on hold, so no runner claims anything until the hold is released."
    agent = turn.acting_agent
    if agent is not None and getattr(agent, "held", False):
        return f"{agent.slug} is on hold, so none of its turns are claimed until the hold is released."
    reach = claim.turn_reach(turn)
    if reach.behind is not None:
        b = reach.behind
        return (f"Waiting behind turn {str(b.pk)[:8]} ({b.status}) in the same conversation; "
                f"it takes this one when that one ends.")
    names = ", ".join(r.name for r in reach.runners) or "none"
    if reach.kind == claim.LIVE:
        targets = [turn.pinned_runner] if turn.pinned_runner_id else list(reach.runners)
        return " ".join(_runner_phrase(r, turn, now) for r in targets)
    if reach.kind == claim.OFFLINE:
        return f"No runner that could take it is online (candidates: {names})."
    if reach.kind == claim.PARKED:
        return f"Every runner that could take it is paused (candidates: {names})."
    if reach.kind == claim.SIGNED_OUT:
        return f"Runners are online but {reach.cli or 'the agent CLI'} is signed out on all of them ({names})."
    return reach.reason or "No runner is set up that could ever take it; the agent's routing needs fixing."


def note_waiting_turns(now: datetime | None = None) -> int:
    """Write an explanation onto every turn queued past FIRST_NOTE_AFTER whose last
    explanation is stale. Returns how many were written. Never raises."""
    now = now or timezone.now()
    written = 0
    try:
        queued = (Turn.objects.filter(status=Turn.QUEUED, created_at__lte=now - FIRST_NOTE_AFTER)
                  .order_by("created_at")[:PER_TICK])
        for turn in queued:
            try:
                why = explain(turn, now)
                last = (TurnEvent.objects.filter(turn=turn, kind="waiting", payload__has_key="why")
                        .order_by("-seq").first())
                if last is not None and (last.payload or {}).get("why") == why \
                        and now - last.ts < REPEAT_EVERY:
                    continue
                append_events(turn, [{"kind": "waiting", "payload": {"why": why}}])
                written += 1
            except Exception:  # noqa: BLE001 — one turn's explanation must not stop the rest
                logger.exception("could not explain queued turn %s", turn.pk)
    except Exception:  # noqa: BLE001
        logger.exception("note_waiting_turns failed")
    return written

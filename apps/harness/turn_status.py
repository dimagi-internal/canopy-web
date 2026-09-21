"""What is happening with one ask, right now — the one answer every channel renders.

The failure this exists for is silence, and it was never Slack's failure alone: a
person who sent something and sees nothing cannot tell "a runner is working on
it" from "nothing will ever pick this up", whether they are reading a Slack
thread, a canopy chat page, or an agent embedded in somebody else's product.

`apps/slack/status.py` answered it first and answered it well — states derived
from the `Turn` plus `turn_reach`, including the three no client can infer for
itself: its runner is offline, no runner is set up for this agent, its runner
went offline mid-turn. But it answered INSIDE the Slack app, so the web guessed
at a thinner version client-side (one boolean, plus a by-name match against the
fleet list that fails open whenever `GET /runners/` omits a retired runner) and
the embedded widget did not answer it at all.

So the derivation lives here, beside `turn_reach`, and each channel renders it:
Slack to mrkdwn and a button, the session socket to a frame the shared chat kit
draws. A new channel renders a status rather than inventing one — which is the
whole test of whether this is the right seam.

**The states are the ones a PERSON distinguishes**, which is why "queued" is
three of them. The wait looks identical in all three; what you should do about
it does not — wait, go open your laptop, or go fix the agent's routing. A
channel is free to collapse them (Slack's native indicator has only three
levels) but it must collapse from the full set rather than from a guess.

Deliberately NOT a model or a column. The answer is a function of rows that
already exist and change on their own clock (`Turn.status`, the runner's
heartbeat); persisting it would create a second source of truth that goes stale
exactly when the runner dies — which is the one case it has to get right.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .models import Turn

#: Queued, and a live runner is going to take it. The only queued state that
#: resolves itself.
PICKING_UP = "picking_up"
#: Queued, its runner is offline. Resolves when a human opens the laptop — or
#: when the ask is moved to a runner that is up.
WAITING_RUNNER = "waiting_runner"
#: Queued, and nothing is set up that could ever take it. Never resolves on its
#: own; the agent's routing has to be fixed.
UNROUTED = "unrouted"
#: Claimed and producing.
WORKING = "working"
#: The agent stopped to ask a person something.
BLOCKED = "blocked"
#: Claimed, and then its box stopped heartbeating — a closed laptop. The turn
#: still reads RUNNING (a dead runner cannot say otherwise, and the lease takes
#: up to 15 minutes to run out), so this is the one state `Turn.status` alone
#: gets wrong.
PAUSED = "paused"
DONE = "done"
CANCELLED = "cancelled"
MISSED = "missed"
FAILED = "failed"
LOST = "lost"

#: Nothing more will happen without someone asking for it.
TERMINAL = frozenset({DONE, CANCELLED, MISSED, FAILED, LOST})
#: The person who asked is owed something and has not got it yet. The union a
#: client turns into "still going" — the distinction between the members is for
#: the words, not for whether to show them.
PENDING = frozenset({PICKING_UP, WAITING_RUNNER, UNROUTED, WORKING, BLOCKED, PAUSED})
#: Nothing is moving and only a person can change that.
STUCK = frozenset({WAITING_RUNNER, UNROUTED, BLOCKED, PAUSED})


@dataclass(frozen=True)
class TurnStatus:
    """One ask's state, and the facts a channel needs to say it in its own voice.

    Carries NAMES rather than model instances so it serializes onto a socket
    unchanged; the rescue offer keeps an id because acting on it needs one.
    """

    state: str
    #: The agent the ask was put to, when there is one.
    agent_slug: str | None = None
    #: The runners the state is ABOUT: the ones that would pick it up while it
    #: is queued, or the one holding it once claimed.
    runners: tuple[str, ...] = ()
    #: The runner holding it, once one does.
    claimed_by: str | None = None
    #: This ask was DIRECTED at its runner rather than offered to the cascade —
    #: a drill, or an explicit "run it on that box". Worth saying out loud: "we
    #: sent it to X" and "X is next in line" are the same wait but not the same
    #: promise, and only one of them is somebody's decision.
    pinned: bool = False
    #: A runner this could be moved to right now, when moving it would help.
    #: Only ever offered — the click re-checks everything, because a status can
    #: outlive its turn.
    cloud_runner: str | None = None
    cloud_runner_id: str | None = None
    #: When the holding runner was last heard from. Only meaningful for PAUSED,
    #: where "last seen" is the thing that tells you whether to keep waiting.
    last_seen_at: datetime | None = None
    #: The agent is sitting on a dialog. A SEPARATE axis from `state` because a
    #: turn blocked on a question is RUNNING as far as the turn is concerned —
    #: collapsing them would draw a spinner over a dialog nobody has answered,
    #: which is the exact lie a status exists to stop.
    menu_pending: bool = False

    @property
    def settled(self) -> bool:
        return self.state in TERMINAL

    @property
    def pending(self) -> bool:
        return self.state in PENDING

    @property
    def stuck(self) -> bool:
        """Nothing is moving and only a person can change that. The one question
        a 'waiting on you' surface actually asks."""
        return self.state in STUCK or self.menu_pending

    def as_dict(self) -> dict:
        """Wire form. Flat and JSON-native so it rides a socket frame, a REST
        snapshot and a test assertion as the same shape."""
        return {
            "state": self.state,
            "agent_slug": self.agent_slug,
            "runners": list(self.runners),
            "claimed_by": self.claimed_by,
            "pinned": self.pinned,
            "cloud_runner": self.cloud_runner,
            "cloud_runner_id": self.cloud_runner_id,
            "last_seen_at": self.last_seen_at.isoformat() if self.last_seen_at else None,
            "menu_pending": self.menu_pending,
            "settled": self.settled,
            "stuck": self.stuck,
        }


def runner_gone(turn: Turn) -> bool:
    """Claimed and not finished, on a runner that has stopped heartbeating.

    `is_reachable`, not `is_available`: a paused or degraded box still has its
    daemon up and will finish what it holds, so neither is "gone".
    """
    return (turn.status in (Turn.CLAIMED, Turn.RUNNING, Turn.NEEDS_HUMAN)
            and turn.claimed_by_id is not None and not turn.claimed_by.is_reachable)


def movable_lost(turn: Turn) -> bool:
    """A lost turn is worth re-running only while it is the conversation's last
    word — once anything newer exists, the thread has moved on without it."""
    return (turn.status == Turn.LOST and turn.chat_session_id is not None
            and not Turn.objects.filter(chat_session_id=turn.chat_session_id,
                                        created_at__gt=turn.created_at).exists())


def derive(turn: Turn, *, reach=None, cloud=None, menu_pending: bool = False) -> TurnStatus:
    """The state, from facts already in hand.

    Pure: every input is injected, so a channel that already paid for `reach`
    does not pay again and a test can drive all eleven states without a fleet.
    Use `resolve` to gather the inputs first.

    Precedence is load-bearing and matches the order the Slack line has always
    used: queued-ness first (it is the only state `reach` speaks to), then a
    dead holder (which outranks anything the turn itself claims, because the
    turn cannot know), then what the turn says.
    """
    from . import services as harness

    session = turn.chat_session if turn.chat_session_id else None
    agent_slug = session.agent.slug if session is not None and session.agent_id else None
    if agent_slug is None and turn.agent_id:
        agent_slug = turn.agent.slug
    claimed_by = turn.claimed_by.name if turn.claimed_by_id else None

    pinned = False
    if turn.status == Turn.QUEUED:
        if turn.pinned_runner_id and reach is not None and reach.kind == harness.LIVE:
            state, runners, pinned = PICKING_UP, (turn.pinned_runner.name,), True
        elif reach is not None and reach.kind == harness.LIVE:
            state, runners = PICKING_UP, tuple(r.name for r in reach.runners)
        elif reach is not None and reach.kind == harness.OFFLINE:
            state, runners = WAITING_RUNNER, tuple(r.name for r in reach.runners)
        else:
            state, runners = UNROUTED, ()
    elif runner_gone(turn):
        state, runners = PAUSED, ((claimed_by,) if claimed_by else ())
    elif turn.status in (Turn.CLAIMED, Turn.RUNNING):
        state, runners = WORKING, ((claimed_by,) if claimed_by else ())
    elif turn.status == Turn.NEEDS_HUMAN:
        state, runners = BLOCKED, ((claimed_by,) if claimed_by else ())
    elif turn.status == Turn.DONE:
        state, runners = DONE, ((claimed_by,) if claimed_by else ())
    elif turn.status == Turn.CANCELLED:
        state, runners = CANCELLED, ()
    elif turn.status == Turn.MISSED:
        state, runners = MISSED, ()
    elif turn.status == Turn.LOST:
        state, runners = LOST, ((claimed_by,) if claimed_by else ())
    else:
        state, runners = FAILED, ((claimed_by,) if claimed_by else ())

    return TurnStatus(
        state=state,
        agent_slug=agent_slug,
        runners=runners,
        claimed_by=claimed_by,
        pinned=pinned,
        cloud_runner=cloud.name if cloud is not None else None,
        cloud_runner_id=str(cloud.id) if cloud is not None else None,
        last_seen_at=turn.claimed_by.last_heartbeat_at if turn.claimed_by_id else None,
        menu_pending=menu_pending,
    )


def reach_and_cloud(turn: Turn):
    """(reach, cloud runner): reach only for a QUEUED turn — it is the only state
    that asks "who WOULD take this" — and a cloud runner wherever moving it could
    rescue it: queued with nothing live, stranded on a runner that went offline
    mid-turn, or lost while still the conversation's last word.

    Imports inside the function: `canopy_sessions` imports harness at module
    level, and both are framework, so the dependency only works one way at
    import time.
    """
    from apps.canopy_sessions import services as session_services

    from . import services as harness

    if turn.status != Turn.QUEUED:
        if turn.chat_session_id and (runner_gone(turn) or movable_lost(turn)):
            return None, session_services.available_cloud_runner(turn.chat_session)
        return None, None
    reach = harness.turn_reach(turn)
    cloud = None
    if reach.kind != harness.LIVE and turn.chat_session_id:
        cloud = session_services.available_cloud_runner(turn.chat_session)
    return reach, cloud


def resolve(turn: Turn) -> TurnStatus:
    """The state, gathering its own inputs. The entry point for a channel that
    has nothing in hand yet.

    Reads the pending dialog too, so a caller never has to remember that a
    blocked agent is invisible in `Turn.status`.
    """
    from apps.canopy_sessions.serializers import pending_menu

    reach, cloud = reach_and_cloud(turn)
    menu = False
    if turn.chat_session_id:
        menu = pending_menu(turn.chat_session) is not None
    return derive(turn, reach=reach, cloud=cloud, menu_pending=menu)

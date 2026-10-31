"""What is happening with one ask, right now — the one answer every channel renders.

`derive` turns a `Turn` plus `claim.turn_reach` into one of the states below;
`resolve` gathers those inputs itself. Slack renders the result to mrkdwn and a
button (`apps/slack/status.py`), the session socket to a frame the shared chat
kit draws (`canopy_sessions.status_feed`); a new channel renders a status rather
than inventing one, and may collapse states only from the full set.

The states are the ones a PERSON distinguishes, which is why "queued" is five of
them: the wait looks the same, but the remedy differs — wait (for a runner, or for
the turn ahead), open the laptop, sign the CLI in on the box, or fix the agent's
routing.

Not a model or a column: the answer is a function of rows that change on their
own clock (`Turn.status`, the runner's heartbeat), and a stored copy would go
stale exactly when a runner dies.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import datetime

from .models import Turn

#: Queued, and a live runner is going to take it. The only queued state that
#: resolves itself.
PICKING_UP = "picking_up"
#: Queued, a live runner will take it — but not until what is ahead of it ends:
#: its conversation's previous message is still executing, or its session is
#: mid-transfer (`routing.blocking_turn` / `transfer_holding`, the claim's own
#: rules). Resolves on its own, like PICKING_UP.
QUEUED_BEHIND = "queued_behind"
#: Queued, its runner is offline. Resolves when a human opens the laptop — or
#: when the ask is moved to a runner that is up.
WAITING_RUNNER = "waiting_runner"
#: Queued, its runner is online — but the agent CLI it needs is not signed in on
#: it (`Runner.signed_out`, `clis.reported`), so it claims nothing. Resolves when
#: a person signs in on that machine, or the ask is moved.
SIGNED_OUT = "signed_out"
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
PENDING = frozenset({PICKING_UP, QUEUED_BEHIND, WAITING_RUNNER, SIGNED_OUT, UNROUTED, WORKING,
                     BLOCKED, PAUSED})
#: Nothing is moving and only a person can change that.
STUCK = frozenset({WAITING_RUNNER, SIGNED_OUT, UNROUTED, BLOCKED, PAUSED})
#: A failure reason is one sentence on a chat line, not a traceback.
DETAIL_MAX = 300
#: How long a WORKING turn may say nothing before a channel says so. The chat kit
#: carries the same number (`canopy-ui/chat/turnStatus.ts::QUIET_AFTER_MS`).
QUIET_AFTER_SECONDS = 5 * 60


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
    #: WHY a failed ask failed, in the runner's words (its `result_note`) — for
    #: FAILED, the one state whose cause cannot be inferred from the state itself
    #: — or, for CANCELLED, the admission rule that rejected it (`rejection`).
    detail: str | None = None
    #: When the turn reached a terminal state. Lets a client tell a failure that
    #: is still the conversation's last word from one the session has since
    #: moved past — work typed straight into the session makes no Turn, so the
    #: latest Turn can be a day-old failure under a thread that is plainly fine.
    finished_at: datetime | None = None
    #: What the conversation's host requires of the box (e.g. `sensitive_data`). Only set
    #: while QUEUED, and only when the requirement is what blocks it — some
    #: runner would take the turn without it (`Reach.blocked_by_requirements`).
    requires: tuple[str, ...] = ()
    #: For QUEUED_BEHIND: what holds it — "session" (the previous message in
    #: this conversation, a transfer's turn included) or "transfer" (the session
    #: is moving to another box and its source is still packing the work).
    behind: str | None = None
    #: For WAITING_RUNNER: its runners are not asleep but PAUSED — somebody
    #: decided (`claim.Reach` PARKED). `held_until` is when the first resumes on
    #: its own; None means only a person resuming one will run it.
    parked: bool = False
    held_until: datetime | None = None
    #: For WORKING: when its runner took it, and when anything last came back from
    #: it (a ledger event — reply text, a tool call — or the claim itself). Facts,
    #: not a verdict: the client says "nothing back from cloud-ec2-2 for 6m" once
    #: it has been quiet for QUIET_AFTER_SECONDS (`quiet_since`).
    claimed_at: datetime | None = None
    last_progress_at: datetime | None = None
    #: For WORKING: would a follow-up sent NOW be typed into the running turn
    #: (`routing.may_ride`) rather than wait for it to end?
    #: `midturn_blocked` says why not, when it would not: "runner" (the box
    #: holding the turn does not deliver mid-turn — the cloud runner), "dialog"
    #: (the agent is on a question; a follow-up waits for the answer) or
    #: "confined" (a caller's turn runs in a session of its own).
    midturn: bool = False
    midturn_blocked: str | None = None

    def quiet_since(self, now: datetime) -> datetime | None:
        """When anything last came back from a WORKING turn, if that was at least
        QUIET_AFTER_SECONDS before `now` — the fact a channel states instead of
        "working" ("nothing back from cloud-ec2-2 since 15:50"). None otherwise,
        and never over a dialog the agent is waiting on."""
        if self.state != WORKING or self.menu_pending or self.last_progress_at is None:
            return None
        if (now - self.last_progress_at).total_seconds() < QUIET_AFTER_SECONDS:
            return None
        return self.last_progress_at

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
            "detail": self.detail,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "requires": list(self.requires),
            "behind": self.behind,
            "parked": self.parked,
            "held_until": self.held_until.isoformat() if self.held_until else None,
            "claimed_at": self.claimed_at.isoformat() if self.claimed_at else None,
            "last_progress_at": self.last_progress_at.isoformat() if self.last_progress_at else None,
            "midturn": self.midturn,
            "midturn_blocked": self.midturn_blocked,
            "settled": self.settled,
            "stuck": self.stuck,
        }


def rejection(turn: Turn) -> str | None:
    """Why canopy closed this turn before it ran, when an admission rule did —
    else None (a person pressing stop is not a rejection). The sender of a chat,
    Slack or MCP ask learns it here; an emailer from the rule's own notice."""
    from apps.admission.evaluate import REJECTED_PREFIX

    note = (turn.result_note or "").strip()
    if not note.startswith(REJECTED_PREFIX):
        return None
    return note.removeprefix(REJECTED_PREFIX).strip()[:DETAIL_MAX] or None


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


def derive(turn: Turn, *, reach=None, cloud=None, menu_pending: bool = False,
           progress_at: datetime | None = None) -> TurnStatus:
    """The state, from facts already in hand.

    Pure: every input is injected, so a channel that already paid for `reach`
    does not pay again and a test can drive all thirteen states without a fleet.
    Use `resolve` to gather the inputs first.

    Precedence is load-bearing: queued-ness first (it is the only state `reach`
    speaks to), then a dead holder (which outranks anything the turn itself
    claims, because the turn cannot know), then what the turn says.
    """
    from . import claim
    from . import runner_requirements as rr

    session = turn.chat_session if turn.chat_session_id else None
    agent_slug = session.agent.slug if session is not None and session.agent_id else None
    if agent_slug is None and turn.agent_id:
        agent_slug = turn.agent.slug
    claimed_by = turn.claimed_by.name if turn.claimed_by_id else None
    claimed_at = getattr(turn, "claimed_at", None)

    pinned = False
    behind = None
    ahead = getattr(reach, "behind", None)
    moving = getattr(reach, "transfer", False)
    if turn.status == Turn.QUEUED:
        if (ahead is not None or moving) and reach.kind == claim.LIVE:
            # A live runner WILL take it — once what is ahead ends. Named by the
            # runner holding that turn, which is where it will run.
            state = QUEUED_BEHIND
            runners = ((ahead.claimed_by.name,) if ahead is not None and ahead.claimed_by_id
                       else tuple(r.name for r in reach.runners))
            pinned = bool(turn.pinned_runner_id)
            behind = "session" if ahead is not None else "transfer"
        elif turn.pinned_runner_id and reach is not None and reach.kind == claim.LIVE:
            state, runners, pinned = PICKING_UP, (turn.pinned_runner.name,), True
        elif reach is not None and reach.kind == claim.LIVE:
            state, runners = PICKING_UP, tuple(r.name for r in reach.runners)
        elif reach is not None and reach.kind in (claim.OFFLINE, claim.PARKED):
            state, runners = WAITING_RUNNER, tuple(r.name for r in reach.runners)
        elif reach is not None and reach.kind == claim.SIGNED_OUT:
            state, runners = SIGNED_OUT, tuple(r.name for r in reach.runners)
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
        detail=(((turn.result_note or "").strip()[:DETAIL_MAX] or None) if state == FAILED
                else rejection(turn) if state == CANCELLED else None),
        finished_at=turn.finished_at if state in TERMINAL else None,
        # Named only when the requirement is what blocks it (`Reach`), so a turn
        # stuck on routing or confinement is not blamed on the requirement.
        requires=(tuple(sorted(rr.requirements_of(turn)))
                  if turn.status == Turn.QUEUED
                  and getattr(reach, "blocked_by_requirements", False) else ()),
        behind=behind,
        parked=state == WAITING_RUNNER and getattr(reach, "kind", None) == claim.PARKED,
        held_until=getattr(reach, "held_until", None) if state == WAITING_RUNNER else None,
        claimed_at=claimed_at if state == WORKING else None,
        last_progress_at=(max(t for t in (claimed_at, progress_at) if t is not None)
                          if state == WORKING and (claimed_at or progress_at) else None),
    )


def waiting_behind(turn: Turn) -> list[Turn]:
    """Queued turns that may be QUEUED_BEHIND this one: the rest of its
    conversation's queue. When this turn moves — above all when it ends — their
    status lines must be re-said, because nothing about THEM changed and so
    nothing else would re-render them until a channel's sweep came round."""
    if not turn.chat_session_id:
        return []
    return list(Turn.objects.filter(chat_session_id=turn.chat_session_id, status=Turn.QUEUED)
                .exclude(pk=turn.pk))


def reach_and_cloud(turn: Turn):
    """(reach, cloud runner): reach only for a QUEUED turn — it is the only state
    that asks "who WOULD take this" — and a cloud runner wherever moving it could
    rescue it: queued with nothing live, stranded on a runner that went offline
    mid-turn, or lost while still the conversation's last word.

    Imports inside the function: `canopy_sessions` imports harness at module
    level, so the dependency only works one way at import time.
    """
    from apps.canopy_sessions import placement as session_placement

    from . import claim

    if turn.status != Turn.QUEUED:
        if turn.chat_session_id and (runner_gone(turn) or movable_lost(turn)):
            return None, session_placement.available_cloud_runner(turn.chat_session,
                                                                  user=turn.enqueued_by)
        return None, None
    reach = claim.turn_reach(turn)
    # The offer is checked against the turn's SENDER, not whoever is looking: a
    # status is derived once and broadcast to every viewer (no viewer here). The
    # click re-checks the clicker (`services.may_place`).
    cloud = None
    if reach.kind != claim.LIVE and turn.chat_session_id:
        cloud = session_placement.available_cloud_runner(turn.chat_session, user=turn.enqueued_by)
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
    status = derive(turn, reach=reach, cloud=cloud, menu_pending=menu,
                    progress_at=progress_at(turn))
    if status.state != WORKING:
        return status
    rides, blocked = midturn_of(turn, menu_pending=menu)
    return dataclasses.replace(status, midturn=rides, midturn_blocked=blocked)


def midturn_of(turn: Turn, *, menu_pending: bool) -> tuple[bool, str | None]:
    """(would a follow-up sent now ride the running turn, why not) for a WORKING
    turn — the composer's question, asked of the conversation's running turn.

    `routing.may_ride`'s own conditions, holder-side: the queued turn does not
    exist yet, so the ones about IT (pinned elsewhere, an earlier delivery that
    failed) cannot be asked, and a send almost never has them."""
    from . import routing  # noqa: PLC0415 — routing imports canopy_sessions, which imports this module

    holder = turn.rides_turn if turn.rides_turn_id else turn
    if holder is None or holder.status not in (Turn.CLAIMED, Turn.RUNNING) or not turn.chat_session_id:
        return False, None
    if holder.capability:
        return False, "confined"
    if not routing.delivers_midturn(holder.claimed_by):
        return False, "runner"
    if menu_pending or routing._dialog_up(turn.chat_session_id):
        return False, "dialog"
    return True, None


def progress_at(turn: Turn) -> datetime | None:
    """When the newest ledger event of a claimed/running turn landed, else None."""
    if turn.status not in (Turn.CLAIMED, Turn.RUNNING):
        return None
    from django.db.models import Max

    return turn.events.aggregate(at=Max("ts"))["at"]

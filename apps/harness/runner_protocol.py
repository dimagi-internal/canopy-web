"""The runner protocol's frames, read once for both channels.

A runner speaks to canopy over REST (`apps/harness/api_*.py`) and over its
WebSocket (`apps/realtime/consumers.py::RunnerConsumer`). Both channels read a
heartbeat and a turn finish through these functions, with the same Pydantic
schemas (`HeartbeatIn`, `TurnFinishIn`), so a field the REST route honours
cannot be dropped by the socket — a WS beat that ignored `degraded`/`note`
reset a degraded box to ONLINE every 20 seconds.

Who may act as a runner is here too (`acting_runner_q`): its owner, and nobody
when it has none.
"""
from __future__ import annotations

from django.db.models import Q
from pydantic import ValidationError

from . import routing, services
from .models import Runner, Turn
from .schemas import HeartbeatIn, TurnFinishIn

#: The statuses a RUNNER may finish a turn with. `missed` is the scheduler's.
RUNNER_FINISH_STATUSES = (Turn.DONE, Turn.FAILED, Turn.CANCELLED)


class ProtocolError(Exception):
    """A frame the protocol refuses. `status` is the HTTP status the REST route
    answers with; the socket reports `detail` in its ack instead."""

    def __init__(self, status: int, title: str, detail: str = ""):
        super().__init__(detail or title)
        self.status, self.title, self.detail = status, title, detail


def owned_runner_q(user) -> Q:
    """Runners `user` OWNS. A runner nobody owns is owned by nobody — a NULL
    owner fails closed (never `| Q(owner__isnull=True)`)."""
    return Q(owner=user)


def acting_runner_q(user) -> Q:
    """Runners `user` may ACT AS over the control channel (heartbeat, claim,
    report a turn): its own fleet runners. A computer's key holder has no
    runner app, so no control channel."""
    return owned_runner_q(user) & ~Q(kind=Runner.COMPUTER)


# -- heartbeat ----------------------------------------------------------------
def parse_heartbeat_frame(frame: dict) -> HeartbeatIn:
    """A socket frame as a `HeartbeatIn`, field by field. A malformed field
    costs that field its report (it takes the schema default), never the beat:
    losing the beat would take the box offline and stop it claiming."""
    good = {}
    for name in HeartbeatIn.model_fields:
        if name not in frame:
            continue
        try:
            HeartbeatIn.model_validate({name: frame[name]})
        except ValidationError:
            continue
        good[name] = frame[name]
    return HeartbeatIn.model_validate(good)


def data_mode_trusted(runner: Runner, *, device_key: bool) -> bool:
    """Whether a beat's `data_mode` may stand. A sensitive-data box's report is half of
    what routing relies on (`runner_requirements.in_force`), and the other half is a
    declaration its own admin makes — so the report must come from something that
    admin cannot simply type: a cloud box (whose mode its stack sets, not a person)
    signed in as itself with its device key. A laptop's report, or any beat sent with
    a person's token, is not believed."""
    return bool(device_key) and runner.kind == Runner.CLOUD


def heartbeat(runner: Runner, payload: HeartbeatIn, *, device_key: bool = False) -> Runner:
    """Record one beat — THE call both channels make.

    `device_key`: this beat was signed in with the runner's OWN credential, a
    `RunnerAccessToken` its approved device key bought — not a person's token. A
    data mode is believed only from such a beat of a CLOUD runner
    (`data_mode_trusted`); from anything else it is recorded as none."""
    if payload.host and payload.host != runner.host:
        runner.host = payload.host
        runner.save(update_fields=["host"])
    if payload.platform in ("macos", "windows", "linux") and payload.platform != runner.platform:
        runner.platform = payload.platform
        runner.save(update_fields=["platform"])
    return services.heartbeat(
        runner,
        active_turn_ids=payload.active_turn_ids,
        degraded=payload.degraded,
        note=payload.note[:255],
        ready=payload.ready,
        ready_note=payload.ready_note[:200] if not payload.ready else "",
        code_branch=payload.code_branch,
        code_version=payload.code_version,
        code_sha=payload.code_sha,
        code_committed_at=payload.code_committed_at,
        projects=payload.projects,
        profiles=payload.profiles,
        envelope=payload.envelope,
        midturn=payload.midturn,
        clis=payload.clis,
        claude_model=payload.claude_model,
        data_mode=payload.data_mode if data_mode_trusted(runner, device_key=device_key) else "",
        data_mode_hash=(payload.data_mode_hash
                        if data_mode_trusted(runner, device_key=device_key) else ""),
        health=payload.health.model_dump() if payload.health is not None else None,
        mailboxes_readable=payload.mailboxes_readable,
        agents_unprepared=payload.agents_unprepared,
    )


# -- finish -------------------------------------------------------------------
def parse_finish_frame(frame: dict) -> TurnFinishIn:
    try:
        return TurnFinishIn.model_validate(
            {k: v for k, v in frame.items() if k in TurnFinishIn.model_fields})
    except ValidationError as exc:
        raise ProtocolError(422, "Invalid finish", detail=str(exc)) from exc


def finish(turn: Turn, payload: TurnFinishIn) -> Turn:
    """Finish a turn the calling runner claimed — THE call both channels make.

    Refuses a status a runner may not report (422) and a turn it never claimed
    (409); an already-terminal turn is an idempotent no-op."""
    if payload.status not in RUNNER_FINISH_STATUSES:
        raise ProtocolError(422, "Invalid finish", detail="finish status must be done|failed|cancelled")
    # Record the session BEFORE the terminal check: a re-reported finish is otherwise
    # a no-op, and this is the only moment the runner tells us which emdash session
    # the turn drove. Never blank an existing value — the first report wins.
    if payload.session_key and not turn.session_key:
        turn.session_key = payload.session_key[:200]
        turn.save(update_fields=["session_key"])
    if turn.status in Turn.TERMINAL:
        return turn  # idempotent finish
    # Read the state BEFORE finishing: a non-terminal result is ambiguous. It
    # means either "you tried to finish a turn you never claimed" (the 409) or
    # "your sessionless failure was put back on the queue" (services.finish_turn's
    # requeue — a SUCCESSFUL finish that legitimately lands on QUEUED). Only the
    # executing case can produce the second, so that is what tells them apart.
    was_executing = turn.status in routing.EXECUTING
    result = services.finish_turn(turn, status=payload.status, result_note=payload.result_note)
    if result.status not in Turn.TERMINAL and not was_executing:
        raise ProtocolError(409, "Turn not finishable", detail=f"status={result.status}")
    return result

"""Pydantic schemas for /api/canopy-sessions."""
from __future__ import annotations

import datetime as dt
import uuid

from ninja import Schema
from pydantic import field_validator

from apps.harness.schemas import Origin, normalize_origin


class SessionCreateIn(Schema):
    agent_slug: str | None = None
    # An agentless PROJECT chat: the repo checkout to drive. Mutually exclusive
    # with agent_slug.
    project: str = ""
    title: str = ""
    metadata: dict = {}
    # Directed placement at creation time: the runner this new chat should run
    # on. Stashed on session.metadata["requested_runner_id"] and consumed by the
    # session's FIRST send (as long as the session is still unbound) — see
    # services.send_message.
    runner_id: uuid.UUID | None = None


class SendIn(Schema):
    text: str
    # Optional client-generated nonce for idempotent (double-submit-safe) sends.
    client_id: str = ""
    # Directed placement for the turn this send enqueues: "wait" pins to the
    # session's currently bound runner, or a runner UUID string pins to that
    # runner outright. None leaves normal routing/stickiness in charge.
    placement: str | None = None
    # What KIND of work this send is, for source-aware routing (spec 2026-07-27).
    # None = the default `canopy_web_chat` (a human typing in the web UI), which
    # a caller cannot spell for itself: `Origin` admits only the POSTABLE values,
    # so the server-only sources keep exactly one producer each. ace-web sets
    # `ace_web` here — without it, a delegated run enqueues as `canopy_web_chat`
    # and an `ace_web` routing rule has nothing to match.
    origin: Origin | None = None

    _norm_origin = field_validator("origin")(staticmethod(normalize_origin))


class PlaceIn(Schema):
    """Body for POST /{session_id}/place — the chat banner's after-the-fact
    directed-placement decision on an already-queued turn."""
    placement: str


class TurnOutMinimal(Schema):
    """Just enough of a Turn for the /place response — the caller only needs to
    confirm the pin took, not the full harness TurnOut shape."""
    id: uuid.UUID
    status: str
    pinned_runner_id: uuid.UUID | None = None


class MessageOut(Schema):
    turn_index: int
    role: str
    plaintext: str
    content: dict
    created_at: dt.datetime


class MessagePageOut(Schema):
    """One backward page of transcript for scroll-back ("Load earlier")."""
    messages: list[MessageOut]
    has_more_before: bool


class SessionOut(Schema):
    # A requested full-history ship is still outstanding (the runner has not sent
    # its final chunk). Lets "Load full session" wait on an exact signal rather
    # than on a timer or on rows-stopped-growing, neither of which can tell "still
    # arriving" from "there was nothing more to send".
    backfill_pending: bool = False
    id: uuid.UUID
    agent_slug: str | None
    project: str
    workspace: str
    title: str
    status: str
    created_at: dt.datetime
    # When the session last DID something (binding.last_interacted_at > newest
    # message > created_at). created_at is when canopy first NOTICED a discovered
    # session, which is identical across a report sweep — useless as an age.
    last_activity_at: dt.datetime
    # Liveness (Plan 4) — computed from the RunnerBinding; a web session with no
    # binding is origin="web", running=False, runner_name=None.
    origin: str = "web"
    running: bool = False
    runner_name: str | None = None
    runner_location: str | None = None
    # Is the bound runner reachable right now? Carried on the session because a
    # session's own payload is the only place a caller who can see the session is
    # guaranteed to learn this: GET /api/harness/runners/ is scoped to runners the
    # caller PAIRED (apps/harness/api.py::_runner_visibility_q), so an embedder's
    # delegated user sees an empty fleet and could not otherwise tell a stalled
    # chat ("bound runner offline, turn waiting") from a slow one. None = no
    # binding, so there is nothing to be offline.
    runner_online: bool | None = None
    # WHY it is not reachable — `Runner.live_status` verbatim (online / paused /
    # stale / degraded / disconnected / retired), None when unbound. Served beside
    # `runner_online` for the same reason RunnerOut serves `paused` beside
    # `status`: a client that only reads the bool still behaves correctly, but it
    # cannot tell a box a human PARKED from one that fell over — and those want
    # opposite things from the reader (unpause it vs go find out what broke).
    runner_status: str | None = None
    session_key: str = ""
    # This session is blocked on a dialog somebody has to answer. A bool here
    # and the dialog itself on the detail read: the list needs to RANK and badge
    # a waiting session, not render its options. Without it a blocked agent and
    # an idle one are indistinguishable in the list — the "it looks like the
    # session stopped" half of the 2026-07-31 spark report.
    waiting_on_you: bool = False


class SessionDetailOut(SessionOut):
    messages: list[MessageOut]
    # The dialog itself, when `waiting_on_you`. Same payload shape whichever
    # half found it (transcript or screen read), so a client never grows two
    # readers — see canopy_transcript.questions.
    menu: dict | None = None
    # Tail-first cursor: the transcript ships the last N messages by default;
    # these tell the client whether earlier history exists and where the loaded
    # window starts, for scroll-back / "load full". See services.SESSION_TAIL_DEFAULT.
    has_more_before: bool = False
    oldest_loaded_turn_index: int | None = None


class SendOut(Schema):
    turn_id: uuid.UUID | None
    message: MessageOut


class StreamStateOut(Schema):
    """Whether the bound runner is being asked to stream this session live."""
    streaming: bool


class BackfillStateOut(Schema):
    """ready = already server-full; requested = runner asked; unavailable = offline."""
    status: str


class AttachmentOut(Schema):
    """An uploaded attachment. Carries no URL — the client builds the content
    path from the id, so there is no signed link to expire or leak."""

    id: uuid.UUID
    filename: str
    content_type: str
    size_bytes: int
    message_id: str | None = None

    @staticmethod
    def resolve_message_id(obj) -> str | None:
        return str(obj.message_id) if obj.message_id else None


class ResetOut(Schema):
    """One session's reset outcome. `reason` is `ok` | `no_binding` |
    `runner_unreachable` — stable strings the UI renders as the refusal."""

    session_id: str
    title: str
    ok: bool
    reason: str
    rows_dropped: int
    runner: str


class ResetIn(Schema):
    """Bulk reset scope. Empty body = every session the caller can see, in the
    workspace the request resolved to."""

    prune_ghosts: bool = False
    dry_run: bool = False


class ResetSummaryOut(Schema):
    dry_run: bool
    reset: list[ResetOut]
    skipped: list[ResetOut]
    pruned: list[dict]
    rows_dropped: int


class MenuAnswerIn(Schema):
    """Which option to press on a blocked agent's dialog.

    `None` means refuse — sent as Escape. Deliberately nullable rather than a
    magic number: every dialog offers Esc, and it is the only answer that is
    safe when the option numbering is not what the client thought it was.

    `selections` carries the whole answer to an `AskUserQuestion`: one list of
    chosen option numbers per declared question, in declaration order, empty for
    a question left unanswered. A single `option` cannot express "Red and Blue",
    and cannot reach the tab holding question 2 at all — so a multi-select or a
    multi-question ask was unanswerable from the web until this field existed.
    `option` is still sent beside it (the first pick) purely so a runner older
    than this behaves exactly as it does today instead of pressing Escape.
    """
    option: int | None = None
    selections: list[list[int]] | None = None

"""Pydantic schemas for /api/canopy-sessions."""
from __future__ import annotations

import datetime as dt
import uuid

from ninja import Schema


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
    session_key: str = ""


class SessionDetailOut(SessionOut):
    messages: list[MessageOut]
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

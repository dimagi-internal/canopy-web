"""Listen sessions over REST: a phone records, its owner's laptop transcribes.

The audio itself never touches these routes; it is relayed, end-to-end encrypted, over
`/ws/listen/<id>/`. Design: docs/architecture/listen-relay.md.
"""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.contacts import people
from apps.tokens import kinds

from . import services
from .models import ListenSession
from .schemas import (
    ListenRunnerOut,
    ListenRunnersOut,
    ListenSessionCreated,
    ListenSessionIn,
    ListenSessionOut,
    PendingListenSession,
    PendingListenSessionsOut,
)

router = Router(auth=session_auth, tags=["listen"])
#: Mounted at /api/runner: what a runner reads with its own key.
runner_router = Router(auth=session_auth, tags=["listen"])


def _the_person_themself(request: HttpRequest) -> None:
    # A recording is the person's own act: no agent login or turn token opens one.
    if kinds.speaks_for_a_turn(request) or people.agent_of_login(request.user) is not None:
        raise HttpError(403, "Only the person may do this")


def _own_or_404(request: HttpRequest, session_id: uuid.UUID) -> ListenSession:
    session = ListenSession.objects.filter(pk=session_id, owner=request.user).first()
    if session is None:
        raise HttpError(404, "listen session not found")
    return session


def _out(session: ListenSession) -> ListenSessionOut:
    return ListenSessionOut(id=session.pk, kind=session.kind, state=session.state,
                            end_reason=session.end_reason, created_at=session.created_at)


@router.get("/runners", response=ListenRunnersOut, summary="Your computers a phone can record to")
@gate("self")
def list_listen_runners(request: HttpRequest):
    """Your runners, each with whether it is online now (its daemon beat in the last 90 seconds)."""
    return ListenRunnersOut(items=[
        ListenRunnerOut(id=r.pk, name=r.name, online=services.is_online(r))
        for r in services.runners_of(request.user)
    ])


@router.post("/sessions", response={201: ListenSessionCreated}, summary="Open a listen session")
@gate("self", kind="conversation")
def open_listen_session(request: HttpRequest, payload: ListenSessionIn):
    """Open a relay from this phone to one of your computers, and tell that computer.

    `hello` carries public keys only. 404 when the runner isn't yours, 409 when it is
    offline or (for a meeting) already has one running."""
    _the_person_themself(request)
    try:
        session = services.create(request.user, runner_id=payload.runner_id, kind=payload.kind,
                                  hello=payload.hello.model_dump())
    except services.Refused as exc:
        raise HttpError(exc.status, str(exc)) from None
    return Status(201, ListenSessionCreated(id=session.pk))


@router.get("/sessions/{session_id}", response=ListenSessionOut, summary="A listen session")
@gate("self")
def get_listen_session(request: HttpRequest, session_id: uuid.UUID):
    """One of your listen sessions: its kind, state and, once ended, why."""
    return _out(_own_or_404(request, session_id))


@router.post("/sessions/{session_id}/end", response=ListenSessionOut, summary="End a listen session")
@gate("self", kind="conversation")
def end_listen_session(request: HttpRequest, session_id: uuid.UUID):
    """End one of your listen sessions now; both of its sockets are closed."""
    _the_person_themself(request)
    session = _own_or_404(request, session_id)
    services.end(session.pk, services.ENDED_BY_OWNER)
    session.refresh_from_db()
    return _out(session)


@runner_router.get("/listen-sessions/pending", response=PendingListenSessionsOut,
                   summary="Listen sessions waiting for this runner")
@gate("runner")
def pending_listen_sessions(request: HttpRequest, runner_id: uuid.UUID | None = None):
    """Sessions waiting for this runner, made in the last 2 minutes: the fallback when
    its control socket is down. A runner's own key reads its own; a person's token
    reads the runner named by `runner_id`, or all of theirs."""
    rtok = getattr(request, "runner_token", None)
    runners = services.runners_of(request.user)
    if rtok is not None:
        if runner_id is not None and runner_id != rtok.runner_id:
            raise HttpError(404, "runner not found")
        runners = runners.filter(pk=rtok.runner_id)
    elif runner_id is not None:
        runners = runners.filter(pk=runner_id)
    return PendingListenSessionsOut(items=[
        PendingListenSession(session_id=s.pk, runner_id=s.runner_id, kind=s.kind, hello=s.hello)
        for s in services.pending_for(runners)
    ])

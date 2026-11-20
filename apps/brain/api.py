"""`/api/me/learning/`: a person's agreement to canopy learning, and taking one session out of it.

PERSON-ONLY. Every route asks `acting_person.refusal` and answers 403 for an agent's login, a
turn's token, a runner or a site's token, and none is an MCP tool (`apps/mcp/api_tools.py`
EXCLUDED): an agent that could turn this on would be consenting for someone. Reading is the
person's too. `docs/architecture/brain.md`."""
from __future__ import annotations

import uuid

from django.http import HttpRequest
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session
from apps.contacts import people
from apps.harness import acting_person

from . import consent, participants, sources
from .models import CLASSES, SourceExclusion
from .schemas import LearningIn, LearningOut, LearningSourceOut

router = Router(auth=session_auth, tags=["brain"])


def _the_person(request: HttpRequest):
    """The signed-in person as themselves, or a 403 naming nothing about why."""
    user = request.user
    if acting_person.refusal(request) is not None or not people.is_human_account(user):
        raise ProblemError(403, "Only the person may do this", type_=TYPE_FORBIDDEN)
    return user


def _out(user) -> dict:
    row = consent.live(user)
    on = set(row.classes) if row else set()
    return {"on": row is not None, "since": row.created_at if row else None,
            "classes": [{"name": c, **consent.CLASS_COPY[c], "on": c in on} for c in CLASSES],
            "workspaces": list(row.workspaces) if row else [], "include_new": row.include_new if row else True,
            "standing_rules": list(row.standing_rules) if row else [],
            "directives": list(row.directives) if row else []}


@router.get("/me/learning/", response=LearningOut, summary="Your agreement to canopy learning")
@gate("self")
def get_my_learning(request: HttpRequest) -> dict:
    """Which classes of learning you agreed to, what each means and who sees what it produces. Only
    you see this."""
    return _out(_the_person(request))


@router.put("/me/learning/", response={200: LearningOut, **refusals(403, 422)},
            summary="Agree to canopy learning, by class")
@gate("self", setting=True)  # who may learn from your sessions
def set_my_learning(request: HttpRequest, payload: LearningIn) -> dict:
    """Your act: the classes you list are the ones canopy may learn from your sessions for, in the
    workspaces you list (none: all of yours). Only you can do this; no admin, agent or token can."""
    user = _the_person(request)
    try:
        consent.set_consent(user, classes=payload.classes, workspaces=payload.workspaces,
                            include_new=payload.include_new, standing_rules=payload.standing_rules,
                            directives=payload.directives)
    except consent.RefusedError as e:
        raise ProblemError(422, "Invalid request", type_=TYPE_VALIDATION, detail=str(e)) from e
    return _out(user)


@router.delete("/me/learning/", response={200: LearningOut, **refusals(403, 404)},
               summary="Stop all canopy learning from your sessions")
@gate("self", setting=True)  # who may learn from your sessions
def revoke_my_learning(request: HttpRequest) -> dict:
    """At once: every capability's next read of your sessions is "not found". Only you can do this."""
    user = _the_person(request)
    if not consent.revoke(user):
        raise ProblemError(404, "You have not agreed to canopy learning", type_=TYPE_NOT_FOUND)
    return _out(user)


def _my_session(request: HttpRequest, user, session_id: uuid.UUID) -> Session:
    """A session `user` is in, else "not found": a stranger learns nothing about whether it exists."""
    session = access.readable_sessions(user).filter(pk=session_id).first()
    if session is None or not any(u == user.pk for _how, u, _c in participants.people_of(session)):
        raise ProblemError(404, "Session not found", type_=TYPE_NOT_FOUND)
    return session


@router.put("/me/learning/sessions/{session_id}/", response={200: LearningSourceOut, **refusals(403, 404)},
            summary="Take one of your sessions out of canopy learning")
@gate("self", setting=True)  # who may learn from this session
def exclude_my_session(request: HttpRequest, session_id: uuid.UUID) -> dict:
    """Your act: no capability may learn from this session again, and what was derived from it is
    withdrawn. If others were in it, it also leaves every shared use; what was learned about them
    from their own words stays. Only you can do this."""
    user = _the_person(request)
    session = _my_session(request, user, session_id)
    consent.exclude(user, sources.for_session(session))
    return {"session_id": session.pk, "excluded": True}


@router.delete("/me/learning/sessions/{session_id}/", response={200: LearningSourceOut, **refusals(403, 404)},
               summary="Allow one of your sessions in canopy learning again")
@gate("self", setting=True)  # who may learn from this session
def include_my_session(request: HttpRequest, session_id: uuid.UUID) -> dict:
    """Undoes the exclusion. Only you can do this. It does not bring back what was withdrawn."""
    user = _the_person(request)
    session = _my_session(request, user, session_id)
    source = sources.existing(session)
    if source is not None:
        consent.include(user, source)
    return {"session_id": session.pk, "excluded": False}


@router.get("/me/learning/sessions/{session_id}/", response={200: LearningSourceOut, **refusals(403, 404)},
            summary="Whether one of your sessions is out of canopy learning")
@gate("self")
def get_my_session_learning(request: HttpRequest, session_id: uuid.UUID) -> dict:
    user = _the_person(request)
    session = _my_session(request, user, session_id)
    source = sources.existing(session)
    out = source is not None and SourceExclusion.objects.filter(user=user, source=source).exists()
    return {"session_id": session.pk, "excluded": out}

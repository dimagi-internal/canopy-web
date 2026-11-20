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
from apps.api.errors import TYPE_CONFLICT, TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError, refusals
from apps.api.route_gates import gate
from apps.agents.models import AgentProject
from apps.agents.services.projects import may_read_project
from apps.canopy_sessions import access
from apps.canopy_sessions.models import Session
from apps.contacts import people
from apps.harness import acting_person
from apps.workspaces import services as wsvc

from . import associate, consent, disclosure, participants, sources, view
from .models import CLASSES, SourceExclusion
from .schemas import AgentLinkIn, DisclosureDayOut, RemovedSessionOut, LearningIn, LearningOut, LearningSourceOut, PersonLinkIn

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
    """At once: every learner's next read of your sessions is "not found". Only you can do this."""
    user = _the_person(request)
    if not consent.revoke(user):
        raise ProblemError(404, "You have not agreed to canopy learning", type_=TYPE_NOT_FOUND)
    return _out(user)


def _my_session(request: HttpRequest, user, session_id: uuid.UUID, *, readable: bool = False) -> Session:
    """A session `user` is in, else "not found": a stranger learns nothing about whether it exists.

    "In it" is `participants.people_of`: their words are in it, even once taken out of it, so they can
    still take it out of learning, see that they did and undo it. A session they excluded stays theirs
    to see and manage whatever happens to them in it. `readable` (saying which project it belongs to)
    also needs them to be able to read it now."""
    session = Session.objects.filter(pk=session_id).first()
    if session is None:
        raise ProblemError(404, "Session not found", type_=TYPE_NOT_FOUND)
    source = sources.existing(session)
    if not readable and source is not None and SourceExclusion.objects.filter(user=user, source=source).exists():
        return session
    in_it = any(u == user.pk for _how, u, _c in participants.people_of(session))
    if not in_it or (readable and not access.readable_sessions(user).filter(pk=session_id).exists()):
        raise ProblemError(404, "Session not found", type_=TYPE_NOT_FOUND)
    return session


def _source_out(user, session) -> dict:
    return view.session_learning(user, session, view.LIVE_SOURCE)


@router.put("/me/learning/sessions/{session_id}/", response={200: LearningSourceOut, **refusals(403, 404)},
            summary="Take one of your sessions out of canopy learning")
@gate("self", setting=True)  # who may learn from this session
def exclude_my_session(request: HttpRequest, session_id: uuid.UUID) -> dict:
    """Your act: nothing may learn from this session again, and what was derived from it is
    withdrawn. If others were in it, it also leaves every shared use; what was learned about them
    from their own words stays. Only you can do this."""
    user = _the_person(request)
    session = _my_session(request, user, session_id)
    consent.exclude(user, sources.for_session(session))
    return _source_out(user, session)


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
    return _source_out(user, session)


@router.get("/me/learning/sessions/{session_id}/", response={200: LearningSourceOut, **refusals(403, 404, 422)},
            summary="Whether one of your sessions is out of canopy learning")
@gate("self")
def get_my_session_learning(request: HttpRequest, session_id: uuid.UUID, source: str = "live") -> dict:
    """Whether you took it out, and the projects it is tied to that you may read. `source=preview:<id>`: the ties one
    of your previews would make (not found when the session is not in it)."""
    from .api_previews import source_of  # noqa: PLC0415 — api_previews imports api_preview, loaded with this app

    user = _the_person(request)
    session = _my_session(request, user, session_id)
    src = source_of(request, source)
    out = view.session_learning(user, session, src)
    if out is None:
        raise ProblemError(404, "Session not in this preview", type_=TYPE_NOT_FOUND)
    return out


# ---- which project a session belongs to (layer 4: declared) -------------------------------------------

def _project_of(user, session, handle: str) -> AgentProject:
    """A WORKSPACE project of the session's own workspace that `user` is in and may read, else "not
    found". Never a personal project: a link to one would hand a session to its lead and agents."""
    project = AgentProject.objects.filter(workspace_id=session.workspace_id, handle=handle.lower()).first()
    if (project is None or project.is_personal or not wsvc.is_member(user, project.workspace_id)
            or not may_read_project(user, project)):
        raise ProblemError(404, "Project not found", type_=TYPE_NOT_FOUND)
    return project


@router.put("/me/learning/sessions/{session_id}/projects/{handle}/",
            response={200: LearningSourceOut, **refusals(403, 404, 409)},
            summary="Say whether one of your sessions belongs to a project")
@gate("self", setting=True)  # who may read this session as part of a project
def declare_my_session_project(request: HttpRequest, session_id: uuid.UUID, handle: str,
                               payload: PersonLinkIn) -> dict:
    """Your act: `core` ties the session to the project (its readers may then learn from it, if everyone
    in it agreed), `rejected` says it does not belong and stays rejected whatever the automatic layers
    later find. Overrides what canopy guessed. Only you can do this. A rejection by someone else in the
    session is theirs to undo (409)."""
    user = _the_person(request)
    session = _my_session(request, user, session_id, readable=True)
    project = _project_of(user, session, handle)
    source = sources.for_session(session)
    row = associate.declare(source, project, tier=payload.tier, by=user)
    if row.by_id != user.pk or row.tier != payload.tier:
        raise ProblemError(409, "Conflict", type_=TYPE_CONFLICT,
                           detail="Someone else in this session rejected this link; only they can undo it")
    return _source_out(user, session)


@router.delete("/me/learning/sessions/{session_id}/projects/{handle}/",
               response={200: LearningSourceOut, **refusals(403, 404)},
               summary="Take back what you said about a session and a project")
@gate("self", setting=True)  # who may read this session as part of a project
def undeclare_my_session_project(request: HttpRequest, session_id: uuid.UUID, handle: str) -> dict:
    """Withdraws your own declaration (not anyone else's); canopy's automatic guess resumes."""
    user = _the_person(request)
    session = _my_session(request, user, session_id, readable=True)
    project = _project_of(user, session, handle)
    source = sources.existing(session)
    if source is not None:
        associate.undeclare(source, project, by=user)
    return _source_out(user, session)


@router.put("/brain/sessions/{session_id}/projects/{handle}/",
            response={200: LearningSourceOut, **refusals(403, 404)},
            summary="An agent says which project the session it is running in worked on")
@gate("agent-login", kind="content")  # the per-turn login of a running turn, for that turn's own conversation
def declare_session_project(request: HttpRequest, session_id: uuid.UUID, handle: str,
                            payload: AgentLinkIn) -> dict:
    """For an agent's own login INSIDE its running turn, at close-out: the conversation this turn runs
    in worked on project `handle` of its workspace (`core`, or `adjacent` if it only touched it).
    Only that conversation (not another session), only a project the agent's own login may read. It
    outranks canopy's guess but never a person's call, and it makes no one's session readable."""
    from apps.harness import turn_asker  # noqa: PLC0415 — harness imports contacts, which imports this app

    turn = turn_asker.serving_turn(request.user)
    agent = turn_asker.turn_agent(turn) if turn is not None else None
    if agent is None or not agent.is_own_login(request.user):
        raise ProblemError(403, "Only an agent's own login, inside its running turn, may do this",
                           type_=TYPE_FORBIDDEN)
    if session_id not in {turn.canopy_session_id, turn.chat_session_id}:
        raise ProblemError(404, "Session not found", type_=TYPE_NOT_FOUND)
    session = Session.objects.filter(pk=session_id).first()
    project = (AgentProject.objects.filter(workspace_id=session.workspace_id, handle=handle.lower()).first()
               if session is not None else None)
    if project is None or project.is_personal or not may_read_project(request.user, project):
        raise ProblemError(404, "Project not found", type_=TYPE_NOT_FOUND)
    if participants.sealed_reason(session):
        raise ProblemError(404, "Session not found", type_=TYPE_NOT_FOUND)
    associate.declare(sources.for_session(session), project, tier=payload.tier, by_agent=agent)
    source = sources.existing(session)
    return {"session_id": session.pk, "excluded": False,
            "projects": [{"project": project.handle, "name": project.name, "tier": link.tier, "how": link.how}
                         for link in source.links.filter(subject=str(project.pk))]}


@router.get("/me/learning/disclosure/", response=list[DisclosureDayOut],
            summary="What was read of your words, by day")
@gate("self")
def get_my_disclosure(request: HttpRequest) -> list[dict]:
    """Per day and learner, how many times what you said was read and how many of your sessions that covered.
    Counts only, never a word, and never your own reads. Only you see this."""
    return disclosure.summary(_the_person(request))


@router.get("/me/learning/removed/", response=list[RemovedSessionOut], summary="Sessions you took out of learning")
@gate("self")
def list_my_removed_sessions(request: HttpRequest) -> list[dict]:
    """The sessions you removed from canopy learning, newest first. Only you see this."""
    user = _the_person(request)
    rows = (SourceExclusion.objects.filter(user=user, source__session__isnull=False)
            .select_related("source__session").order_by("-created_at")[:200])
    return [{"session_id": r.source.session_id, "title": r.source.session.title or "",
             "workspace": r.source.session.workspace_id} for r in rows]

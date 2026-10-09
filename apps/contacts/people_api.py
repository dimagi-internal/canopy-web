"""/api/people — what agents know about a person, and the person's own view of it.

The fleet brain v1 (canopy#804; design: hal `docs/proposals/2026-10-07-caller-
context-brain.md`). The rules, all applied in `apps/contacts/people.py`:

* **Per workspace.** A fact is read and written inside the workspace it
  belongs to, by a member of it (an agent's login is a member). A person a
  workspace does not deal with (`people.known_in`) is "not found" there, so a
  guessed id or address confirms nothing across tenants.
* **The subject sees everything.** `GET /me/` returns every live fact about the
  caller in every workspace, and the last 50 reads of them.
* **Every read is logged** (`PersonAccess`) — the envelope's and this API's.
* **Conversations are the one new read of turn content**, and only for the agent
  that had them: its own login, or its admins. Nothing else widens.
"""
from __future__ import annotations

import datetime as dt
import uuid

from django.http import HttpRequest
from django.utils.dateparse import parse_datetime
from ninja import Router

from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.workspaces import services as wsvc

from . import hcp, people
from . import services as contact_services
from .models import Person, PersonAccess, PersonFact
from .people_schemas import (
    AgentGrantIn,
    AgentMemoryIn,
    AgentMemoryOut,
    SessionMemoryIn,
    SessionMemoryOut,
    PeopleCoverageOut,
    PersonConversationsOut,
    PersonFactCreatedOut,
    PersonFactIn,
    PersonMeOut,
    PersonOut,
    PersonProjectsOut,
    PersonRefOut,
)

router = Router(auth=session_auth, tags=["people"])

ME_ACCESSES = 50


def _not_found(what: str = "Person not found") -> ProblemError:
    return ProblemError(404, what, type_=TYPE_NOT_FOUND)


def _bad(detail: str) -> ProblemError:
    return ProblemError(400, "Invalid request", type_=TYPE_VALIDATION, detail=detail)


def _workspace(request: HttpRequest, explicit: str | None):
    """The workspace a per-person call is about: the explicit slug, else the
    `/api/w/{ws}/` one. The caller must be a member — else 404, never 403."""
    from apps.workspaces.models import Workspace

    slug = (explicit or "").strip() or getattr(request, "workspace_slug", None) or ""
    if not slug:
        raise _bad("say which workspace: ?workspace=<slug> (or the workspace field)")
    if not wsvc.is_member(request.user, slug):
        raise _not_found("Workspace not found")
    ws = Workspace.objects.filter(pk=slug).first()
    if ws is None:
        raise _not_found("Workspace not found")
    return ws


def _person_in(person_id: int, workspace) -> Person:
    person = Person.objects.select_related("user").filter(pk=person_id).first()
    if person is None or not people.known_in(person, workspace.pk):
        raise _not_found()
    return person


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _ref(person: Person) -> dict:
    return {"id": person.pk, "display_name": people.display_name(person),
            "email": people.email_of(person)}


@router.get("/me/", response=PersonMeOut, summary="What agents know about me")
def people_me(request: HttpRequest) -> dict:
    """Everything canopy holds about YOU: every live fact in every workspace
    (corrections first) and the last 50 times an agent or a
    person read it. Any signed-in user; only ever your own."""
    person = contact_services.person_for(user=request.user)
    if person is None:
        raise _not_found()
    facts = []
    for f in people.live_facts(person).select_related("asserted_by_agent", "asserted_by_user"):
        d = people.fact_dict(f)
        d["workspace"] = f.workspace_id
        d["asserted_by"] = (f.asserted_by_agent.slug if f.asserted_by_agent_id
                            else (f.asserted_by_user.email if f.asserted_by_user_id else ""))
        d["source_turn_id"] = str(f.source_turn_id) if f.source_turn_id else None
        facts.append(d)
    accesses = [
        {"created_at": a.created_at.isoformat(), "via": a.via, "workspace": a.workspace_id,
         "reader_agent": a.reader_agent.slug if a.reader_agent_id else None,
         "reader_user": a.reader_user.email if a.reader_user_id else None,
         "turn_id": str(a.turn_id) if a.turn_id else None}
        for a in PersonAccess.objects.filter(person=person)
        .select_related("reader_agent", "reader_user").order_by("-created_at", "-pk")[:ME_ACCESSES]
    ]
    return {**_ref(person), **_memory(person), "facts": facts, "accesses": accesses}


def _memory(person: Person) -> dict:
    return {"agent_memory": {
        f: {"available": bool(getattr(person, f"hcp_{f}_available")),
            "default": bool(getattr(person, f"hcp_{f}_default")),
            "changed_at": _iso(getattr(person, f"hcp_{f}_changed_at"))}
        for f in hcp.FEATURES}}


def _the_person_themself(request: HttpRequest) -> Person:
    """Only the person — never an agent's login, a session acting for them (a caller
    token), or a system account."""
    user = request.user
    if (people.agent_of_login(user) is not None
            or getattr(request, "auth_method", "") == "caller_token"
            or not people.is_human_account(user)):
        raise ProblemError(403, "Only the person may change this", type_=TYPE_FORBIDDEN)
    person = contact_services.person_for(user=user)
    if person is None:
        raise _not_found()
    return person


@router.put("/me/agent-memory/", response=AgentMemoryOut,
            summary="Choose what agent memory is available, and its defaults")
def set_my_agent_memory(request: HttpRequest, payload: AgentMemoryIn) -> dict:
    """Your agent memory at the canopy level. For each of `record` (agents may
    learn about me) and `use` (agents may use what they've learned): `available`
    — may it be on at all — and `default` — is it on in a new session. A session
    can turn either on or off for itself, but never turn on what is not
    available. Off deletes nothing. Only you can change these."""
    person = _the_person_themself(request)
    hcp.set_agent_memory(person, actor=hcp.user_actor(request.user),
                         record=payload.record.model_dump() if payload.record else None,
                         use=payload.use.model_dump() if payload.use else None)
    return _memory(person)["agent_memory"]


def _my_session(request: HttpRequest, session_id: str):
    from apps.canopy_sessions.models import Session

    try:
        sid = uuid.UUID(str(session_id))
    except ValueError:
        raise _not_found() from None
    session = Session.objects.filter(pk=sid, created_by=request.user).first()
    if session is None:            # not yours, or not there: the same answer
        raise _not_found()
    return session


def session_memory_payload(person: Person, session) -> dict:
    state = hcp.memory_state(person, session)
    agent = getattr(session, "agent", None)
    grants = hcp.agent_grants(person, agent, session) if agent is not None else []
    for f in hcp.FEATURES:
        g = next((g for g in grants if f in hcp.features_of(g)), None)
        state[f]["granted"] = g is not None
        state[f]["grant"] = ({"grant_id": hcp.entry_urn(g.grant_id), "type": g.grant_type,
                              "expires_at": _iso(g.expires_at)} if g is not None else None)
    return {"session_id": str(session.pk), **state,
            "agent": ({"slug": agent.slug, "name": agent.name or agent.slug}
                      if agent is not None else None),
            "categories": list(hcp.HELD_CATEGORIES),
            "session_grant_hours": int(hcp.SESSION_GRANT_CAP.total_seconds() // 3600)}


@router.get("/me/sessions/{session_id}/agent-memory/", response=SessionMemoryOut,
            summary="Agent memory in one of your sessions")
def get_my_session_memory(request: HttpRequest, session_id: str) -> dict:
    """What applies in this session of yours: per feature, whether it is
    available, its default, this session's choice, and the effective value."""
    person = _the_person_themself(request)
    return session_memory_payload(person, _my_session(request, session_id))


@router.put("/me/sessions/{session_id}/agent-memory/", response=SessionMemoryOut,
            summary="Turn agent memory on or off for one of your sessions")
def set_my_session_memory(request: HttpRequest, session_id: str,
                          payload: SessionMemoryIn) -> dict:
    """For this session only: `on`, `off` or `inherit` (use your default) for
    `record` and `use`. A feature you have not made available cannot be turned
    on here. Only you can change it."""
    person = _the_person_themself(request)
    session = _my_session(request, session_id)
    try:
        hcp.set_session_memory(person, session, actor=hcp.user_actor(request.user),
                               record=payload.record, use=payload.use)
    except hcp.HcpError as exc:
        raise ProblemError(exc.status, "Not changed",
                           type_=TYPE_VALIDATION if exc.status == 422 else TYPE_FORBIDDEN,
                           detail=exc.detail) from None
    return session_memory_payload(person, session)


@router.post("/me/sessions/{session_id}/agent-grants/", response=SessionMemoryOut,
             summary="Grant this session's agent access to what it learns about you")
def grant_my_session_agent(request: HttpRequest, session_id: str, payload: AgentGrantIn) -> dict:
    """Your act (HCP 4.1.6): give the agent of this session of yours `record`
    (it may save what it learns about you) and/or `use` (it may be told what has
    been learned), for this session only (`duration=session`, lapses when the
    session ends or after `session_grant_hours`) or always for that agent
    (`duration=always`, until you revoke it on /people/me). Only features you
    made available can be granted. Granting one that is off by default also turns
    it on for this session. Only you can do this."""
    person = _the_person_themself(request)
    session = _my_session(request, session_id)
    agent = getattr(session, "agent", None)
    if agent is None:
        raise _bad("this session has no agent to grant")
    try:
        hcp.issue_agent_grant(person, agent=agent, features=payload.features,
                              duration=payload.duration, actor=hcp.user_actor(request.user),
                              session=session, surface=payload.surface)
    except hcp.HcpError as exc:
        raise ProblemError(exc.status, "Not granted",
                           type_=TYPE_VALIDATION if exc.status == 422 else TYPE_FORBIDDEN,
                           detail=exc.detail) from None
    return session_memory_payload(person, session)


@router.get("/coverage/", response=PeopleCoverageOut,
            summary="Is the people brain working? Per-agent coverage")
def people_coverage(request: HttpRequest, workspace: str | None = None, days: int = 7) -> dict:
    """Per agent of the workspace, over the last `days` (1–90, default 7): the
    turns humans started with it, how many of those were handed what canopy
    knows about the person, and the facts it recorded in-session — with an
    explicit `healthy` verdict and the rule behind it.

    Counts only: no person is named. Members of the workspace see every agent;
    an admin of one of its agents who is not a member sees only the agents they
    administer."""
    from apps.agents.models import Agent
    from apps.workspaces.models import Workspace

    from . import coverage

    slug = (workspace or "").strip() or getattr(request, "workspace_slug", None) or ""
    if not slug:
        raise _bad("say which workspace: ?workspace=<slug>")
    ws = Workspace.objects.filter(pk=slug).first()
    if ws is None:
        raise _not_found("Workspace not found")
    agents = Agent.objects.filter(workspace=ws)
    if not wsvc.is_member(request.user, slug):
        # Not a member: only the agents they hold the keys to, if any.
        mine = [a.pk for a in agents.select_related("workspace") if a.is_admin(request.user)]
        if not mine:
            raise _not_found("Workspace not found")
        agents = agents.filter(pk__in=mine)
    if not (coverage.MIN_DAYS <= days <= coverage.MAX_DAYS):
        raise _bad(f"days is {coverage.MIN_DAYS}–{coverage.MAX_DAYS}")
    return coverage.workspace_coverage(ws.pk, days=days, agents=agents)


@router.get("/lookup/", response=PersonRefOut, summary="Find a person by email")
def lookup_person(request: HttpRequest, email: str, workspace: str | None = None) -> dict:
    """The person an address names — only if a workspace you are a member of
    deals with them (`?workspace=` narrows to one), else 404. Creates nothing."""
    person = people.by_email(email)
    if person is None:
        raise _not_found()
    if workspace or getattr(request, "workspace_slug", None):
        slugs = {_workspace(request, workspace).pk}
    else:
        slugs = wsvc.request_workspace_slugs(request)
    if not any(people.known_in(person, s) for s in slugs):
        raise _not_found()
    return _ref(person)


@router.get("/{person_id}/", response=PersonOut, summary="A person, as one workspace knows them")
def get_person(request: HttpRequest, person_id: int, workspace: str | None = None) -> dict:
    """Live facts (corrections first) for `?workspace=`. Members
    of that workspace only, and the read is logged where the person can see it."""
    ws = _workspace(request, workspace)
    if people.agent_of_login(request.user) is not None:
        # HCP 3.1: an agent never bulk-reads a person. It recalls what is
        # relevant about the person it is SERVING, under that client's grant,
        # with hcp_searchPreferences — the caller-only rule, enforced here.
        raise ProblemError(403, "Agents recall through HCP", type_=TYPE_FORBIDDEN,
                           detail="use hcp_searchPreferences with the turn you are serving")
    person = _person_in(person_id, ws)
    people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=ws.pk,
                      reader_user=request.user, reader_agent=None)
    return {
        **_ref(person), "workspace": ws.pk,
        "facts": [people.fact_dict(f) for f in people.live_facts(person, ws.pk)],
        "see_all": people.SEE_ALL,
    }


@router.get("/{person_id}/projects/", response=PersonProjectsOut,
            summary="The projects a person takes part in, in one workspace")
def list_person_projects(request: HttpRequest, person_id: int, workspace: str | None = None) -> dict:
    """The projects of `?workspace=`'s agents this person is a participant of —
    most recently active first, archived included. Members of that workspace
    only, and the read is logged where the person can see it."""
    from apps.agents import participants

    ws = _workspace(request, workspace)
    person = _person_in(person_id, ws)
    people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=ws.pk,
                      reader_user=request.user, reader_agent=people.agent_of_login(request.user))
    rows = participants.projects_of(person, ws.pk)
    return {"person": person.pk, "workspace": ws.pk, "projects": [
        {"id": r.project_id, "ext_id": r.project.ext_id, "name": r.project.name,
         "agent": r.project.agent.slug, "status": r.project.status, "role": r.role,
         "source": r.source, "since": _iso(r.created_at)} for r in rows]}


@router.post("/{person_id}/facts/", response={201: PersonFactCreatedOut},
             summary="Remember something about a person")
def add_person_fact(request: HttpRequest, person_id: int, payload: PersonFactIn):
    """Append a work-context fact (`kind` is closed: anything outside role,
    project, instance, preference, correction, terminology is a 400). With
    `supersedes_id`, that fact (same person, same workspace) stops being live.
    Asserted by the calling agent when you are an agent's login, else by you."""
    from apps.agents.models import AgentProject
    from apps.harness.models import Turn

    ws = _workspace(request, payload.workspace)
    person = _person_in(person_id, ws)
    source_turn = None
    if payload.source_turn_id:
        try:
            tid = uuid.UUID(str(payload.source_turn_id))
        except ValueError:
            raise _bad("source_turn_id is not a turn id") from None
        source_turn = Turn.objects.filter(pk=tid).select_related("agent", "chat_session").first()
        tenant = None
        if source_turn is not None:
            tenant = (source_turn.agent.workspace_id if source_turn.agent_id else
                      source_turn.chat_session.workspace_id if source_turn.chat_session_id
                      else source_turn.workspace_id)
        if tenant != ws.pk:
            raise _bad("source_turn_id is not a turn in that workspace")
    project = None
    if payload.project_id is not None:
        project = AgentProject.objects.select_related("agent").filter(pk=payload.project_id).first()
        if project is None or project.agent.workspace_id != ws.pk:
            raise _bad("project_id is not a project in that workspace")
    if people.agent_of_login(request.user) is not None and not hcp.may_record(person, source_turn):
        raise ProblemError(403, "This person has not let agents learn about them",
                           type_=TYPE_FORBIDDEN, detail=hcp.RECORD_OFF)
    supersedes = None
    if payload.supersedes_id is not None:
        supersedes = PersonFact.objects.filter(pk=payload.supersedes_id).first()
        if supersedes is None:
            raise _bad("supersedes_id is not a fact")
    try:
        fact = people.record_fact(
            person=person, workspace=ws, kind=payload.kind, statement=payload.statement,
            basis=payload.basis, by_user=request.user,
            by_agent=people.agent_of_login(request.user), source_turn=source_turn,
            project=project, instance_ref=payload.instance_ref, supersedes=supersedes,
            category=payload.category, dimension=payload.dimension, confidence=payload.confidence)
    except hcp.ZdrRefused as exc:
        raise ProblemError(403, "Not written: zero data retention", type_=TYPE_FORBIDDEN,
                           detail=str(exc)) from None
    except people.FactError as exc:
        raise _bad(str(exc)) from None
    return 201, {**people.fact_dict(fact), "supersedes_id": fact.supersedes_id}


@router.post("/{person_id}/facts/{fact_id}/retract/", response=PersonFactCreatedOut,
             summary="Take back a fact")
def retract_person_fact(request: HttpRequest, person_id: int, fact_id: int) -> dict:
    """The person themself, an admin of the fact's workspace, or whoever asserted
    it (the user, or the asserting agent's login). Anyone else: 404."""
    fact = (PersonFact.objects.select_related("person", "asserted_by_agent", "project")
            .filter(pk=fact_id, person_id=person_id).first())
    if fact is None or not people.may_retract(request.user, fact):
        raise _not_found("Fact not found")
    people.retract(fact, by=request.user)
    return {**people.fact_dict(fact), "supersedes_id": fact.supersedes_id}


@router.get("/{person_id}/conversations/", response=PersonConversationsOut,
            summary="The conversations a person had with one agent")
def list_person_conversations(request: HttpRequest, person_id: int, agent: str,
                              since: str | None = None) -> dict:
    """The turns this person started WITH `agent` (directly or in its chats),
    newest first: their message (≤ 4000 chars) and the result note.

    Only for that agent's OWN login, or an admin of that agent — "an agent may
    recall conversations it was party to". It widens no other visibility: not
    another agent, not a workspace member, and an admin who is not the agent's
    login sees only the turns the turn ACL already lets them read."""
    from apps.agents.models import Agent

    target = Agent.objects.filter(slug=agent).select_related("workspace").first()
    if target is None or not target.workspace_id or not wsvc.is_member(request.user, target.workspace_id):
        raise _not_found("Agent not found")
    own_login = target.user_id is not None and target.user_id == request.user.pk
    if not (own_login or target.is_admin(request.user)):
        raise ProblemError(403, "Only this agent's own login, or its admins, may read "
                                "its conversations", type_=TYPE_FORBIDDEN)
    person = _person_in(person_id, target.workspace)
    since_dt: dt.datetime | None = None
    if since:
        # A `+hh:mm` offset arrives as a space when the caller did not encode it.
        since_dt = parse_datetime(since.strip().replace(" ", "+"))
        if since_dt is None:
            raise _bad("since is not an ISO-8601 datetime")
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=dt.timezone.utc)
    rows = list(people.conversations(person, target, since=since_dt))
    if not own_login:
        # An admin who is not the agent itself gets no NEW read: only the turns
        # the turn ACL already lets them open (a private chat stays private).
        # The agent's own login is the one grant this route adds (Q1).
        from apps.harness import turn_access

        memo: dict = {}
        rows = [t for t in rows if turn_access.can_read_turn_content(request.user, t, memo)]
    people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=target.workspace_id,
                      reader_user=request.user, reader_agent=target if own_login else None)
    return {"person": person.pk, "agent": target.slug,
            "conversations": [people.conversation_dict(t) for t in rows]}

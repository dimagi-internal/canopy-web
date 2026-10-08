"""/api/people — what agents know about a person, and the person's own view of it.

The fleet brain v1 (canopy#804; design: hal `docs/proposals/2026-10-07-caller-
context-brain.md`). The rules, all applied in `apps/contacts/people.py`:

* **Per workspace.** A fact or digest is read and written inside the workspace it
  belongs to, by a member of it (an agent's login is a member). A person a
  workspace does not deal with (`people.known_in`) is "not found" there, so a
  guessed id or address confirms nothing across tenants.
* **The subject sees everything.** `GET /me/` returns every live fact about the
  caller in every workspace, every digest, and the last 50 reads of them.
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

from . import people
from . import services as contact_services
from .models import Person, PersonAccess, PersonDigest, PersonFact
from .people_schemas import (
    PersonConversationsOut,
    PersonDigestIn,
    PersonDigestOut,
    PersonFactCreatedOut,
    PersonFactIn,
    PersonMeOut,
    PersonOut,
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
    (corrections first), every digest, and the last 50 times an agent or a
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
    digests = [
        {"workspace": d.workspace_id, "text": d.text, "updated_at": _iso(d.updated_at),
         "updated_by": d.updated_by_agent.slug if d.updated_by_agent_id else
         (d.updated_by_user.email if d.updated_by_user_id else "")}
        for d in PersonDigest.objects.filter(person=person)
        .select_related("updated_by_agent", "updated_by_user").order_by("workspace_id")
    ]
    accesses = [
        {"created_at": a.created_at.isoformat(), "via": a.via, "workspace": a.workspace_id,
         "reader_agent": a.reader_agent.slug if a.reader_agent_id else None,
         "reader_user": a.reader_user.email if a.reader_user_id else None,
         "turn_id": str(a.turn_id) if a.turn_id else None}
        for a in PersonAccess.objects.filter(person=person)
        .select_related("reader_agent", "reader_user").order_by("-created_at", "-pk")[:ME_ACCESSES]
    ]
    return {**_ref(person), "facts": facts, "digests": digests, "accesses": accesses}


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
    """Live facts (corrections first) and the digest for `?workspace=`. Members
    of that workspace only, and the read is logged where the person can see it."""
    ws = _workspace(request, workspace)
    person = _person_in(person_id, ws)
    digest = people.digest_for(person, ws.pk)
    people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=ws.pk,
                      reader_user=request.user, reader_agent=people.agent_of_login(request.user))
    return {
        **_ref(person), "workspace": ws.pk,
        "digest": digest.text if digest else "",
        "digest_updated_at": _iso(digest.updated_at) if digest else None,
        "facts": [people.fact_dict(f) for f in people.live_facts(person, ws.pk)],
        "see_all": people.SEE_ALL,
    }


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
            project=project, instance_ref=payload.instance_ref, supersedes=supersedes)
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


@router.put("/{person_id}/digest/", response=PersonDigestOut, summary="Replace a person's digest")
def put_person_digest(request: HttpRequest, person_id: int, payload: PersonDigestIn) -> dict:
    """The short brief agents read about this person in this workspace — a cache,
    written by the `people_digest` turn. Members of the workspace (in practice an
    agent's login)."""
    ws = _workspace(request, payload.workspace)
    person = _person_in(person_id, ws)
    agent = people.agent_of_login(request.user)
    try:
        d = people.put_digest(person=person, workspace=ws, text=payload.text,
                              source_turn_ids=payload.source_turn_ids,
                              by_user=request.user, by_agent=agent)
    except people.FactError as exc:
        raise _bad(str(exc)) from None
    return {"workspace": ws.pk, "text": d.text, "updated_at": _iso(d.updated_at),
            "updated_by": agent.slug if agent is not None else request.user.email}


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

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

from apps.agents import access as agent_access
from apps.api.auth import session_auth
from apps.api.errors import TYPE_FORBIDDEN, TYPE_NOT_FOUND, TYPE_VALIDATION, ProblemError
from apps.api.route_gates import gate
from apps.workspaces import services as wsvc
from apps.tokens import kinds

from . import hcp, people
from . import services as contact_services
from .models import MemoryCollection, Person, PersonAccess, PersonAddress, PersonFact, PersonGrant
from .people_schemas import (
    AgentGrantIn,
    AgentMemoryIn,
    AgentMemoryOut,
    GrantCollectionIn,
    MemoryCollectionIn,
    MemoryCollectionOut,
    MemoryCollectionsOut,
    MemoryCollectionUpdateIn,
    MemoryPageAuditOut,
    MemoryPageOut,
    MemoryViewerIn,
    MemoryViewersOut,
    PeopleCoverageOut,
    PersonAddressesOut,
    PersonAddressIn,
    PersonAddressOut,
    PersonConversationsOut,
    PersonFactCreatedOut,
    PersonFactIn,
    PersonMeOut,
    PersonOut,
    PersonProjectsOut,
    PersonRefOut,
    SearchHistoryOut,
    SearchPreviewIn,
    SearchPreviewOut,
    SessionMemoryIn,
    SessionMemoryOut,
    SharedPagesOut,
    WorkspaceAddressesOut,
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


@router.get("/me/", response=PersonMeOut, summary="Your memory")
# Every per-person route is ALSO gated on `people.known_in` — a person the
# workspace does not deal with is 404, so ids and addresses confirm nothing.
# only ever the caller's own Person
@gate("self")
def people_me(request: HttpRequest) -> dict:
    """Everything canopy holds about YOU: every live fact in every workspace
    (corrections first) and the last 50 times an agent or a
    person read it. Any signed-in user; only ever your own."""
    person = contact_services.person_for(user=request.user)
    if person is None:
        raise _not_found()
    return _me_payload(person)


def _uses(person: Person) -> dict:
    """entry uuid → how often agents were told it, over all time. The page used to
    count its first 50 audit rows, which said "used 9×" of an entry told in 84
    conversations."""
    from django.db.models import Count, Max, Q

    from .models import PersonAuditEvent

    reads = (PersonAuditEvent.objects
             .filter(person=person, event_type="preference.read", actor_type="agent", entry_id__isnull=False)
             .order_by())  # no default ordering in the GROUP BY
    out: dict = {}
    # A read outside any turn (an API look-up) counts as its own conversation.
    for row in reads.values("entry_id").annotate(n=Count("turn", distinct=True),
                                                 loose=Count("pk", filter=Q(turn__isnull=True)),
                                                 last=Max("timestamp")):
        out[row["entry_id"]] = {"conversations": row["n"] + row["loose"],
                                "last_at": row["last"].isoformat(), "by": []}
    for row in reads.values("entry_id", "actor_id").distinct():
        # "agent:ace" or "agent:ace/model:x" → "ace" (words.ts `actorSlug`).
        slug = row["actor_id"].removeprefix("agent:").split("/")[0]
        if slug and slug not in out[row["entry_id"]]["by"]:
            out[row["entry_id"]]["by"].append(slug)
    return out


def _me_payload(person: Person) -> dict:
    facts = []
    places: dict[str, dict] = {}
    uses = _uses(person)
    for f in people.live_facts(person).select_related("asserted_by_agent", "asserted_by_user"):
        d = people.fact_dict(f)
        d["used"] = uses.get(f.entry_id, {"conversations": 0, "last_at": None, "by": []})
        d["workspace"] = f.workspace_id
        if f.workspace_id:
            if f.workspace_id not in places:
                org = hcp.root_of(f.workspace_id)
                places[f.workspace_id] = {"workspace_name": hcp._display_name(f.workspace_id),
                                          "org": org, "org_name": hcp._display_name(org)}
            d.update(places[f.workspace_id])
        d["asserted_by"] = (f.asserted_by_agent.slug if f.asserted_by_agent_id
                            else (f.asserted_by_user.email if f.asserted_by_user_id else ""))
        d["source_turn_id"] = str(f.source_turn_id) if f.source_turn_id else None
        d["learned_by"] = (f.asserted_by_agent.name or f.asserted_by_agent.slug
                           if f.asserted_by_agent_id else "")
        d["provenance_site"] = dict(f.provenance_site or {})
        facts.append(d)
    accesses = [
        {"created_at": a.created_at.isoformat(), "via": a.via, "workspace": a.workspace_id,
         "reader_agent": a.reader_agent.slug if a.reader_agent_id else None,
         "reader_user": a.reader_user.email if a.reader_user_id else None,
         "turn_id": str(a.turn_id) if a.turn_id else None}
        for a in PersonAccess.objects.filter(person=person)
        .select_related("reader_agent", "reader_user").order_by("-created_at", "-pk")[:ME_ACCESSES]
    ]
    addresses = [people.address_dict(a) for a in people.addresses_of(person)]
    return {**_ref(person), **_memory(person), "facts": facts, "addresses": addresses,
            "accesses": accesses}


def _memory(person: Person) -> dict:
    """Outside the rollout every switch reads off (what is stored is kept, and
    comes back if memory is turned on for them)."""
    enabled = hcp.rollout_allows(person)
    return {"memory_enabled": enabled,
            "memory_paused": not enabled and hcp.has_memory(person),
            "agent_memory": {
        f: {"available": enabled and bool(getattr(person, f"hcp_{f}_available")),
            "default": bool(getattr(person, f"hcp_{f}_default")),
            "changed_at": _iso(getattr(person, f"hcp_{f}_changed_at"))}
        for f in hcp.FEATURES}}


def _the_person_themself(request: HttpRequest) -> Person:
    """Only the person — never an agent's login, a session acting for them (a caller
    token), or a system account."""
    user = request.user
    if (people.agent_of_login(user) is not None
            or kinds.speaks_for_a_turn(request)
            or not people.is_human_account(user)):
        raise ProblemError(403, "Only the person may change this", type_=TYPE_FORBIDDEN)
    person = contact_services.person_for(user=user)
    if person is None:
        raise _not_found()
    return person


@router.post("/me/addresses/", response={201: PersonAddressOut},
             summary="Add a way to reach you")
@gate("self", kind="person-data")  # only the person: never an agent's login, a caller token or a system account
def add_my_address(request: HttpRequest, payload: PersonAddressIn):
    """Add a labelled email or phone of yours (`kind` email | phone, `label`
    such as work or personal). Everyone who shares a workspace with you, and
    its agents, can read it. The same kind and value again relabels it."""
    person = _the_person_themself(request)
    try:
        row = people.add_address(person=person, kind=payload.kind, label=payload.label,
                                 value=payload.value)
    except people.AddressError as exc:
        raise _bad(str(exc)) from None
    return 201, people.address_dict(row)


@router.delete("/me/addresses/{address_id}/", response={204: None},
               summary="Remove a way to reach you")
@gate("self", kind="person-data")  # only the person; someone else's row is 404
def remove_my_address(request: HttpRequest, address_id: int):
    person = _the_person_themself(request)
    deleted, _ = PersonAddress.objects.filter(pk=address_id, person=person).delete()
    if not deleted:
        raise _not_found("Address not found")
    return 204, None


@router.put("/me/agent-memory/", response=AgentMemoryOut,
            summary="Choose what agent memory is available, and its defaults")
# The person's agent-memory switch: their own Person only, and refused to an agent's
# login, a caller token and a system account — nobody may flip it for them.
@gate("self", setting=True)
def set_my_agent_memory(request: HttpRequest, payload: AgentMemoryIn) -> dict:
    """Your agent memory at the canopy level. For each of `record` (agents may
    learn about me) and `use` (agents may use what they've learned): `available`
    — may it be on at all — and `default` — is it on in a new session. A session
    can turn either on or off for itself, but never turn on what is not
    available. Off deletes nothing. Only you can change these."""
    person = _the_person_themself(request)
    try:
        hcp.set_agent_memory(person, actor=hcp.user_actor(request.user),
                             record=payload.record.model_dump() if payload.record else None,
                             use=payload.use.model_dump() if payload.use else None)
    except hcp.HcpError as exc:
        raise ProblemError(exc.status, "Not changed",
                           type_=TYPE_VALIDATION if exc.status == 422 else TYPE_FORBIDDEN,
                           detail=exc.detail) from None
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
                              "expires_at": _iso(g.expires_at),
                              "reach": hcp.reach_label(hcp.grant_sources(g))}
                             if g is not None else None)
    return {"session_id": str(session.pk), **state,
            "agent": ({"slug": agent.slug, "name": agent.name or agent.slug}
                      if agent is not None else None),
            "categories": list(hcp.HELD_CATEGORIES),
            # 4.1.5.1: what a grant here would reach (derived, never chosen in the prompt),
            # and the organisation it serves, for the prompt's words.
            "reach": (hcp.reach_label(hcp.default_reach(person, agent)[0])
                      if agent is not None else ""),
            "org": (hcp.org_name(agent) if agent is not None else ""),
            "session_grant_hours": int(hcp.SESSION_GRANT_CAP.total_seconds() // 3600),
            "memory_enabled": hcp.rollout_allows(person)}


@router.get("/me/sessions/{session_id}/agent-memory/", response=SessionMemoryOut,
            summary="Agent memory in one of your sessions")
@gate("self")  # One of the caller's OWN sessions (`created_by`), as that person only — same refusals.
def get_my_session_memory(request: HttpRequest, session_id: str) -> dict:
    """What applies in this session of yours: per feature, whether it is
    available, its default, this session's choice, and the effective value."""
    person = _the_person_themself(request)
    return session_memory_payload(person, _my_session(request, session_id))


@router.put("/me/sessions/{session_id}/agent-memory/", response=SessionMemoryOut,
            summary="Turn agent memory on or off for one of your sessions")
@gate("self", setting=True)
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
@gate("self", setting=True)  # The person granting their session's agent (HCP 4.1.6) — only the person, never an agent.
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
    if payload.surface not in ("chat", "widget"):
        # `embed-trusted` names a contact's act in a site's frame; a signed-in
        # person never performs that one here.
        raise _bad("surface is chat or widget")
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
@gate("member", "agent-admin")  # counts only; a non-member agent admin sees just their agents
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
        rows = list(agents)
        standings = agent_access.Standings([request.user], rows)
        mine = [a.pk for a in rows if standings.is_admin(request.user, a)]
        if not mine:
            raise _not_found("Workspace not found")
        agents = agents.filter(pk__in=mine)
    if not (coverage.MIN_DAYS <= days <= coverage.MAX_DAYS):
        raise _bad(f"days is {coverage.MIN_DAYS}–{coverage.MAX_DAYS}")
    return coverage.workspace_coverage(ws.pk, days=days, agents=agents)


@router.get("/addresses/", response=WorkspaceAddressesOut,
            summary="Every member's other addresses, as one workspace sees them")
@gate("member")  # every human member of `?workspace=`; each person whose addresses are returned gets a logged read
def list_workspace_addresses(request: HttpRequest, workspace: str | None = None) -> dict:
    """Every person in `?workspace=` (its human members, not agents or system
    accounts) with their labelled emails and phones, and `may_edit` — true
    only on your own row: a person changes their own contact details, at
    `POST /api/people/me/addresses/`. Members of that workspace only."""
    ws = _workspace(request, workspace)
    reader_agent = people.agent_of_login(request.user)
    users = [m.user for m in wsvc.effective_memberships(ws) if people.is_human_account(m.user)]
    persons = {u.pk: contact_services.person_for(user=u) for u in users}
    held: dict[int, list] = {}
    for row in PersonAddress.objects.filter(person__in=[p for p in persons.values() if p]):
        held.setdefault(row.person_id, []).append(row)
    out = []
    for u in sorted(users, key=lambda u: u.email.lower()):
        person = persons.get(u.pk)
        if person is None:
            continue
        rows = held.get(person.pk, [])
        if rows:
            people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=ws.pk,
                              reader_user=request.user, reader_agent=reader_agent)
        out.append({"person_id": person.pk, "user_id": u.pk, "email": u.email,
                    "display_name": people.display_name(person),
                    "may_edit": people.may_edit_addresses(request.user, person),
                    "addresses": [people.address_dict(a) for a in rows]})
    return {"workspace": ws.pk, "people": out}


@router.get("/lookup/", response=PersonRefOut, summary="Find a person by email")
@gate("member")  # 404 unless known in one of the caller's workspaces
def lookup_person(request: HttpRequest, email: str, workspace: str | None = None) -> dict:
    """The person an address names — only if a workspace you are a member of
    deals with them (`?workspace=` narrows to one), else 404. Creates nothing."""
    person = people.by_email(email)
    if person is None:
        raise _not_found()
    if workspace or getattr(request, "workspace_slug", None):
        slugs = {_workspace(request, workspace).pk}
    else:
        # One person by address, not a list: membership anywhere answers it.
        slugs = wsvc.request_member_slugs(request)
    if not any(people.known_in(person, s) for s in slugs):
        raise _not_found()
    return _ref(person)


@router.get("/{person_id}/", response=PersonOut, summary="A person, as one workspace knows them")
@gate("member")  # read logged as PersonAccess(via=api)
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
@gate("member")  # same gate as get_person (known_in); read logged
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
         "agent": (r.project.agent_slug or ""), "status": r.project.status, "role": r.role,
         "source": r.source, "since": _iso(r.created_at)} for r in rows]}


@router.get("/{person_id}/addresses/", response=PersonAddressesOut,
            summary="Every other way to reach a person")
@gate("member")  # same gate as get_person (known_in), agents included; read logged
def list_person_addresses(request: HttpRequest, person_id: int, workspace: str | None = None) -> dict:
    """The person's labelled emails and phones (work, personal …), beyond
    the address that identifies them. Anyone who shares `?workspace=` with
    them, an agent's login included — the person declared these, an agent did
    not learn them — and the read is logged where the person can see it."""
    ws = _workspace(request, workspace)
    person = _person_in(person_id, ws)
    people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=ws.pk,
                      reader_user=request.user, reader_agent=people.agent_of_login(request.user))
    return {"person": person.pk, "workspace": ws.pk,
            "addresses": [people.address_dict(a) for a in people.addresses_of(person)]}


@router.post("/{person_id}/facts/", response={201: PersonFactCreatedOut},
             summary="Remember something about a person")
@gate(
    "member",
    kind="person-data",
    viewer_write=(
        "an agent's login is a member, and that is who writes facts (contract canopy#804); a fact is append-only, "
        "attributed, visible to its subject and retractable by them"
    ),
)
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
        if project is None or project.workspace_id != ws.pk:
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
    except hcp.SensitiveRefused as exc:
        raise ProblemError(403, "Not written: zero data retention", type_=TYPE_FORBIDDEN,
                           detail=str(exc)) from None
    except people.FactError as exc:
        raise _bad(str(exc)) from None
    return 201, {**people.fact_dict(fact), "supersedes_id": fact.supersedes_id}


@router.post("/{person_id}/facts/{fact_id}/retract/", response=PersonFactCreatedOut,
             summary="Take back a fact")
@gate("self", "members.manage", kind="person-data")  # the subject, the asserter, or a workspace admin
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
@gate("agent-admin")  # or the agent's OWN login; an admin gets only turns turn_access already shows them
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
    own_login = target.is_own_login(request.user)
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


# --- memory collections ("More settings" on /people/me) ----------------------------


def _collection_error(exc: Exception) -> ProblemError:
    from . import hcp_collections

    if isinstance(exc, hcp_collections.NeedsConfirm):
        return ProblemError(409, "Confirm the widening", type_=TYPE_VALIDATION, detail=str(exc),
                            extras={"confirm_widening": [hcp.entry_urn(g.grant_id) for g in exc.grants],
                                   "agents": sorted({g.client_name for g in exc.grants}),
                                   "adds": hcp.reach_label(exc.added)})
    if isinstance(exc, hcp_collections.InUse):
        return ProblemError(409, "Still in use", type_=TYPE_VALIDATION, detail=str(exc))
    if isinstance(exc, hcp.HcpError):
        return ProblemError(exc.status, "Not changed",
                            type_=TYPE_VALIDATION if exc.status == 422 else TYPE_FORBIDDEN,
                            detail=exc.detail)
    raise exc


def _my_collection(person: Person, collection_id: int) -> MemoryCollection:
    c = MemoryCollection.objects.filter(pk=collection_id, person=person).first()
    if c is None:
        raise _not_found("Collection not found")
    return c


def _collections(person: Person) -> dict:
    from . import hcp_collections

    return {"collections": [hcp_collections.collection_dict(c)
                            for c in MemoryCollection.objects.filter(person=person)],
            "contexts": [{"source": s, "label": hcp_collections.context_label(s),
                          "group": hcp_collections.context_group(s)}
                         for s in hcp_collections.contexts(person)]}


@router.get("/me/memory-collections/", response=MemoryCollectionsOut,
            summary="Your memory collections")
# The person's own memory collections, and which one an agent they allowed uses
# (HCP 4.1.5.1) — only the person; a widening also needs their explicit confirm.
@gate("self")
def list_my_memory_collections(request: HttpRequest) -> dict:
    """The collections you made — named sets of contexts (all of an organisation,
    one workspace, entries in no workspace) you can point an agent at, resolved
    when read — which agents use each, and the contexts you can choose from.
    Only you see these."""
    return _collections(_the_person_themself(request))


@router.post("/me/memory-search-preview/", response=SearchPreviewOut,
             summary="Try a search over your memory, and see why each entry would come back")
# Trying a search over your own memory (scores only, records nothing) — only
# the person, and only inside the memory rollout.
# POST for the question in a body; writes nothing
@gate("self", kind="action")
def preview_my_memory_search(request: HttpRequest, payload: SearchPreviewIn) -> dict:
    """Scores every entry you hold against a question the way an agent's search
    does (`hcp.explain`): shared words, closeness in meaning, and the reason each
    would or would not be returned. `floor` previews another cutoff; agents keep
    using HCP_SEMANTIC_FLOOR until it is changed in the deploy. Covers all your
    entries — an agent only reaches what you let it. Changes nothing and is not
    an agent read, so it is not in your audit; the question is embedded like a
    search's."""
    from django.conf import settings

    from . import embeddings

    person = _the_person_themself(request)
    if not hcp.rollout_allows(person):
        raise ProblemError(403, hcp.NOT_ROLLED_OUT, type_=TYPE_FORBIDDEN)
    query = " ".join(payload.query.split())[:500]
    if not query:
        raise ProblemError(422, "Type a question", type_=TYPE_VALIDATION)
    facts = list(hcp.servable(PersonFact.objects.filter(person=person))
                 .select_related("project", "person", "workspace"))
    returned, scores, stats = hcp.explain(facts, query, limit=payload.limit, floor=payload.floor)
    return {
        "method": hcp.minimization_method(), "model": embeddings.model(),
        "floor": stats["floor"], "live_floor": settings.HCP_SEMANTIC_FLOOR,
        "weight": settings.HCP_SEMANTIC_WEIGHT, "project_boost": settings.HCP_PROJECT_BOOST,
        "limit": payload.limit, "embed_ms": stats["embed_ms"],
        "entries": _with_text(person, hcp.score_rows(returned, scores)),
    }


def _with_text(person: Person, rows: list[dict]) -> list[dict]:
    """Each scored row with what its entry says NOW (a record keeps only ids):
    the current version's text, kind and project; `statement` None once the
    entry is deleted."""
    ids = [r["entry_id"] for r in rows if r.get("entry_id")]
    now = {str(f.entry_id): f for f in PersonFact.objects.filter(
        person=person, entry_id__in=ids, superseded_at__isnull=True).select_related("project")}
    out = []
    for r in rows:
        f = now.get(r.get("entry_id") or "")
        gone = f is None or f.status == PersonFact.DELETED
        out.append({"meaning": None, "words": [], "on_project": False, "total": 0.0, "because": "", **r,
                    "statement": None if gone else f.statement, "kind": "" if gone else f.kind,
                    "project": None if gone or not f.project_id else f.project.name})
    return out


@router.get("/me/memory-searches/", response=SearchHistoryOut,
            summary="Every search agents ran over your memory, as it ran")
@gate("self")  # Every search agents ran over your memory, as it ran — only the person.
def list_my_memory_searches(request: HttpRequest, before: str | None = None, limit: int = 30) -> dict:
    """Newest first: who searched, for which turn (with a link to its session),
    the question, and every entry it scored — what came back, in what place,
    and why; what was left out and why not — with the settings at that moment.
    Searches from before scores were kept (`scored` false) show what came back
    only. Entry text is what the entry says now."""
    return _searches(_the_person_themself(request), before, limit)


def _searches(person: Person, before: str | None, limit: int) -> dict:
    from .models import PersonSearch

    qs = PersonSearch.objects.filter(person=person).select_related(
        "turn__chat_session", "turn__agent")
    if before:
        at = parse_datetime(before)
        if at is None:
            raise _bad(f"before is not a timestamp: {before}")
        qs = qs.filter(created_at__lt=at)
    limit = max(1, min(limit, 100))
    page = list(qs[:limit + 1])
    rows = []
    for s in page[:limit]:
        sess = s.turn.chat_session if s.turn_id and s.turn.chat_session_id else None
        rows.append({
            "retrieval_id": str(s.retrieval_id), "at": s.created_at, "actor_id": s.actor_id,
            "actor_type": s.actor_type, "agent": s.turn.agent.name if s.turn_id and s.turn.agent_id else None,
            "purpose": s.purpose, "query": s.query, "method": s.method, "model": s.model,
            "floor": s.floor, "weight": s.weight, "project_boost": s.project_boost, "embed_ms": s.embed_ms,
            "scored": s.scored, "turn_id": str(s.turn_id) if s.turn_id else None,
            "session_href": f"/w/{sess.workspace_id}/chat/{sess.pk}" if sess and sess.workspace_id else None,
            "session_title": (sess.title or None) if sess else None,
            "entries": _with_text(person, s.entries or []),
        })
    return {"searches": rows, "total": qs.count() if not before else None,
            "next_before": page[limit - 1].created_at.isoformat() if len(page) > limit else None}


@router.post("/me/memory-collections/", response=MemoryCollectionOut,
             summary="Make a memory collection")
@gate("self", setting=True)
def create_my_memory_collection(request: HttpRequest, payload: MemoryCollectionIn) -> dict:
    from . import hcp_collections

    person = _the_person_themself(request)
    try:
        c = hcp_collections.create(person, name=payload.name, sources=payload.sources,
                                   actor=hcp.user_actor(request.user))
    except hcp.HcpError as exc:
        raise _collection_error(exc) from None
    return hcp_collections.collection_dict(c)


@router.put("/me/memory-collections/{collection_id}/", response=MemoryCollectionOut,
            summary="Change a memory collection")
@gate("self", setting=True)
def update_my_memory_collection(request: HttpRequest, collection_id: int,
                                payload: MemoryCollectionUpdateIn) -> dict:
    """Rename it or change what it holds. Every agent using it follows at once.
    Adding a context lets those agents reach more, so it is refused (409, naming
    them in `confirm_widening`) until you send it again with `confirm_widening`
    listing those grants. Removing one applies immediately."""
    from . import hcp_collections

    person = _the_person_themself(request)
    c = _my_collection(person, collection_id)
    try:
        c = hcp_collections.update(person, c, actor=hcp.user_actor(request.user),
                                   name=payload.name, sources=payload.sources,
                                   confirm_widening=payload.confirm_widening)
    except (hcp.HcpError, hcp_collections.NeedsConfirm) as exc:
        raise _collection_error(exc) from None
    return hcp_collections.collection_dict(c)


@router.delete("/me/memory-collections/{collection_id}/", response={204: None},
               summary="Remove a memory collection")
@gate("self", setting=True)
def delete_my_memory_collection(request: HttpRequest, collection_id: int):
    """Refused (409) while an agent still uses it — point it elsewhere first."""
    from . import hcp_collections

    person = _the_person_themself(request)
    c = _my_collection(person, collection_id)
    try:
        hcp_collections.delete(person, c, actor=hcp.user_actor(request.user))
    except hcp_collections.InUse as exc:
        raise _collection_error(exc) from None
    return 204, None


@router.put("/me/grants/{grant_id}/collection/", response=MemoryCollectionsOut,
            summary="Choose what an agent you allowed may reach")
@gate("self", setting=True)
def set_my_grant_collection(request: HttpRequest, grant_id: str, payload: GrantCollectionIn) -> dict:
    """Point the agent behind this grant at one of your collections, or back to
    its default (`collection_id: null` — its own organisation). If that lets it
    reach more, send `confirm_widening` listing the grants the 409 named."""
    from . import hcp_collections

    person = _the_person_themself(request)
    try:
        gid = hcp.parse_entry_id(grant_id)
    except (ValueError, hcp.HcpError):
        raise _not_found("Grant not found") from None
    grant = (PersonGrant.objects.select_related("agent", "collection")
             .filter(grant_id=gid, person=person).first())
    if grant is None:
        raise _not_found("Grant not found")
    c = _my_collection(person, payload.collection_id) if payload.collection_id is not None else None
    try:
        hcp_collections.use_for_grant(person, grant, c, actor=hcp.user_actor(request.user),
                                      confirm_widening=payload.confirm_widening)
    except (hcp.HcpError, hcp_collections.NeedsConfirm) as exc:
        raise _collection_error(exc) from None
    return _collections(person)


# --- letting an agent see your memory page (apps/contacts/viewers.py) ---------------


def _pickable_agents(user):
    from apps.agents.models import Agent

    return Agent.objects.filter(workspace_id__in=wsvc.user_workspace_slugs(user)).order_by("name", "slug")


def _viewers(person: Person, user) -> dict:
    from . import viewers

    live = viewers.live(person)
    taken = {v.agent_id for v in live}
    return {"viewers": [{"agent": v.agent.slug, "name": v.agent.name or v.agent.slug,
                         "granted_at": v.granted_at, "last_viewed_at": v.last_viewed_at}
                        for v in live],
            "agents": [{"slug": a.slug, "name": a.name or a.slug}
                       for a in _pickable_agents(user) if a.pk not in taken]}


@router.get("/me/viewers/", response=MemoryViewersOut,
            summary="The agents you let see your memory page")
# Letting an agent see your memory page, read-only (apps/contacts/viewers.py):
# choosing who is the person's own act; the agent's own login then reads it.
@gate("self")
def list_my_viewers(request: HttpRequest) -> dict:
    """Which agents may see this page, read-only, and which you could let in
    (the agents of workspaces you belong to). Only you see this."""
    return _viewers(_the_person_themself(request), request.user)


@router.post("/me/viewers/", response=MemoryViewersOut,
             summary="Let an agent see your memory page, read-only")
@gate("self", setting=True)  # who may see your memory page
def add_my_viewer(request: HttpRequest, payload: MemoryViewerIn) -> dict:
    """Your act: the agent's own login may then read everything this page shows
    you — what is held, who may read it, what happened, how agents searched it —
    and change nothing. Each look goes in your audit. Only you can do this."""
    from . import viewers

    person = _the_person_themself(request)
    agent = _pickable_agents(request.user).filter(slug=payload.agent.strip()).first()
    if agent is None:
        raise _not_found("Agent not found")
    viewers.grant(person, agent, user=request.user)
    return _viewers(person, request.user)


@router.delete("/me/viewers/{agent_slug}/", response=MemoryViewersOut,
               summary="Stop an agent seeing your memory page")
@gate("self", setting=True)  # who may see your memory page
def remove_my_viewer(request: HttpRequest, agent_slug: str) -> dict:
    """At once: its next read is "not found". Only you can do this."""
    from apps.agents.models import Agent

    from . import viewers

    person = _the_person_themself(request)
    agent = Agent.objects.filter(slug=agent_slug).first()
    if agent is None or not viewers.revoke(person, agent, user=request.user):
        raise _not_found("That agent cannot see your page")
    return _viewers(person, request.user)


@router.get("/pages/shared-with-me/", response=SharedPagesOut,
            summary="The memory pages people let this agent see")
@gate("authenticated")  # empty unless the caller is an agent's own login
def list_shared_pages(request: HttpRequest) -> dict:
    """For an agent's own login: whose memory page it may read, and where.
    Anyone else gets an empty list."""
    from . import viewers

    agent = viewers.viewing_agent(request)
    if agent is None:
        return {"pages": []}
    return {"pages": [{"person_id": v.person_id, "display_name": people.display_name(v.person),
                       "granted_at": v.granted_at, "href": f"/people/{v.person_id}/page"}
                      for v in viewers.shared_with(agent)]}


def _page_of(request: HttpRequest, person_id: int) -> Person:
    from . import viewers

    person = viewers.page_of(request, person_id)
    if person is None:
        raise _not_found()
    return person


@router.get("/{person_id}/page/", response=MemoryPageOut,
            summary="A person's memory page, as they let this agent see it")
@gate("page-viewer")
def get_memory_page(request: HttpRequest, person_id: int) -> dict:
    """What /people/me shows the person — what is held, who may read it (their
    grants, their collections) — for an agent whose own login they let see it.
    Read-only. Anyone else: not found."""
    from .hcp_api import grants_of

    person = _page_of(request, person_id)
    return {"me": _me_payload(person), "grants": grants_of(person),
            "collections": _collections(person)}


@router.get("/{person_id}/page/audit/", response=MemoryPageAuditOut,
            summary="What happened to a person's memory, as they let this agent see it")
@gate("page-viewer")
def get_memory_page_audit(request: HttpRequest, person_id: int, cursor: str | None = None) -> dict:
    """Their audit log, newest first, 50 at a time — the page's "What happened"."""
    from .hcp_api import audit_page

    person = _page_of(request, person_id)
    try:
        return audit_page(person, cursor=cursor)
    except hcp.HcpError as exc:
        raise _bad(exc.detail) from None


@router.get("/{person_id}/page/searches/", response=SearchHistoryOut,
            summary="How agents searched a person's memory, as they let this agent see it")
@gate("page-viewer")
def get_memory_page_searches(request: HttpRequest, person_id: int, before: str | None = None,
                             limit: int = 30) -> dict:
    """The page's "How agents searched it"."""
    return _searches(_page_of(request, person_id), before, limit)

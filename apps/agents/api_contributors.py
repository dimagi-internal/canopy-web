"""/api/agents/{slug}/projects/{ref}/… — a project's team view, its ideas, and
its contributor pages (hal T69, T82).

Mounted beside the agents router. The rules live in `ideas.py` and
`contributors.py`; this file is the HTTP shape:

* `GET  …/team/` — the project as a teammate sees it: what it is, where it
  stands (built, in progress, decisions waiting on people), its ideas and its
  people. Members of the agent's workspace.
* `POST …/ideas/` — add your own idea (a `kind: "idea"` task). Any member.
* `GET  …/contributors/` — the ideas and the project's contributors.
* `GET  …/contributors/{person}/` — one contributor's page. `person` is an id or
  an email address. Members; the read is logged where the person can see it.
* `PUT  …/contributors/{person}/summary/` — an agent writes (or rewrites) the
  page, under the person's record switch and its grant. The person may too.
* `DELETE …/contributors/{person}/summary/` — the person, the agent's admins, or
  the agent that wrote it.
"""
from __future__ import annotations

from django.http import HttpRequest
from ninja import Router, Status
from ninja.errors import HttpError

from apps.api.auth import session_auth
from apps.api.route_gates import gate
from apps.contacts import hcp, people
from apps.contacts.models import Person, PersonAccess

from . import contributors as svc
from . import engagement
from . import ideas as idea_svc
from .gates import agent_for_read, project_or_404
from .models import ProjectParticipant
from .schemas import (
    ContributorPageOut,
    ContributorSummaryIn,
    IdeaCreatedOut,
    IdeaIn,
    ProjectIdeasOut,
    ProjectTeamOut,
)

router = Router(auth=session_auth, tags=["agents"])


def _person(project, ref: str):
    """The person `ref` names (an id or an address), if the agent's workspace deals
    with them — else 404, so a guess confirms nothing across tenants."""

    ref = (ref or "").strip()
    person = (Person.objects.select_related("user").filter(pk=int(ref)).first() if ref.isdigit()
              else svc.person_by_email(ref))
    if person is None or not people.known_in(person, project.workspace_id):
        raise HttpError(404, "person not found")
    return person


def _log_read(person, project, request: HttpRequest, reader: svc.Reader) -> None:

    people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=project.workspace_id,
                      reader_user=request.user, reader_agent=reader.agent)


@router.get("/{slug}/projects/{ref}/team/", response=ProjectTeamOut,
            summary="A project as a teammate sees it: what it is, where it stands, its ideas and people")
# hal T82: the team view, and adding your OWN idea (a kind=idea task with
# your words). Someone else's words go through create_tasks (agent.work).
@gate("member")
def project_team(request: HttpRequest, slug: str, ref: str) -> dict:
    """The team view's one read. What it is (outcome, links, owner); where it
    stands — ideas shipped and in progress, and the decisions waiting on people,
    one line each (open asks, tasks parked on a person, and task links marked
    `kind: "decision"`); every idea with its status and whose it is; and the
    people, each tallied over the same ideas. No task cards, numbers, rationale
    or notes. `may_edit` says the caller may use the work board."""
    agent = agent_for_read(request, slug)
    project = project_or_404(agent, ref)
    return idea_svc.team(project, request.user)


@router.post("/{slug}/projects/{ref}/ideas/", response={201: IdeaCreatedOut},
             summary="Add your own idea to a project, in your own words")
# an idea is a task with kind=idea
@gate(
    "member",
    kind="content",
    viewer_write=(
        "any member may put their OWN want on a project, in their own words, as themself — a suggested kind=idea task "
        "carrying only their quote, with no on_approve; it names nobody else, and recording someone else's words is "
        "create_tasks (agent.work)"
    ),
)
def create_idea(request: HttpRequest, slug: str, ref: str, payload: IdeaIn) -> Status:
    """Any member of the workspace. It becomes a task with `kind: "idea"` on the
    project, with your words as its human-why and you as its person; it tells
    the agent (one turn) and the project's owner, and `notified` says who was
    told. To record someone ELSE's words, create a task with `kind: "idea"` and
    `human_why` (`POST /tasks/`, the editor/agent tier)."""
    agent = agent_for_read(request, slug)
    project = project_or_404(agent, ref)
    if agent.is_own_login(request.user):
        raise HttpError(422, "an agent has no ideas of its own: record a person's words as a task "
                             "with kind=idea and human_why")
    try:
        task = idea_svc.add_own_idea(project, quote=payload.quote, title=payload.title, user=request.user)
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None
    told = idea_svc.notify(task, by_user=request.user)

    engagement.record("idea_added", workspace=project.workspace_id, surface=engagement.surface_of(request),
                      user=request.user, project=project, task=task)
    [out] = [i for i in idea_svc.of_project(project) if i["key"] == task.key]
    return Status(201, {"idea": out, "notified": told})


@router.get("/{slug}/projects/{ref}/contributors/", response=ProjectIdeasOut,
            summary="A project's ideas (where each stands, whose it is) and its contributors")
@gate("member")  # page content withheld from an agent without the person's `use`
def project_ideas(request: HttpRequest, slug: str, ref: str) -> dict:
    """The project's ideas: its top-level tasks with `kind: "idea"` — a
    person's want in their words, with the work toward it as subtasks. A task
    of kind `work` is the agent's own and never shows here. `stage` is the
    idea task's status in a contributor's words. Contributors are tallied over
    the same ideas; those with a page lead.

    An agent sees contributor-page content (goals, quotes) only for people who
    let agents use what they learned."""
    agent = agent_for_read(request, slug)
    project = project_or_404(agent, ref)
    return svc.board(project, svc.reader_of(request.user))


@router.get("/{slug}/projects/{ref}/contributors/{person}/", response=ContributorPageOut,
            summary="One contributor's page: their goal in their own words, and their ideas")
@gate("member")  # the person must be known_in the workspace; read logged
def contributor_page(request: HttpRequest, slug: str, ref: str, person: str) -> dict:
    """What they are trying to achieve, quoted with links to where they said it,
    and their ideas on this project as shipped / started / proposed (the
    project's ideas whose person they are), plus wants not yet recorded as ideas. Read it before building on — or against —
    their idea. `person` is an id or an email address."""
    agent = agent_for_read(request, slug)
    project = project_or_404(agent, ref)
    who = _person(project, person)
    reader = svc.reader_of(request.user)
    # Anyone the workspace deals with has a page, if only an empty one: it says
    # whether an agent may write it (and why not), which is what an agent asks first.
    out = svc.page(project, who, reader)
    _log_read(who, project, request, reader)
    return out


@router.put("/{slug}/projects/{ref}/contributors/{person}/summary/", response=ContributorPageOut,
            summary="Write a contributor's page (an agent, with their permission)")
# An agent's login (a member) under the PERSON's record switch + its grant, or the person.
@gate(
    "member",
    "self",
    kind="content",
    viewer_write=(
        "an agent's login is a member, and that is who writes a contributor page — only while the person lets agents "
        "learn about them and has granted that agent; otherwise only the person themself"
    ),
)
def put_contributor_summary(request: HttpRequest, slug: str, ref: str, person: str,
                            payload: ContributorSummaryIn) -> dict:
    """Replace the person's page on this project with the agent's reading of
    their own words. Every quote is verbatim and carries a `source_url`; each
    `ideas[].task` must be a task of this project; `open` holds wants no task
    covers yet. Allowed for an agent while the person lets agents learn about
    them and has granted it, or for the person themself — anyone else is 403.
    Makes them a participant (source `summary`) if they were not one."""
    agent = agent_for_read(request, slug)
    project = project_or_404(agent, ref)
    who = _person(project, person)
    reader = svc.reader_of(request.user)
    allowed, why_not = svc.may_write_summary(who, reader)
    if not allowed:
        raise HttpError(403, why_not)

    try:
        svc.write_summary(project, who, payload.model_dump(), by_agent=reader.agent)
    except hcp.SensitiveRefused as exc:
        raise HttpError(403, str(exc)) from None
    except svc.SummaryError as exc:
        raise HttpError(400, str(exc)) from None
    return svc.page(project, who, reader)


@router.delete("/{slug}/projects/{ref}/contributors/{person}/summary/", response={204: None},
               summary="Delete a contributor's page")
@gate("self", "agent-admin", kind="content")  # or the agent that wrote it
def delete_contributor_summary(request: HttpRequest, slug: str, ref: str, person: str):
    """The person themself, the agent's admins, or the agent that wrote it.
    Leaves them a participant; their ideas stay on the board."""
    agent = agent_for_read(request, slug)
    project = project_or_404(agent, ref)
    who = _person(project, person)
    row = (ProjectParticipant.objects.select_related("person", "project__agent", "summary_by_agent")
           .filter(project=project, person=who).first())
    if row is None or not row.summary:
        raise HttpError(404, "no page to delete")
    if not svc.may_delete_summary(row, request.user):
        raise HttpError(403, "only the person, the agent's admins, or the agent that wrote it")
    svc.clear_summary(row)
    return Status(204, None)

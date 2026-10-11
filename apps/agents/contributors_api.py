"""/api/agents/{slug}/projects/{ref}/contributors — ideas and contributor pages (hal T69).

Mounted beside the agents router. The rules live in `contributors.py`; this file
is the HTTP shape:

* `GET  …/contributors/` — every idea (task) with its stage and who it came from,
  and the project's contributors. Members of the agent's workspace.
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

from . import contributors as svc
from .api import _get_agent_or_404, _get_project_or_404
from .models import ProjectParticipant
from .schemas import ContributorPageOut, ContributorSummaryIn, ProjectIdeasOut

router = Router(auth=session_auth, tags=["agents"])


def _person(project, ref: str):
    """The person `ref` names (an id or an address), if the agent's workspace deals
    with them — else 404, so a guess confirms nothing across tenants."""
    from apps.contacts import people
    from apps.contacts.models import Person

    ref = (ref or "").strip()
    person = (Person.objects.select_related("user").filter(pk=int(ref)).first() if ref.isdigit()
              else svc.person_by_email(ref))
    if person is None or not people.known_in(person, project.agent.workspace_id):
        raise HttpError(404, "person not found")
    return person


def _log_read(person, project, request: HttpRequest, reader: svc.Reader) -> None:
    from apps.contacts import people
    from apps.contacts.models import PersonAccess

    people.log_access(person, via=PersonAccess.VIA_API, workspace_slug=project.agent.workspace_id,
                      reader_user=request.user, reader_agent=reader.agent)


@router.get("/{slug}/projects/{ref}/contributors/", response=ProjectIdeasOut,
            summary="A project's ideas (where each stands, whose it was) and its contributors")
def project_ideas(request: HttpRequest, slug: str, ref: str) -> dict:
    """Every task of the project is an idea. `stage` reads its status in a
    contributor's words: suggested → proposed, in_progress → started,
    done → shipped (declined stays declined). `came_from` lists who it came from,
    strongest evidence first: the task's human-why, the human whose turn raised
    it, then contributor pages that claim it. Contributors with a page lead.

    An agent sees contributor-page content (goals, quotes, the attributions that
    rest on a page) only for people who let agents use what they learned."""
    agent = _get_agent_or_404(request, slug)
    project = _get_project_or_404(agent, ref)
    return svc.board(project, svc.reader_of(request.user))


@router.get("/{slug}/projects/{ref}/contributors/{person}/", response=ContributorPageOut,
            summary="One contributor's page: their goal in their own words, and their ideas")
def contributor_page(request: HttpRequest, slug: str, ref: str, person: str) -> dict:
    """What they are trying to achieve, quoted with links to where they said it,
    and their ideas on this project as shipped / started / proposed, plus what
    they want that no task covers yet. Read it before building on — or against —
    their idea. `person` is an id or an email address."""
    agent = _get_agent_or_404(request, slug)
    project = _get_project_or_404(agent, ref)
    who = _person(project, person)
    reader = svc.reader_of(request.user)
    # Anyone the workspace deals with has a page, if only an empty one: it says
    # whether an agent may write it (and why not), which is what an agent asks first.
    out = svc.page(project, who, reader)
    _log_read(who, project, request, reader)
    return out


@router.put("/{slug}/projects/{ref}/contributors/{person}/summary/", response=ContributorPageOut,
            summary="Write a contributor's page (an agent, with their permission)")
def put_contributor_summary(request: HttpRequest, slug: str, ref: str, person: str,
                            payload: ContributorSummaryIn) -> dict:
    """Replace the person's page on this project with the agent's reading of
    their own words. Every quote is verbatim and carries a `source_url`; each
    `ideas[].task` must be a task of this project; `open` holds wants no task
    covers yet. Allowed for an agent while the person lets agents learn about
    them and has granted it, or for the person themself — anyone else is 403.
    Makes them a participant (source `summary`) if they were not one."""
    agent = _get_agent_or_404(request, slug)
    project = _get_project_or_404(agent, ref)
    who = _person(project, person)
    reader = svc.reader_of(request.user)
    allowed, why_not = svc.may_write_summary(who, reader)
    if not allowed:
        raise HttpError(403, why_not)
    from apps.contacts import hcp

    try:
        svc.write_summary(project, who, payload.model_dump(), by_agent=reader.agent)
    except hcp.ZdrRefused as exc:
        raise HttpError(403, str(exc)) from None
    except svc.SummaryError as exc:
        raise HttpError(400, str(exc)) from None
    return svc.page(project, who, reader)


@router.delete("/{slug}/projects/{ref}/contributors/{person}/summary/", response={204: None},
               summary="Delete a contributor's page")
def delete_contributor_summary(request: HttpRequest, slug: str, ref: str, person: str):
    """The person themself, the agent's admins, or the agent that wrote it.
    Leaves them a participant; their ideas stay on the board."""
    agent = _get_agent_or_404(request, slug)
    project = _get_project_or_404(agent, ref)
    who = _person(project, person)
    row = (ProjectParticipant.objects.select_related("person", "project__agent", "summary_by_agent")
           .filter(project=project, person=who).first())
    if row is None or not row.summary:
        raise HttpError(404, "no page to delete")
    if not svc.may_delete_summary(row, request.user):
        raise HttpError(403, "only the person, the agent's admins, or the agent that wrote it")
    svc.clear_summary(row)
    return Status(204, None)

"""Contributor pages on an agent's project (hal T69), and the board of ideas.

The Connect team builds Supply together with one shared agent. Two questions
have to have an answer on the page, not in someone's head:

* **Where does each idea stand, and whose was it?** An idea is a task with
  `kind="idea"` carrying the person's own words (`apps/agents/ideas.py`, hal
  T82), its work as subtasks. Ideas used to be read off EVERY task, which made
  the agent's own process work look like the product's ideas.
* **What is this person trying to achieve?** Their CONTRIBUTOR PAGE: an agent's
  summary of their own words, each quote linked to where they said it, beside
  their ideas. Read it before building on (or against) their idea.

The summary is judgment — which of someone's hundreds of prompts say what they
want — so an agent writes it and canopy only stores, validates and renders it.
It lives on `ProjectParticipant.summary` (per project: what someone wants from
Supply says nothing about anything else). Because it is something an agent
LEARNED about a person, the person's own switches govern it, exactly as they
govern facts (`apps/contacts/hcp/`):

* an agent WRITES it only while the person lets agents learn about them
  (record) and this agent holds their grant for it;
* an agent READS it only while they let agents use it (use) and it holds that
  grant — otherwise the summary is withheld from the agent (the ideas
  themselves, which people put on the project, stay readable);
* the person, and members of the workspace, see it; the person (or the agent's
  admins) may delete it, and turning a switch off deletes nothing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from apps.contacts import hcp, people
from apps.contacts.models import Person

from . import ideas as idea_svc
from . import participants
from .models import AgentProject, ProjectParticipant

logger = logging.getLogger(__name__)

PROPOSED, STARTED, SHIPPED, DECLINED = "proposed", "started", "shipped", "declined"


class SummaryError(ValueError):
    """A summary canopy will not store. The message is safe to show the caller."""


# --- who is reading / writing ----------------------------------------------------


@dataclass(frozen=True)
class Reader:
    """Who is asking, as far as a person's switches care."""

    user: object
    agent: object  # the Agent whose own login `user` is, or None

    @property
    def is_agent(self) -> bool:
        return self.agent is not None


def reader_of(user) -> Reader:

    return Reader(user=user, agent=people.agent_of_login(user))


def agent_may_use(person, agent) -> bool:
    """May `agent` read what agents learned about `person`? The switch AND the grant."""

    return hcp.may_use(person) and hcp.granted(person, agent)["use"]


def agent_may_record(person, agent) -> bool:

    return hcp.may_record(person) and hcp.granted(person, agent)["record"]


def may_see_summary(person, reader: Reader) -> bool:
    """A human member sees it; an agent only under the person's `use`."""
    if not reader.is_agent:
        return True
    return agent_may_use(person, reader.agent)


def may_write_summary(person, reader: Reader) -> tuple[bool, str]:
    """(allowed, why not). An agent under the person's `record`, or the person themself."""

    if reader.is_agent:
        if not hcp.may_record(person):
            return False, hcp.RECORD_OFF
        if not hcp.granted(person, reader.agent)["record"]:
            return False, hcp.NOT_GRANTED
        return True, ""
    if person.user_id and person.user_id == getattr(reader.user, "pk", None):
        return True, ""
    return False, "only an agent (with the person's permission) or the person themself writes this page"


def may_delete_summary(row: ProjectParticipant, user) -> bool:
    person = row.person
    if person.user_id and person.user_id == getattr(user, "pk", None):
        return True
    agent = row.project.agent
    if agent is not None and agent.is_admin(user):
        return True
    writer = row.summary_by_agent
    return writer is not None and writer.is_own_login(user)


# --- the summary itself ----------------------------------------------------------


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = parse_datetime(value)
        return parsed.isoformat() if parsed else None
    return value.isoformat()


def write_summary(project: AgentProject, person, payload: dict, *, by_agent=None) -> ProjectParticipant:
    """Store `payload` (an already-validated `ContributorSummaryIn` dump) as this
    person's page on `project`, making them a participant if they are not one.

    Refuses an `ideas[].task` that is not a task of this project: an idea that
    points at nothing would show as theirs on no board, and a typo there is
    silent. Refuses when the request comes from a zero-data-retention session.
    """

    # A summary re-reads many sessions; it is not one turn's learning, so it is not
    # REQUIRED to name a turn (unlike a fact). But when the request does come from
    # a turn, a sensitive one writes nothing about a person.
    hcp.refuse_if_sensitive()
    # A page claims IDEAS — the project's kind=idea tasks — never the agent's work.
    # Named by key or by an agent's alias; stored as the name it was given.
    task_ids = {}
    for ext_id, key in project.tasks.filter(kind="idea").values_list("ext_id", "key"):
        task_ids[key.lower()] = ext_id or key
        if ext_id:
            task_ids[ext_id.lower()] = ext_id
    ideas = []
    for idea in payload.get("ideas") or []:
        real = task_ids.get(str(idea.get("task", "")).strip().lower())
        if real is None:
            raise SummaryError(f"ideas: {idea.get('task')!r} is not an idea (a kind=idea task) "
                               f"of {project.address}")
        ideas.append({**idea, "task": real})
    summary = {
        "goal": payload["goal"],
        "quotes": payload.get("quotes") or [],
        "ideas": ideas,
        "open": payload.get("open") or [],
        "sources": payload.get("sources") or [],
        "basis": payload.get("basis") or "",
    }
    # JSON-safe: datetimes as ISO strings.
    for group in ("quotes", "ideas", "open"):
        for item in summary[group]:
            if "said_at" in item:
                item["said_at"] = _iso(item["said_at"])
    row = participants.ensure(project, person, source=ProjectParticipant.SUMMARY)
    row.summary = summary
    row.summary_written_at = timezone.now()
    row.summary_by_agent = by_agent
    row.save(update_fields=["summary", "summary_written_at", "summary_by_agent"])
    return row


def clear_summary(row: ProjectParticipant) -> None:
    row.summary = {}
    row.summary_written_at = None
    row.summary_by_agent = None
    row.save(update_fields=["summary", "summary_written_at", "summary_by_agent"])


# --- the board and a page, read from the project's ideas -------------------------------


def person_by_email(address: str):
    """The person an address names, without creating one: a correspondent, a
    verified login (`people.by_email`), else the account whose login address it
    is. Callers still gate on `people.known_in` — an address confirms nothing
    about a workspace that does not deal with them."""

    address = (address or "").strip()
    if "@" not in address:
        return None
    return (people.by_email(address)
            or Person.objects.select_related("user").filter(user__email__iexact=address).first())


def board(project: AgentProject, reader: Reader) -> dict:
    """The project's ideas (`ideas.of_project` — never its tasks) and its
    contributors, every tally a count over that one list."""

    ideas = idea_svc.of_project(project)
    return {"agent": project.agent_slug or "", "project": project.ext_id or project.handle, "name": project.name,
            "stages": idea_svc.counts(ideas), "ideas": ideas,
            "contributors": idea_svc.contributors(
                project, ideas, page_visible=lambda row: may_see_summary(row.person, reader))}


def page(project: AgentProject, person, reader: Reader) -> dict:
    """One contributor's page on `project`: their summary, and their ideas —
    the same list the board counts, filtered to them."""

    row = (ProjectParticipant.objects.filter(project=project, person=person)
           .select_related("summary_by_agent").first())
    visible = row is not None and bool(row.summary) and may_see_summary(person, reader)
    order = {SHIPPED: 0, STARTED: 1, PROPOSED: 2, DECLINED: 3}
    ideas = sorted(idea_svc.of_person(idea_svc.of_project(project), person.pk),
                   key=lambda i: order[i["stage"]])
    summary = None
    state = "not_written"
    if row is not None and row.summary:
        if visible:
            s = row.summary
            # A want the page lists as "still open" that is now an idea of theirs
            # is shown once, as the idea.
            said = {i["quote"].strip() for i in ideas}
            open_ = [o for o in (s.get("open") or []) if (o.get("quote") or "").strip() not in said]
            summary = {"goal": s.get("goal", ""), "quotes": s.get("quotes") or [],
                       "open": open_, "sources": s.get("sources") or [],
                       "basis": s.get("basis", ""), "written_at": row.summary_written_at,
                       "written_by": row.summary_by_agent.slug if row.summary_by_agent_id else ""}
            state = "written"
        else:
            state = "withheld"
    may_write, why_not = may_write_summary(person, reader)
    return {
        "person": {"id": person.pk, "display_name": people.display_name(person),
                   "email": people.email_of(person)},
        "agent": project.agent_slug or "", "project": project.ext_id or project.handle,
        "project_name": project.name,
        "role": row.role if row is not None else "",
        "participant_since": row.created_at if row is not None else None,
        "summary_state": state, "summary": summary,
        "ideas": ideas, "stages": idea_svc.counts(ideas),
        "may_write": may_write, "why_not": why_not,
    }

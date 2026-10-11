"""Ideas and contributor pages on an agent's project (hal T69).

The Connect team builds Supply together with one shared agent. Two questions
have to have an answer on the page, not in someone's head:

* **Where does each idea stand, and whose was it?** An idea IS a board task —
  there is no second tracker. Its stage is the task's status read in the words a
  contributor uses (`STAGE`), and "who it came from" is resolved from the
  evidence canopy holds, strongest first (`came_from`).
* **What is this person trying to achieve?** Their CONTRIBUTOR PAGE: an agent's
  summary of their own words, each quote linked to where they said it, beside
  what has been built and what is still open. Read it before building on (or
  against) their idea.

The summary is judgment — which of someone's hundreds of prompts say what they
want — so an agent writes it and canopy only stores, validates and renders it.
It lives on `ProjectParticipant.summary` (per project: what someone wants from
Supply says nothing about anything else). Because it is something an agent
LEARNED about a person, the person's own switches govern it, exactly as they
govern facts (`apps/contacts/hcp.py`):

* an agent WRITES it only while the person lets agents learn about them
  (record) and this agent holds their grant for it;
* an agent READS it only while they let agents use it (use) and it holds that
  grant — otherwise the summary, and every attribution that rests on it, is
  withheld from the agent (the board itself stays readable);
* the person, and members of the workspace, see it; the person (or the agent's
  admins) may delete it, and turning a switch off deletes nothing.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import AgentProject, AgentTask, ProjectParticipant

logger = logging.getLogger(__name__)

PROPOSED, STARTED, SHIPPED, DECLINED = "proposed", "started", "shipped", "declined"
#: A task's status, in a contributor's words. `declined` is shown, not hidden:
#: an idea that was turned down is still that person's idea, and seeing it is
#: how they learn it was considered.
STAGE = {
    AgentTask.SUGGESTED: PROPOSED,
    AgentTask.IN_PROGRESS: STARTED,
    AgentTask.DONE: SHIPPED,
    AgentTask.DECLINED: DECLINED,
}
STAGES = (PROPOSED, STARTED, SHIPPED, DECLINED)

#: Where an attribution came from, strongest first. `human_why` is the task's own
#: record of what a person said (T68); `raised_by` is the human whose turn raised
#: it; `contributor_page` is an agent's reading of the person's sessions.
VIA_HUMAN_WHY, VIA_RAISED_BY, VIA_PAGE = "human_why", "raised_by", "contributor_page"


def stage_of(task: AgentTask) -> str:
    return STAGE.get(task.status, PROPOSED)


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
    from apps.contacts import people

    return Reader(user=user, agent=people.agent_of_login(user))


def agent_may_use(person, agent) -> bool:
    """May `agent` read what agents learned about `person`? The switch AND the grant."""
    from apps.contacts import hcp

    return hcp.may_use(person) and hcp.granted(person, agent)["use"]


def agent_may_record(person, agent) -> bool:
    from apps.contacts import hcp

    return hcp.may_record(person) and hcp.granted(person, agent)["record"]


def may_see_summary(person, reader: Reader) -> bool:
    """A human member sees it; an agent only under the person's `use`."""
    if not reader.is_agent:
        return True
    return agent_may_use(person, reader.agent)


def may_write_summary(person, reader: Reader) -> tuple[bool, str]:
    """(allowed, why not). An agent under the person's `record`, or the person themself."""
    from apps.contacts import hcp

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
    if agent.is_admin(user):
        return True
    writer = row.summary_by_agent
    return writer is not None and writer.user_id is not None and writer.user_id == getattr(user, "pk", None)


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
    from apps.contacts import hcp

    from . import participants

    # A summary re-reads many sessions; it is not one turn's learning, so it is not
    # REQUIRED to name a turn (unlike a fact). But when the request does come from
    # a turn, a ZDR one writes nothing about a person.
    hcp.refuse_if_zdr()
    task_ids = {t.lower(): t for t in project.tasks.values_list("ext_id", flat=True)}
    ideas = []
    for idea in payload.get("ideas") or []:
        real = task_ids.get(str(idea.get("task", "")).strip().lower())
        if real is None:
            raise SummaryError(f"ideas: task {idea.get('task')!r} is not a task of {project.ext_id}")
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


# --- who an idea came from -------------------------------------------------------


def person_by_email(address: str):
    """The person an address names, without creating one: a correspondent, a
    verified login (`people.by_email`), else the account whose login address it
    is. Callers still gate on `people.known_in` — an address confirms nothing
    about a workspace that does not deal with them."""
    from apps.contacts import people
    from apps.contacts.models import Person

    address = (address or "").strip()
    if "@" not in address:
        return None
    return (people.by_email(address)
            or Person.objects.select_related("user").filter(user__email__iexact=address).first())



def _human_why(task: AgentTask) -> list[dict]:
    """T68's structured human-why on a task — `[{quote, who, source_url, said_at}]`
    — when the field exists. Read defensively: until T68 lands there is none."""
    value = getattr(task, "human_why", None) or []
    return [v for v in value if isinstance(v, dict)] if isinstance(value, list) else []


class _People:
    """Resolve and name people once per request."""

    def __init__(self, workspace_slug: str):
        self.ws = workspace_slug
        self._by_email: dict[str, object] = {}
        self._names: dict[int, str] = {}

    def by_email(self, address: str):
        from apps.contacts import people

        key = (address or "").strip().lower()
        if key not in self._by_email:
            person = person_by_email(key)
            if person is not None and not people.known_in(person, self.ws):
                person = None
            self._by_email[key] = person
        return self._by_email[key]

    def name(self, person) -> str:
        from apps.contacts import people

        if person.pk not in self._names:
            self._names[person.pk] = people.display_name(person)
        return self._names[person.pk]


def came_from(task: AgentTask, *, page_claims: list[dict], resolver: _People) -> list[dict]:
    """Who an idea came from, strongest evidence first, one entry per person.

    `page_claims` are the contributor-page entries naming this task, already
    filtered to what the reader may see.
    """
    from apps.contacts import people

    out: list[dict] = []
    seen: set = set()

    def add(person, name: str, via: str, quote: str = "", source_url: str = "", said_at=None):
        key = ("p", person.pk) if person is not None else ("n", name.strip().lower())
        if not name.strip() or key in seen:
            return
        seen.add(key)
        out.append({"person_id": person.pk if person is not None else None, "name": name,
                    "via": via, "quote": quote or "", "source_url": source_url or "",
                    "said_at": _iso(said_at)})

    for why in _human_why(task):
        who = str(why.get("who") or "").strip()
        person = resolver.by_email(who) if who else None
        add(person, resolver.name(person) if person is not None else who, VIA_HUMAN_WHY,
            str(why.get("quote") or ""), str(why.get("source_url") or ""), why.get("said_at"))
    if task.raised_by_id:
        person = people.initiator_person(task.raised_by)
        if person is not None:
            add(person, resolver.name(person), VIA_RAISED_BY,
                source_url=task.source_url or "")
    for claim in page_claims:
        person = claim["person"]
        add(person, resolver.name(person), VIA_PAGE, claim.get("quote", ""),
            claim.get("source_url", ""), claim.get("said_at"))
    return out


def _idea_dict(task: AgentTask, sources: list[dict]) -> dict:
    return {
        "ext_id": task.ext_id, "title": task.title, "status": task.status, "stage": stage_of(task),
        "next_action": task.next_action, "updated_at": task.updated_at,
        "links": [link for link in (task.links or []) if isinstance(link, dict)][:8],
        "came_from": sources,
    }


def _visible_rows(project: AgentProject, reader: Reader) -> list[tuple[ProjectParticipant, bool]]:
    """Every participant row with whether the reader may see its summary."""
    rows = list(ProjectParticipant.objects.filter(project=project)
                .select_related("person", "person__user", "summary_by_agent")
                .order_by("-created_at", "-pk"))
    return [(r, bool(r.summary) and may_see_summary(r.person, reader)) for r in rows]


def _claims(rows) -> dict[str, list[dict]]:
    """task ext_id (lowercased) → the visible contributor-page entries naming it."""
    claims: dict[str, list[dict]] = {}
    for row, visible in rows:
        if not visible:
            continue
        for idea in row.summary.get("ideas") or []:
            claims.setdefault(str(idea.get("task", "")).lower(), []).append({**idea, "person": row.person})
    return claims


def board(project: AgentProject, reader: Reader) -> dict:
    """The project's ideas (every task) with their stage and who they came from,
    and its contributors with how far their ideas have got."""
    resolver = _People(project.agent.workspace_id)
    rows = _visible_rows(project, reader)
    claims = _claims(rows)
    tasks = list(project.tasks.select_related("raised_by", "raised_by__initiator_user",
                                              "raised_by__initiator_contact")
                 .order_by("-updated_at", "id"))
    ideas = [_idea_dict(t, came_from(t, page_claims=claims.get(t.ext_id.lower(), []),
                                     resolver=resolver)) for t in tasks]
    counts = {s: 0 for s in STAGES}
    for idea in ideas:
        counts[idea["stage"]] += 1
    contributors = []
    for row, visible in rows:
        mine = [i for i in ideas if any(c["person_id"] == row.person_id for c in i["came_from"])]
        per = {s: sum(i["stage"] == s for i in mine) for s in STAGES}
        contributors.append({
            "id": row.person_id, "display_name": resolver.name(row.person), "role": row.role,
            "source": row.source, "since": row.created_at,
            "has_page": bool(row.summary) and visible,
            "page_withheld": bool(row.summary) and not visible,
            "goal": (row.summary.get("goal") or "") if visible else "",
            "written_at": row.summary_written_at if visible else None,
            "ideas": per,
        })
    # The people with pages lead: they are the engaged builders this is for.
    contributors.sort(key=lambda c: (not c["has_page"], -sum(c["ideas"].values())))
    return {"agent": project.agent.slug, "project": project.ext_id, "name": project.name,
            "stages": counts, "ideas": ideas, "contributors": contributors}


def page(project: AgentProject, person, reader: Reader) -> dict:
    """One contributor's page on `project`."""
    resolver = _People(project.agent.workspace_id)
    rows = _visible_rows(project, reader)
    claims = _claims(rows)
    row = next((r for r, _ in rows if r.person_id == person.pk), None)
    visible = next((v for r, v in rows if r.person_id == person.pk), False)
    tasks = list(project.tasks.select_related("raised_by", "raised_by__initiator_user",
                                              "raised_by__initiator_contact")
                 .order_by("-updated_at", "id"))
    ideas = []
    for t in tasks:
        sources = came_from(t, page_claims=claims.get(t.ext_id.lower(), []), resolver=resolver)
        mine = [c for c in sources if c["person_id"] == person.pk]
        if mine:
            idea = _idea_dict(t, sources)
            # Their own words about THIS idea lead the card.
            idea["their_words"] = mine[0]
            ideas.append(idea)
    order = {SHIPPED: 0, STARTED: 1, PROPOSED: 2, DECLINED: 3}
    ideas.sort(key=lambda i: order[i["stage"]])
    summary = None
    state = "not_written"
    if row is not None and row.summary:
        if visible:
            s = row.summary
            summary = {"goal": s.get("goal", ""), "quotes": s.get("quotes") or [],
                       "open": s.get("open") or [], "sources": s.get("sources") or [],
                       "basis": s.get("basis", ""), "written_at": row.summary_written_at,
                       "written_by": row.summary_by_agent.slug if row.summary_by_agent_id else ""}
            state = "written"
        else:
            state = "withheld"
    may_write, why_not = may_write_summary(person, reader)
    counts = {s: sum(i["stage"] == s for i in ideas) for s in STAGES}
    return {
        "person": {"id": person.pk, "display_name": resolver.name(person),
                   "email": _email(person)},
        "agent": project.agent.slug, "project": project.ext_id, "project_name": project.name,
        "role": row.role if row is not None else "",
        "participant_since": row.created_at if row is not None else None,
        "summary_state": state, "summary": summary,
        "ideas": ideas, "stages": counts,
        "may_write": may_write, "why_not": why_not,
    }


def _email(person) -> str:
    from apps.contacts import people

    return people.email_of(person)

"""Ideas on an agent's project, and the team view that shows them (hal T82).

An IDEA is a task with `kind="idea"`: a person's want about the product,
carrying their own words (at least one human-why quote, T68 — enforced in
`services`), with the work toward it as its subtasks (T72's `parent`). It is
never inferred. T69 read every task as an idea, so the agent's own process
work (a docs round-up, a screen review, an email round) counted as the
product's shipped ideas — and the Ideas header, the People card and a
contributor's page each counted differently, because each counted its own way.

So there is ONE read, `of_project` — the project's top-level idea tasks — and
every number on every page is a count over its result: the stage tally, each
contributor's tally, a contributor page's list. Two views cannot disagree about
a figure neither computes itself.

An idea's stage IS its task's status, in a contributor's words: suggested,
backlog or todo → proposed, in progress → started, done → shipped (declined →
not pursued).
Subtask progress alone never ships it: the idea task is marked done by its
owner (an editor) or by the person whose idea it is.
"""
from __future__ import annotations

import logging
import re

from apps.contacts import people
from apps.contacts import services as contacts
from apps.contacts.models import Person
from apps.harness import initiator as who
from apps.harness import services as harness
from apps.harness.models import Turn
from apps.workspaces import permissions as perms

from . import services
from .models import AgentProject, AgentTask, ProjectParticipant
from .services import LIVE_STATUSES

logger = logging.getLogger(__name__)

PROPOSED, STARTED, SHIPPED, DECLINED = "proposed", "started", "shipped", "declined"
STAGES = (PROPOSED, STARTED, SHIPPED, DECLINED)
STAGE = {AgentTask.SUGGESTED: PROPOSED, AgentTask.BACKLOG: PROPOSED, AgentTask.TODO: PROPOSED,
         AgentTask.IN_PROGRESS: STARTED, AgentTask.DONE: SHIPPED, AgentTask.DECLINED: DECLINED}

#: An idea's title when the person gave none: the start of their words.
_TITLE_FROM_QUOTE = 120


class IdeaError(ValueError):
    """An idea canopy will not store. The message is safe to show the caller."""


# --- who may -------------------------------------------------------------------------


def may_edit_project(project: AgentProject, user) -> bool:
    """The project's lead, an editor of its workspace (or its lead agent's), the
    lead agent's admins, or the lead agent itself — the people the work board is
    for."""

    agent = project.agent
    if not getattr(user, "is_authenticated", False):
        return False
    return (project.owner_user_id == user.pk
            or perms.can(user, project.workspace_id, perms.AGENT_WORK)
            or (agent is not None and (agent.is_own_login(user) or agent.is_admin(user)
                                       or perms.can(user, agent.workspace_id, perms.AGENT_WORK))))


def is_ideas_person(task: AgentTask, user) -> bool:
    """Is `user` someone whose words this idea carries? They may mark it shipped."""
    if task.kind != AgentTask.IDEA or not getattr(user, "is_authenticated", False):
        return False
    return task.human_whys.filter(person__user_id=user.pk).exists()


# --- writing -------------------------------------------------------------------------


def add_own_idea(project: AgentProject, *, quote: str, title: str, user) -> AgentTask:
    """A member's own idea, in their own words: a `kind="idea"` task on the
    project with them as its human-why. Recording SOMEONE ELSE's words is the
    task API's (`POST /tasks/` with `kind: "idea"` and `human_why`), which is
    the editor/agent tier."""

    quote = (quote or "").strip()
    if not quote:
        raise IdeaError("an idea needs your own words")
    person = contacts.person_for(user=user)
    if person is None:
        raise IdeaError("canopy could not tell who you are")
    title = (title or "").strip() or _title_from(quote)
    # Filed by the person, in the project; its lead agent is who takes it on.
    [task] = services.create_tasks(None, [{
        "title": title, "project": project.address, "kind": AgentTask.IDEA,
        "assignee": f"agent:{project.agent.slug}" if project.agent_id else "",
        "status": AgentTask.SUGGESTED, "origin": "api", "source": "team view",
        "origin_ref": {"submitted_by": user.email or user.get_username()},
        "human_why": [{"quote": quote, "who": {"person_id": person.pk}}],
    }], workspace=project.workspace_id, by_user=user)
    return task


def _title_from(quote: str) -> str:
    line = " ".join(quote.split())
    return line if len(line) <= _TITLE_FROM_QUOTE else line[: _TITLE_FROM_QUOTE - 1].rstrip() + "…"


# --- the one read ----------------------------------------------------------------------


def _who(task: AgentTask, names: dict):
    """(who, their quote row) — the first human-why that names a person, else the first."""

    whys = list(task.human_whys.all())
    first = next((w for w in whys if w.person_id is not None), whys[0] if whys else None)
    if first is None:
        return {"person_id": None, "name": "someone"}, None
    if first.person_id is not None:
        if first.person_id not in names:
            names[first.person_id] = people.display_name(first.person)
        name = names[first.person_id]
    else:
        name = first.who_name or first.who_email or "someone"
    return {"person_id": first.person_id, "name": name}, first


def idea_dict(task: AgentTask, names: dict | None = None) -> dict:
    who, why = _who(task, {} if names is None else names)
    subs = sorted(task.subtasks.all(), key=lambda t: (t.status != AgentTask.IN_PROGRESS, t.pk))
    return {
        "ext_id": task.ref, "key": task.key, "title": task.title, "status": task.status,
        "stage": STAGE.get(task.status, PROPOSED),
        "quote": why.quote if why else "", "who": who,
        "source_url": why.source_url if why else "", "said_at": why.said_at if why else None,
        "people": sorted({w.person_id for w in task.human_whys.all() if w.person_id is not None}),
        "subtasks": [{"ext_id": t.ref, "key": t.key, "title": t.title, "status": t.status} for t in subs],
        "created_at": task.created_at, "updated_at": task.updated_at,
    }


def of_project(project: AgentProject) -> list[dict]:
    """The project's ideas: its top-level `kind="idea"` tasks, newest-moved
    first. THE read: every count on every page is a count over this list."""
    rows = (project.tasks.filter(kind=AgentTask.IDEA, parent__isnull=True)
            .prefetch_related("human_whys", "human_whys__person", "human_whys__person__user",
                              "subtasks")
            .order_by("-updated_at", "id"))
    names: dict = {}
    return [idea_dict(t, names) for t in rows]


def counts(ideas: list[dict]) -> dict:
    return {s: sum(i["stage"] == s for i in ideas) for s in STAGES}


def of_person(ideas: list[dict], person_id: int) -> list[dict]:
    """The ideas carrying this person's words."""
    return [i for i in ideas if person_id in i["people"]]


# --- notifying the people who act on it ----------------------------------------------


def notify(task: AgentTask, *, by_user) -> list[str]:
    """Tell the agent (one turn) and the project's owner (one email) that `by_user`
    added an idea. Returns who was told, by name. Never raises: an idea that was
    stored must not come back as an error because a notice did not go out."""
    from django.conf import settings
    from django.core.mail import send_mail

    project = task.project
    agent = project.agent
    told: list[str] = []
    who_name = by_user.get_full_name() or by_user.email or "someone"
    quote = next((w.quote for w in task.human_whys.all()), "")
    if agent is not None:
        try:
            prompt = (
                f"{who_name} added an idea to {project.address} {project.name} on its team view "
                f"(task {task.key}, kind idea). In their words:\n\n> {quote}\n\n"
                f"It shows as proposed. Read it; if you take it on, file the work as its subtasks "
                f"(`parent: {task.key}`), and make sure the project's owner has seen it."
            )
            once = "idea-" + str(task.pk)  # one notice per idea, however often this runs
            harness.enqueue_turn(
                agent=agent, origin=Turn.ORIGIN_API, idempotency_key=once, prompt=prompt,
                origin_ref={"project": project.address, "task": task.key, "trigger": "idea_added"},
                initiator=who.for_user(by_user, via=f"idea:{task.key}", assurance=who.SESSION),
            )
            told.append(agent.name or agent.slug)
        except Exception:  # noqa: BLE001 — see docstring
            logger.exception("ideas: could not tell %s about idea %s", agent.slug, task.pk)
    owner = project.owner_user
    if owner is not None and owner.pk != getattr(by_user, "pk", None) and owner.email:
        try:
            sent = send_mail(
                subject=f"New idea on {project.name}: from {who_name}",
                message=(f"{who_name} added an idea to {project.name}:\n\n\"{quote}\"\n\n"
                         f"It is on the project's team view as proposed."),
                from_email=settings.DEFAULT_FROM_EMAIL, recipient_list=[owner.email],
                fail_silently=False,
            )
            if sent:
                told.append(owner.get_full_name() or owner.email)
        except Exception:  # noqa: BLE001 — see docstring
            logger.exception("ideas: could not email %s about idea %s", owner.pk, task.pk)
    return told


# --- the team view -------------------------------------------------------------------

_T_PREFIX = re.compile(r"^\s*\[T\d+\]\s*")


def _plain(text: str, limit: int = 160) -> str:
    """One line, no task-number prefix, at most `limit` characters."""
    text = (text or "").strip()
    line = _T_PREFIX.sub("", text.splitlines()[0]) if text else ""
    return line if len(line) <= limit else line[: limit - 1].rstrip() + "…"


def _user_name(user) -> str:
    return (user.get_full_name() or user.email or user.get_username()) if user is not None else ""


def owner_name(project: AgentProject) -> str:
    return _user_name(project.owner_user) if project.owner_user_id else project.owner_note


def _is_agent_label(text: str, agent) -> bool:
    t = (text or "").strip().lower()
    return t in ({""} if agent is None else {"", agent.slug.lower(), (agent.name or "").lower()})


def waiting_on_people(project: AgentProject) -> list[dict]:
    """What the project is waiting on people for, one line each.

    Three sources, all on live tasks: an open ask (its first line); a task
    parked on a person (its next step); and a task's links marked
    `kind: "decision"` — several open questions one task is carrying, each
    with its issue. Waiting on: the link's or task's named person, else the
    project's owner."""

    owner = owner_name(project)
    agent = project.agent
    out: list[dict] = []
    tasks = (project.tasks.filter(status__in=LIVE_STATUSES)
             .select_related("waiting_on_user").order_by("rank", "id"))
    for task in tasks:
        person = (_user_name(task.waiting_on_user) if task.waiting_on_user_id
                  else (task.assigned if not _is_agent_label(task.assigned, agent) else ""))
        if task.ask_is_open:
            out.append({"text": _plain(task.ask_body or task.title), "url": "",
                        "waiting_on": person or owner})
        elif task.waiting_on_user_id:
            out.append({"text": _plain(task.next_action or task.title), "url": "",
                        "waiting_on": person})
        for link in task.links or []:
            if isinstance(link, dict) and link.get("kind") == "decision":
                out.append({"text": _plain(link.get("label", "")), "url": link.get("url", ""),
                            "waiting_on": (link.get("waiting_on") or "").strip() or person or owner})
    return out


def contributors(project: AgentProject, ideas: list[dict], *, page_visible) -> list[dict]:
    """The project's people with their tallies, counted over `ideas`.

    Participant rows, plus anyone an idea's words name. `page_visible(row)` says
    whether the reader may see that row's contributor page."""

    rows = list(ProjectParticipant.objects.filter(project=project)
                .select_related("person", "person__user").order_by("-created_at", "-pk"))
    out, listed = [], set()
    for row in rows:
        visible = bool(row.summary) and page_visible(row)
        out.append({
            "id": row.person_id, "display_name": people.display_name(row.person), "role": row.role,
            "source": row.source, "since": row.created_at,
            "has_page": visible, "page_withheld": bool(row.summary) and not visible,
            "goal": (row.summary.get("goal") or "") if visible else "",
            "written_at": row.summary_written_at if visible else None,
            "ideas": counts(of_person(ideas, row.person_id)),
        })
        listed.add(row.person_id)
    for pid in sorted({p for i in ideas for p in i["people"]} - listed):
        mine = of_person(ideas, pid)
        person = Person.objects.select_related("user").filter(pk=pid).first()
        if person is None:
            continue
        out.append({
            "id": pid, "display_name": people.display_name(person), "role": "", "source": "idea",
            "since": min(i["created_at"] for i in mine), "has_page": False, "page_withheld": False,
            "goal": "", "written_at": None, "ideas": counts(mine),
        })
    out.sort(key=lambda c: (not c["has_page"], -sum(c["ideas"].values())))
    return out


def team(project: AgentProject, user) -> dict:
    """Everything the team view shows, in its order: what it is, where it
    stands, the ideas, the people. Nothing of the agent's own process — no
    task cards, rationale, notes or review logs."""

    from .contributors import may_see_summary, reader_of  # cycle: agents.contributors -> agents.ideas  # noqa: PLC0415

    reader = reader_of(user)
    ideas = of_project(project)
    agent = project.agent
    me = contacts.person_for(user=user) if getattr(user, "is_authenticated", False) else None
    return {
        "agent": agent.slug if agent else "", "agent_name": (agent.name or agent.slug) if agent else "",
        "project": project.ext_id or project.handle, "name": project.name, "outcome": project.outcome,
        "status": project.status, "owner_name": owner_name(project),
        "links": [link for link in (project.links or []) if isinstance(link, dict) and link.get("url")],
        "may_edit": may_edit_project(project, user),
        "my_person_id": me.pk if me is not None else None,
        "built": [i for i in ideas if i["stage"] == SHIPPED],
        "in_progress": [i for i in ideas if i["stage"] == STARTED],
        "waiting": waiting_on_people(project),
        "stages": counts(ideas),
        "ideas": ideas,
        "contributors": contributors(project, ideas,
                                     page_visible=lambda row: may_see_summary(row.person, reader)),
    }

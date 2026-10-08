"""Who takes part in an agent's project (fleet brain v1.1, canopy#804).

A project used to know one human — its `owner_user` — and nothing about the
people the work actually involves. The brain already sees them: a fact filed
against a project names its subject, and a task raised from a conversation
names the human who was talking. This module turns both into
`ProjectParticipant` rows, automatically, so "who is on P3" and "what is
Fatima working on" have an answer without anyone maintaining a list.

Two triggers, both best-effort (a participant is never worth a failed write):

* `on_fact(fact)` — from `people.record_fact`, for ANY kind of fact that
  carries a `project` (source=fact);
* `on_task(task)` — wherever a task gains a project or a `raised_by` turn
  (create, patch, `set_task_project`): the turn's initiator, when a human,
  joins `task.project` (source=turn).

The first row wins: `ensure` never changes an existing participant's source
or role.
"""
from __future__ import annotations

import logging

from django.db import IntegrityError, transaction

from .models import AgentProject, ProjectParticipant

logger = logging.getLogger(__name__)

#: Most projects the envelope `person` block lists.
ENVELOPE_PROJECTS = 5


def ensure(project: AgentProject, person, *, source: str, role: str = "") -> ProjectParticipant | None:
    """Make `person` a participant of `project` if they are not one already.

    Returns the row (new or existing). Never changes an existing row.
    """
    if project is None or person is None:
        return None
    existing = ProjectParticipant.objects.filter(project=project, person=person).first()
    if existing is not None:
        return existing
    try:
        with transaction.atomic():  # savepoint: a concurrent insert must not poison the caller
            return ProjectParticipant.objects.create(
                project=project, person=person, source=source,
                role=(role or "").strip()[:ProjectParticipant.ROLE_MAX])
    except IntegrityError:
        return ProjectParticipant.objects.filter(project=project, person=person).first()


def on_fact(fact) -> None:
    """A fact filed against a project makes its subject a participant."""
    if not fact.project_id:
        return
    try:
        ensure(fact.project, fact.person, source=ProjectParticipant.FACT)
    except Exception:  # noqa: BLE001
        logger.exception("participants: could not link fact %s to its project", fact.pk)


def on_task(task) -> None:
    """A task in a project, raised by a human's turn, makes that human a participant."""
    if not task.project_id or not task.raised_by_id:
        return
    try:
        from apps.contacts import people

        person = people.initiator_person(task.raised_by)
        if person is not None:
            ensure(task.project, person, source=ProjectParticipant.TURN)
    except Exception:  # noqa: BLE001
        logger.exception("participants: could not link task %s's raiser to its project", task.pk)


def of_project(project: AgentProject) -> list[dict]:
    """The project's participants as the API shows them, newest first."""
    from apps.contacts import people

    rows = (ProjectParticipant.objects.filter(project=project)
            .select_related("person", "person__user").order_by("-created_at", "-pk"))
    return [{"id": r.person_id, "display_name": people.display_name(r.person),
             "email": people.email_of(r.person), "role": r.role, "source": r.source,
             "since": r.created_at}
            for r in rows]


def projects_of(person, workspace_slug: str, *, limit: int | None = None,
                include_archived: bool = True):
    """The participations of `person` in projects of `workspace_slug`'s agents,
    most recently active project first. Only that workspace's: a project is
    served only where its agent lives."""
    qs = (ProjectParticipant.objects
          .filter(person=person, project__agent__workspace_id=workspace_slug)
          .select_related("project", "project__agent")
          .order_by("-project__updated_at", "-created_at", "-pk"))
    if not include_archived:
        qs = qs.exclude(project__status=AgentProject.ARCHIVED)
    return list(qs[:limit] if limit else qs)


def backfill() -> dict:
    """Link every existing fact-with-a-project and every task raised by a human
    turn. Idempotent; returns counts. The `backfill_project_participants`
    command runs it (the data migration does the fact half on historical
    models)."""
    from apps.contacts.models import PersonFact

    from .models import AgentTask

    before = ProjectParticipant.objects.count()
    facts = 0
    for fact in (PersonFact.objects.filter(project__isnull=False)
                 .select_related("project", "person").iterator()):
        on_fact(fact)
        facts += 1
    tasks = 0
    for task in (AgentTask.objects.filter(project__isnull=False, raised_by__isnull=False)
                 .select_related("project", "raised_by", "raised_by__initiator_user",
                                 "raised_by__initiator_contact").iterator()):
        on_task(task)
        tasks += 1
    return {"facts_seen": facts, "tasks_seen": tasks,
            "participants_added": ProjectParticipant.objects.count() - before}

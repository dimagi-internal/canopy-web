"""Proposed projects — a project an agent puts to people, and what they do with it.

Spec: `docs/superpowers/specs/2026-10-16-work-group-huddles-design.md` (layer 1).
Nothing here is huddle code: a huddle's survivors land here, and so does a
project an agent proposes from a chat or its own turn.

A proposal is ordinary rows, so the rest of canopy reads it without knowing:

* the PROJECT, `status=proposed`, `source_url` the page that proposed it;
* its PLAN steps — the project's tasks, `suggested`;
* ONE decision card — a `review` task, "Start this project?", whose body is the
  why and the done-when. Approve / decline / reply are the existing task actions
  (`services.act` calls `before_act` / `after_act` here), so a decision made on
  the board, the project page or the huddle page is the same click;
* a linked `suggested` task on each PARTNER's own board (projects stay per agent,
  Jonathan 2026-09-19), whose `source_url` is the project page;
* `ProjectBacking` rows — where each person and agent stands. A signal, never a
  gate: nobody's backing starts anything.

The three kinds of task are told apart by `origin_ref.kind`, never by title.

A PROPOSED TASK (spec 2026-10-17-project-check-ins-design.md §3) is the same
idea one level down — what a project's check-in produces: a `suggested` task in
the project (`origin_ref.kind == "checkin_proposal"`), its plan steps as child
tasks, a linked task on each partner's board, and backings on the task. Start
and Decline are the task's own approve and decline. A task that turns out to be
a project's worth of work SPAWNS one (`AgentProject.spawned_from`).
"""
from __future__ import annotations

import datetime as dt

from django.db import transaction
from django.utils import timezone

from apps.huddles.models import ProjectCheckin

from .models import Agent, AgentProject, AgentTask, AgentTaskAction, ProjectBacking, ProjectParticipant

#: A proposed project nobody starts or declines within this is archived as "not taken up".
EXPIRES_AFTER = dt.timedelta(days=14)
DECISION, PLAN_STEP, PARTNER = "project_decision", "project_plan_step", "project_partner"
#: A check-in's proposed task, its plan steps (child tasks) and its partners' linked tasks.
CHECKIN_PROPOSAL, TASK_PLAN_STEP, TASK_PARTNER = "checkin_proposal", "checkin_plan_step", "task_partner"
DECISION_TITLE = "Start this project?"
#: Who the expiry's decline is "by" — never a person, so `before_act` lets it through.
EXPIRY_BY = "system:proposal-expiry"
NOT_TAKEN_UP = "Not taken up: nobody started or declined it within 14 days."


class ProposalError(ValueError):
    """The proposal itself is malformed (422)."""


class DeclinedBefore(Exception):
    """A project of that name was declined, and no `new_evidence` came with it (409)."""


class MayNotDecide(PermissionError):
    """Only a workspace owner or admin, or the project's owner, starts or declines it (403)."""


class UnknownRun(ValueError):
    """`run` names no check-in run of this project (422)."""


# --- reading the rows ------------------------------------------------------------


def page_path(project: AgentProject) -> str:
    return f"/agents/{project.agent.slug}/projects/{project.ext_id}"


def page_url(project: AgentProject) -> str:
    from apps.workspaces import services as wsvc

    return wsvc.scoped_url(project.agent.workspace_id, page_path(project))


def _kind(task: AgentTask) -> str:
    ref = task.origin_ref if isinstance(task.origin_ref, dict) else {}
    return str(ref.get("kind") or "")


def is_decision(task: AgentTask) -> bool:
    return _kind(task) == DECISION


def decision_card(project: AgentProject) -> AgentTask | None:
    return project.tasks.filter(origin_ref__kind=DECISION).order_by("-pk").first()


def plan_steps(project: AgentProject) -> list[AgentTask]:
    return list(project.tasks.filter(origin_ref__kind=PLAN_STEP).order_by("position", "pk"))


def partner_tasks(project: AgentProject):
    return (AgentTask.objects.filter(origin_ref__kind=PARTNER, origin_ref__project_id=project.pk)
            .select_related("agent").order_by("agent__slug", "pk"))


def may_decide(user, project: AgentProject) -> bool:
    """Decision 4(b): the workspace's owners and admins, or the project's own owner."""
    from apps.workspaces import permissions as perms

    if not getattr(user, "is_authenticated", False):
        return False
    return (project.owner_user_id == user.pk
            or perms.can(user, project.agent.workspace_id, perms.PROJECTS_DECIDE))


def _declined_by_a_person(project: AgentProject) -> bool:
    card = decision_card(project)
    return card is not None and card.actions.filter(
        action=AgentTaskAction.DECLINE).exclude(by=EXPIRY_BY).exists()


# --- proposing -------------------------------------------------------------------


def _card_body(why: str, outcome: str) -> str:
    return f"{why.strip()}\n\nDone when: {outcome.strip()}"


def _sync_plan(agent: Agent, project: AgentProject, plan: list[str]) -> None:
    """The project's plan steps become exactly `plan`, in order. A step already
    being worked on (or finished) is kept as it is: only suggestions are rewritten."""
    from . import services

    steps = plan_steps(project)
    for i, title in enumerate(plan):
        if i < len(steps):
            step = steps[i]
            if step.status == AgentTask.SUGGESTED and (step.title, step.position) != (title, i):
                step.title, step.position = title, i
                step.save(update_fields=["title", "position", "updated_at"])
            continue
        services.create_tasks(agent, [{
            "title": title, "project": project.ext_id, "status": AgentTask.SUGGESTED, "position": i,
            "origin_ref": {"kind": PLAN_STEP}, "source_url": page_url(project)}])
    for step in steps[len(plan):]:
        if step.status == AgentTask.SUGGESTED:
            step.delete()


def _sync_partners(project: AgentProject, partners: list[Agent]) -> None:
    """One linked `suggested` task on each partner's board; a partner dropped by a
    revision has its still-suggested task declined."""
    from . import services

    have = {t.agent_id: t for t in partner_tasks(project)}
    title = f"[{project.agent.slug} {project.ext_id}] {project.name}"
    for partner in partners:
        task = have.pop(partner.pk, None)
        if task is not None:
            if task.title != title:
                task.title = title
                task.save(update_fields=["title", "updated_at"])
            continue
        services.create_tasks(partner, [{
            "title": title, "status": AgentTask.SUGGESTED,
            "rationale": f"{project.agent.slug} proposes {project.name!r} with you as a partner.",
            "origin_ref": {"kind": PARTNER, "project_id": project.pk, "lead": project.agent.slug,
                           "project": project.ext_id},
            "source_url": page_url(project)}])
    for task in have.values():
        if task.status == AgentTask.SUGGESTED:
            task.status = AgentTask.DECLINED
            task.save(update_fields=["status", "updated_at"])


@transaction.atomic
def propose(agent: Agent, *, name: str, outcome: str, why: str, plan: list[str],
            partners: list[Agent], source_url: str = "", owner_email: str = "",
            new_evidence: str = "", from_task: AgentTask | None = None) -> tuple[AgentProject, bool]:
    """Create — or revise — a proposed project; returns (project, created).

    REVISING: an open proposed project of this agent's with the same name
    (ignoring case) is updated in place — outcome, why, plan, partners — and its
    backings go `stale` (they backed the earlier version). It never files a
    second decision card.

    DECLINED BEFORE: a project of that name a person declined comes back only with
    `new_evidence`, as a new project whose why carries it; otherwise
    `DeclinedBefore`. One that merely expired may come back freely.
    """
    from . import services

    name, outcome, why = name.strip(), outcome.strip(), why.strip()
    plan = [s.strip() for s in plan if s and s.strip()]
    if not (name and outcome and why):
        raise ProposalError("a proposal needs a name, an outcome (done when) and a why")
    if any(p.pk == agent.pk for p in partners):
        raise ProposalError(f"{agent.slug} leads this project; it cannot also be a partner")
    if new_evidence.strip():
        why = f"{why}\n\nNew evidence: {new_evidence.strip()}"
    owner = services.resolve_project_owner(agent, owner_email) if owner_email else None
    now = timezone.now()

    project = (AgentProject.objects.select_for_update().select_related("agent")
               .filter(agent=agent, status=AgentProject.PROPOSED, name__iexact=name).first())
    created = project is None
    if created:
        if not new_evidence.strip() and any(
                _declined_by_a_person(p) for p in agent.projects.filter(
                    status=AgentProject.ARCHIVED, name__iexact=name)):
            raise DeclinedBefore(f"{agent.slug} proposed {name!r} before and it was declined: "
                                 "send new_evidence to propose it again")
        project = AgentProject.objects.create(
            agent=agent, ext_id=services.next_project_ext_id(agent), name=name, outcome=outcome,
            status=AgentProject.PROPOSED, source_url=source_url, proposed_at=now, owner_user=owner,
            spawned_from=from_task)
        [card] = services.create_tasks(agent, [{
            "title": DECISION_TITLE, "project": project.ext_id, "status": AgentTask.SUGGESTED,
            "ask_kind": AgentTask.ASK_REVIEW, "ask_body": _card_body(why, outcome), "rationale": why,
            "origin_ref": {"kind": DECISION}, "source_url": page_url(project),
            "position": -1}])
        if owner is not None:
            card.waiting_on_user = owner
            card.save(update_fields=["waiting_on_user"])
    else:
        project.outcome, project.proposed_at = outcome, now
        if source_url:
            project.source_url = source_url
        if owner is not None:
            project.owner_user = owner
        if from_task is not None:
            project.spawned_from = from_task
        project.save()
        card = decision_card(project)
        if card is not None:
            card.ask_body, card.rationale = _card_body(why, outcome), why
            card.save(update_fields=["ask_body", "rationale", "updated_at"])
        project.backings.update(stale=True)
    _sync_plan(agent, project, plan)
    _sync_partners(project, partners)
    return project, created


# --- deciding (called from services.act) -----------------------------------------


def before_act(task: AgentTask, action: str, by_user) -> None:
    """Refuse a start/decline on a decision card by someone who may not decide.

    `by_user` None is machinery (the expiry) and is let through: every route
    passes the person acting."""
    if action not in (AgentTaskAction.APPROVE, AgentTaskAction.DECLINE):
        return
    if is_task_proposal(task):
        if by_user is not None and task.project is not None and not may_decide_task(by_user, task):
            raise MayNotDecide("only the project's owner, the check-in's lead, or a workspace owner or admin "
                               "may start or decline a proposed task — back it or reply instead")
        return
    if not is_decision(task):
        return
    project = task.project
    if project is None:
        return
    if by_user is not None and not may_decide(by_user, project):
        raise MayNotDecide("only a workspace owner or admin, or the project's owner, may start or "
                           "decline a proposed project — back it or reply instead")


def after_act(task: AgentTask, action: str, comment: str) -> None:
    """A decision card's approve starts the project (its partners' tasks move to
    in progress — the lead's turn on the card starts the work); a decline archives
    it with the reason, and declines what was only suggested. A proposed task
    does the same for its own partners and plan steps."""
    if action not in (AgentTaskAction.APPROVE, AgentTaskAction.DECLINE):
        return
    if is_task_proposal(task):
        _after_task_act(task, action, comment)
        return
    if not is_decision(task):
        return
    project = task.project
    if project is None or project.status != AgentProject.PROPOSED:
        return
    if action == AgentTaskAction.APPROVE:
        project.status = AgentProject.ACTIVE
        project.save(update_fields=["status", "updated_at"])
        partner_tasks(project).filter(status=AgentTask.SUGGESTED).update(
            status=AgentTask.IN_PROGRESS, updated_at=timezone.now())
        return
    reason = (comment or "").strip() or "Declined."
    project.status = AgentProject.ARCHIVED
    project.notes = (f"{project.notes}\n\n" if project.notes else "") + f"Declined: {reason}"
    project.save(update_fields=["status", "notes", "updated_at"])
    later = timezone.now()
    partner_tasks(project).filter(status=AgentTask.SUGGESTED).update(status=AgentTask.DECLINED,
                                                                     updated_at=later)
    project.tasks.filter(origin_ref__kind=PLAN_STEP, status=AgentTask.SUGGESTED).update(
        status=AgentTask.DECLINED, updated_at=later)


def _after_task_act(task: AgentTask, action: str, comment: str) -> None:
    later = timezone.now()
    if action == AgentTaskAction.APPROVE:
        task_partner_tasks(task).filter(status=AgentTask.SUGGESTED).update(status=AgentTask.IN_PROGRESS,
                                                                            updated_at=later)
        return
    reason = (comment or "").strip() or "Declined."
    AgentTask.objects.filter(pk=task.pk).update(
        notes=(f"{task.notes}\n\n" if task.notes else "") + f"Declined: {reason}", updated_at=later)
    task.refresh_from_db(fields=["notes"])
    task_partner_tasks(task).filter(status=AgentTask.SUGGESTED).update(status=AgentTask.DECLINED, updated_at=later)
    task.subtasks.filter(origin_ref__kind=TASK_PLAN_STEP, status=AgentTask.SUGGESTED).update(
        status=AgentTask.DECLINED, updated_at=later)


def expire_stale(now: dt.datetime | None = None) -> int:
    """Archive every proposed project nobody started or declined within
    `EXPIRES_AFTER` of its last proposal: its decision card is declined "not taken
    up" (by `EXPIRY_BY`), and an `expired` event is recorded. Returns how many.
    Safe to run from every web process at once: `services.act` locks the card,
    and a card someone else just closed is skipped."""
    from . import engagement, services

    cutoff = (now or timezone.now()) - EXPIRES_AFTER
    done = 0
    for project in (AgentProject.objects.filter(status=AgentProject.PROPOSED, proposed_at__lt=cutoff)
                    .select_related("agent")):
        card = decision_card(project)
        try:
            if card is not None and card.ask_is_open:
                services.act(card, action=AgentTaskAction.DECLINE, comment=NOT_TAKEN_UP, by=EXPIRY_BY,
                             by_user=None, actor_workspace_ids={project.agent.workspace_id})
            else:
                AgentProject.objects.filter(pk=project.pk, status=AgentProject.PROPOSED).update(
                    status=AgentProject.ARCHIVED, notes=f"Declined: {NOT_TAKEN_UP}")
        except services.ClosedAskError:
            continue  # decided a moment ago, by a person or another process
        engagement.record("expired", workspace=project.agent.workspace_id, agent=project.agent,
                          project=project, task=card)
        done += 1
    return done + expire_stale_tasks(now)


def expire_stale_tasks(now: dt.datetime | None = None) -> int:
    """Decline every proposed task nobody started or declined within
    `EXPIRES_AFTER` of its last proposal, "not taken up" (by `EXPIRY_BY`)."""
    from . import engagement, services

    cutoff = (now or timezone.now()) - EXPIRES_AFTER
    done = 0
    rows = (AgentTask.objects.filter(status=AgentTask.SUGGESTED, origin_ref__kind=CHECKIN_PROPOSAL)
            .select_related("agent", "project"))
    for task in rows:
        if (_proposed_at(task) or task.created_at) >= cutoff:
            continue
        try:
            services.act(task, action=AgentTaskAction.DECLINE, comment=NOT_TAKEN_UP, by=EXPIRY_BY,
                         by_user=None, actor_workspace_ids={task.agent.workspace_id})
        except services.ClosedAskError:
            continue
        engagement.record("expired", workspace=task.agent.workspace_id, agent=task.agent,
                          project=task.project, task=task, meta={"level": "task"})
        done += 1
    return done


# --- backing ---------------------------------------------------------------------


def back(project: AgentProject | None, *, stance: str, line: str = "", person=None, agent: Agent | None = None,
         source: str = "web", huddle_id: str = "", task: AgentTask | None = None) -> ProjectBacking:
    """Set where `person` (or `agent`) stands on `project` — or on `task`, a
    proposed task (pass `project=None`) — replacing what they said before;
    rewriting clears `stale`. A person who backs becomes a participant (of the
    task's project, for a task)."""
    from . import participants

    if (person is None) == (agent is None):
        raise ProposalError("a backing is a person's or an agent's, never both")
    if stance not in ProjectBacking.STANCES:
        raise ProposalError(f"stance must be one of {', '.join(ProjectBacking.STANCES)}")
    line = " ".join((line or "").split())
    if len(line) > ProjectBacking.LINE_MAX:
        raise ProposalError(f"keep it to one line ({ProjectBacking.LINE_MAX} characters)")
    if stance != ProjectBacking.BACKS and not line:
        raise ProposalError("say in one line what you would change, or why you pass")
    if (project is None) == (task is None):
        raise ProposalError("a backing is on a project or a task, never both")
    who = {"person": person} if person is not None else {"agent": agent}
    on = {"project": project} if project is not None else {"task": task}
    row, _ = ProjectBacking.objects.update_or_create(
        **on, **who,
        defaults={"stance": stance, "line": line, "source": source, "huddle_id": huddle_id,
                  "stale": False})
    if person is not None:
        participants.ensure(project if project is not None else task.project, person,
                            source=ProjectParticipant.BACKING)
    return row


def unback(project: AgentProject | None, person, *, task: AgentTask | None = None) -> bool:
    on = {"project": project} if project is not None else {"task": task}
    return ProjectBacking.objects.filter(person=person, **on).delete()[0] > 0


def _backer(row: ProjectBacking) -> dict:
    from apps.contacts import people

    if row.agent_id:
        return {"kind": "agent", "agent": row.agent.slug, "person_id": None,
                "name": row.agent.name or row.agent.slug, "email": ""}
    return {"kind": "person", "agent": "", "person_id": row.person_id,
            "name": people.display_name(row.person), "email": people.email_of(row.person)}


def backings_of(project: AgentProject | AgentTask, *, me=None) -> list[dict]:
    """Every backing on a project or a task, oldest first; `mine` marks the
    reader's own (`me` a Person)."""
    rows = project.backings.select_related("agent", "person", "person__user")
    return [{"backer": _backer(r), "stance": r.stance, "line": r.line, "source": r.source,
             "huddle_id": r.huddle_id, "stale": r.stale,
             "mine": me is not None and r.person_id == me.pk, "updated_at": r.updated_at}
            for r in rows]


# --- the one read ----------------------------------------------------------------


def _comments(card: AgentTask | None) -> list[dict]:
    if card is None:
        return []
    agent_users = {card.agent.user_id} - {None}
    rows = card.actions.filter(action=AgentTaskAction.REPLY).order_by("created_at", "pk")
    return [{"by": r.by, "by_agent": r.by_user_id in agent_users, "comment": r.comment,
             "at": r.created_at} for r in rows]


def card(project: AgentProject, user) -> dict:
    """A proposed (or once-proposed) project as the project page and the huddle
    page both render it (`ProposedProjectCard`): what it is, the decision and who
    may make it, where people stand, what they said, and its partners."""
    from apps.contacts import services as contacts
    from apps.workspaces import services as wsvc

    decision = decision_card(project)
    me = contacts.person_for(user=user) if getattr(user, "is_authenticated", False) else None
    partners = [{"agent": t.agent.slug, "ext_id": t.ext_id, "task_id": t.pk, "status": t.status,
                 "url": wsvc.scoped_path(t.agent.workspace_id, f"/agents/{t.agent.slug}/tasks")}
                for t in partner_tasks(project)]
    proposed_at = project.proposed_at
    return {
        "id": project.pk, "agent": project.agent.slug, "workspace": project.agent.workspace_id,
        "ref": project.ext_id, "name": project.name, "status": project.status,
        "outcome": project.outcome, "why": decision.rationale if decision else "",
        "plan": [s.title for s in plan_steps(project)],
        "url": wsvc.scoped_path(project.agent.workspace_id, page_path(project)),
        "source_url": project.source_url, "proposed_at": proposed_at,
        "expires_at": (proposed_at + EXPIRES_AFTER
                       if proposed_at and project.status == AgentProject.PROPOSED else None),
        "owner_email": project.owner_email, "notes": project.notes,
        "spawned_from": project.spawned_from_ref,
        "decision_task": decision.ext_id if decision else None,
        "decision_task_id": decision.pk if decision else None,
        "decision_open": bool(decision and decision.ask_is_open),
        "may_decide": may_decide(user, project),
        "backings": backings_of(project, me=me),
        "comments": _comments(decision),
        "partners": partners,
    }


def is_proposal(project: AgentProject) -> bool:
    """Was it ever proposed? (It has a decision card.) Its page shows the card then."""
    return project.status == AgentProject.PROPOSED or decision_card(project) is not None


# --- proposed tasks: what a check-in produces (spec 2026-10-17 §3) ----------------


def is_task_proposal(task: AgentTask) -> bool:
    return _kind(task) == CHECKIN_PROPOSAL


def task_plan_steps(task: AgentTask) -> list[AgentTask]:
    return list(task.subtasks.filter(origin_ref__kind=TASK_PLAN_STEP).order_by("position", "pk"))


def task_partner_tasks(task: AgentTask):
    return (AgentTask.objects.filter(origin_ref__kind=TASK_PARTNER, origin_ref__task_id=task.pk)
            .select_related("agent").order_by("agent__slug", "pk"))


def _ref_of(task: AgentTask) -> dict:
    return task.origin_ref if isinstance(task.origin_ref, dict) else {}


def _proposed_at(task: AgentTask) -> dt.datetime | None:
    raw = _ref_of(task).get("proposed_at")
    try:
        return dt.datetime.fromisoformat(raw) if raw else None
    except (TypeError, ValueError):
        return None


def task_runs(task: AgentTask) -> list[int]:
    """Every check-in run that proposed (or revised) this task, oldest first."""
    ref = _ref_of(task)
    runs = [int(r) for r in (ref.get("runs") or []) if str(r).isdigit()]
    if not runs and str(ref.get("run") or "").isdigit():
        runs = [int(ref["run"])]
    return runs


def _checkin_leads(task: AgentTask) -> set[int]:
    """The lead of the check-in a proposed task came from — or, when it names none
    (or that check-in is gone), the leads of every check-in on its project."""

    if task.project_id is None:
        return set()
    rows = ProjectCheckin.objects.filter(project_id=task.project_id, lead__isnull=False)
    cid = _ref_of(task).get("checkin")
    named = rows.filter(pk=cid) if str(cid or "").isdigit() else rows.none()
    return set((named if named.exists() else rows).values_list("lead_id", flat=True))


def may_decide_task(user, task: AgentTask) -> bool:
    """The project's owner, the check-in's lead, or anyone with `projects.decide`."""
    from apps.workspaces import permissions as perms

    if not getattr(user, "is_authenticated", False) or task.project_id is None:
        return False
    project = task.project
    return (project.owner_user_id == user.pk or user.pk in _checkin_leads(task)
            or perms.can(user, project.agent.workspace_id, perms.PROJECTS_DECIDE))


def _task_declined_by_a_person(task: AgentTask) -> bool:
    return task.actions.filter(action=AgentTaskAction.DECLINE).exclude(by=EXPIRY_BY).exists()


def _huddle_of(source_url: str) -> str:
    from apps.huddles.services import _huddle_of_url

    return _huddle_of_url(source_url)


def _sync_task_plan(task: AgentTask, plan: list[str]) -> None:
    """The task's plan steps (its child tasks) become exactly `plan`, in order; a
    step already being worked on is kept as it is."""
    from . import services

    steps = task_plan_steps(task)
    for i, title in enumerate(plan):
        if i < len(steps):
            step = steps[i]
            if step.status == AgentTask.SUGGESTED and (step.title, step.position) != (title, i):
                step.title, step.position = title, i
                step.save(update_fields=["title", "position", "updated_at"])
            continue
        services.create_tasks(task.agent, [{
            "title": title, "project": task.project.ext_id, "parent": task.ext_id, "status": AgentTask.SUGGESTED,
            "position": i, "origin_ref": {"kind": TASK_PLAN_STEP, "task_id": task.pk},
            "source_url": page_url(task.project)}])
    for step in steps[len(plan):]:
        if step.status == AgentTask.SUGGESTED:
            step.delete()


def _sync_task_partners(task: AgentTask, partners: list[Agent]) -> None:
    """One linked `suggested` task on each partner's board; a partner dropped by a
    revision has its still-suggested task declined."""
    from . import services

    project = task.project
    have = {t.agent_id: t for t in task_partner_tasks(task)}
    title = f"[{project.agent.slug} {task.ext_id}] {task.title}"[:300]
    for partner in partners:
        row = have.pop(partner.pk, None)
        if row is not None:
            if row.title != title:
                row.title = title
                row.save(update_fields=["title", "updated_at"])
            continue
        services.create_tasks(partner, [{
            "title": title, "status": AgentTask.SUGGESTED,
            "rationale": f"{project.agent.slug} proposes {task.title!r} in {project.name} with you as a partner.",
            "origin_ref": {"kind": TASK_PARTNER, "task_id": task.pk, "lead": project.agent.slug,
                           "task": task.ext_id, "project": project.ext_id},
            "source_url": page_url(project)}])
    for row in have.values():
        if row.status == AgentTask.SUGGESTED:
            row.status = AgentTask.DECLINED
            row.save(update_fields=["status", "updated_at"])


@transaction.atomic
def propose_task(project: AgentProject, *, title: str, why: str, done_when: str, plan: list[str],
                 partners: list[Agent], source_url: str = "", run=None,
                 new_evidence: str = "") -> tuple[AgentTask, bool]:
    """Create — or revise — a proposed task in `project`; returns (task, created).

    The task-level twin of `propose`: REVISING an open proposed task of the same
    title (ignoring case) updates it in place and marks its backings `stale`; a
    title a person DECLINED comes back only with `new_evidence`, else
    `DeclinedBefore`. `run` is the CheckinRun it came from (it must be this
    project's)."""
    from . import services

    title, why, done_when = title.strip(), why.strip(), done_when.strip()
    plan = [s.strip() for s in plan if s and s.strip()]
    agent = project.agent
    if not (title and why and done_when):
        raise ProposalError("a proposed task needs a title, a why and a done_when")
    if any(p.pk == agent.pk for p in partners):
        raise ProposalError(f"{agent.slug} leads this project; it cannot also be a partner")
    if run is not None and run.project_id != project.pk:
        raise UnknownRun(f"check-in run {run.pk} is not {project.name}'s")
    if new_evidence.strip():
        why = f"{why}\n\nNew evidence: {new_evidence.strip()}"
    now = timezone.now()
    huddle_id = _huddle_of(source_url) or (run.huddle_id if run is not None else "")

    task = (AgentTask.objects.select_for_update().select_related("agent", "project")
            .filter(project=project, origin_ref__kind=CHECKIN_PROPOSAL, status=AgentTask.SUGGESTED,
                    title__iexact=title).first())
    created = task is None
    if created:
        if not new_evidence.strip() and any(
                _task_declined_by_a_person(t) for t in project.tasks.filter(
                    origin_ref__kind=CHECKIN_PROPOSAL, status=AgentTask.DECLINED, title__iexact=title)):
            raise DeclinedBefore(f"{title!r} was proposed in {project.name} before and declined: "
                                 "send new_evidence to propose it again")
        ref = {"kind": CHECKIN_PROPOSAL, "checkin": run.checkin_id if run is not None else None,
               "run": run.pk if run is not None else None, "runs": [run.pk] if run is not None else [],
               "huddle_id": huddle_id, "done_when": done_when, "proposed_at": now.isoformat()}
        [task] = services.create_tasks(agent, [{
            "title": title[:300], "project": project.ext_id, "status": AgentTask.SUGGESTED, "rationale": why,
            "origin_ref": ref, "source_url": source_url or page_url(project)}])
    else:
        ref = dict(_ref_of(task))
        runs = task_runs(task)
        if run is not None:
            if run.pk not in runs:
                runs.append(run.pk)
            ref.update(checkin=run.checkin_id, run=run.pk)
        ref.update(runs=runs, done_when=done_when, proposed_at=now.isoformat())
        if huddle_id:
            ref["huddle_id"] = huddle_id
        task.origin_ref, task.rationale = ref, why
        fields = ["origin_ref", "rationale", "updated_at"]
        if source_url:
            task.source_url = source_url
            fields.append("source_url")
        task.save(update_fields=fields)
        task.backings.update(stale=True)
    _sync_task_plan(task, plan)
    _sync_task_partners(task, partners)
    return task, created


def _task_comments(task: AgentTask) -> list[dict]:
    agent_users = {task.agent.user_id} - {None}
    rows = task.actions.filter(action=AgentTaskAction.REPLY).order_by("created_at", "pk")
    return [{"by": r.by, "by_agent": r.by_user_id in agent_users, "comment": r.comment,
             "at": r.created_at} for r in rows]


def spawned_of(task: AgentTask) -> list[dict]:
    """The projects this task grew into ("spawned → P9")."""
    from apps.workspaces import services as wsvc

    return [{"agent": p.agent.slug, "ref": p.ext_id, "name": p.name, "status": p.status,
             "url": wsvc.scoped_path(p.agent.workspace_id, page_path(p))}
            for p in task.spawned_projects.select_related("agent").order_by("pk")]


def task_card(task: AgentTask, user) -> dict:
    """A proposed task as the project page and the huddle page both render it
    (`ProposalCard`): what it is, the decision and whether the reader may make
    it, where people stand, what they said, its partners, and any project it
    spawned."""
    from apps.contacts import services as contacts
    from apps.workspaces import services as wsvc

    project = task.project
    ref = _ref_of(task)
    me = contacts.person_for(user=user) if getattr(user, "is_authenticated", False) else None
    proposed_at = _proposed_at(task) or task.created_at
    partners = [{"agent": t.agent.slug, "ext_id": t.ext_id, "task_id": t.pk, "status": t.status,
                 "url": wsvc.scoped_path(t.agent.workspace_id, f"/agents/{t.agent.slug}/tasks")}
                for t in task_partner_tasks(task)]
    return {
        "id": task.pk, "agent": task.agent.slug, "workspace": task.agent.workspace_id, "ref": task.ext_id,
        "title": task.title, "status": task.status, "why": task.rationale,
        "done_when": str(ref.get("done_when") or ""),
        "plan": [s.title for s in task_plan_steps(task)],
        "project_ref": project.ext_id if project else "", "project_name": project.name if project else "",
        "project_url": wsvc.scoped_path(task.agent.workspace_id, page_path(project)) if project else "",
        "url": wsvc.scoped_path(task.agent.workspace_id, page_path(project)) if project else "",
        "source_url": task.source_url, "checkin": ref.get("checkin") if str(ref.get("checkin") or "").isdigit()
        else None, "run": ref.get("run") if str(ref.get("run") or "").isdigit() else None,
        "huddle_id": str(ref.get("huddle_id") or ""), "proposed_at": proposed_at,
        "expires_at": proposed_at + EXPIRES_AFTER if task.status == AgentTask.SUGGESTED else None,
        "notes": task.notes, "decision_open": task.status == AgentTask.SUGGESTED,
        "may_decide": may_decide_task(user, task),
        "backings": backings_of(task, me=me), "comments": _task_comments(task), "partners": partners,
        "spawned": spawned_of(task),
    }


def proposed_tasks_of(project: AgentProject):
    return (project.tasks.filter(origin_ref__kind=CHECKIN_PROPOSAL)
            .select_related("agent", "project").order_by("-created_at", "-pk"))
